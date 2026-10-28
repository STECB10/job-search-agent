"""Pré-remplissage des formulaires de candidature (Greenhouse, Lever, Ashby).

Le bot ouvre le formulaire dans Chrome sur ton Mac, le remplit et joint CV et lettre,
puis s'arrête : c'est toi qui relis et cliques sur « Submit ». Il ne coche jamais
les cases de consentement (confidentialité, RGPD, conditions).

    python -m jobbot.prefill <id de l'offre> <fr|en|non>     lancé par le bot Telegram
    python -m jobbot.prefill --essai <url | id> [fr|en]      test invisible, rien n'est envoyé
"""
import json
import logging
import re
import sys
import unicodedata
from pathlib import Path

import yaml
from playwright.sync_api import Page, sync_playwright

from . import config

log = logging.getLogger(__name__)

PATTERNS = {
    "ashby": re.compile(r"https://jobs\.ashbyhq\.com/[^/]+/[0-9a-f-]{36}"),
    "lever": re.compile(r"https://jobs\.lever\.co/[^/]+/[0-9a-f-]{36}"),
    "greenhouse": re.compile(r"https://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/[^/]+/jobs/\d+"),
}
WAIT_FOR_USER_MS = 3 * 3600 * 1000  # le formulaire reste ouvert 3 heures au plus

CONSENT = re.compile(r"consent|privacy|gdpr|terms|agree|acknowledg|policy|politique|consentement|accepte|"
                     r"reviewed our|confirm that you have read|data protection|retain", re.I)
DIVERSITY = re.compile(r"gender|race|ethnic|veteran|disab|sexual|pronoun|hispanic|genre|origine|handicap", re.I)
DECLINE = re.compile(r"decline|prefer not|don.t wish|do not wish|not to (say|answer|disclose)|"
                     r"ne souhaite pas|pr[ée]f[èe]re ne pas", re.I)

# Repère chaque champ du formulaire, lui donne une clé (attribut data-jobbot) et
# renvoie sa description : type, intitulé, options, obligatoire ou non.
EXTRACT_JS = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').replace(/[✱*]\s*$/, '').trim();
  const visible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  document.querySelectorAll('[data-jobbot]').forEach(e => e.removeAttribute('data-jobbot'));
  document.querySelectorAll('[data-jobbot-opt]').forEach(e => e.removeAttribute('data-jobbot-opt'));
  const fields = []; let n = 0;
  const container = (e) => e.closest('[data-field-path], li.application-question, .application-question, ' +
                                      '.field-wrapper, fieldset, .select__container, .text-input-wrapper');
  const labelOf = (e) => {
    const c = e.closest('[data-field-path]');
    if (c) { const l = c.querySelector('label, legend'); const d = c.querySelector('p');
             return clean((l ? l.innerText : '') + (d ? ' — ' + d.innerText : '')); }
    const lever = e.closest('li.application-question, .application-question');
    if (lever) { const l = lever.querySelector('.application-label, .text');
                 if (l) return clean(l.innerText); }
    if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) return clean(l.innerText); }
    const by = e.getAttribute('aria-labelledby');
    if (by) { const t = by.split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
              if (clean(t)) return clean(t); }
    if (e.labels && e.labels.length) return clean(e.labels[0].innerText);
    return clean(e.getAttribute('aria-label') || e.placeholder || e.name || '');
  };
  const add = (el, kind, extra = {}) => {
    const key = 'k' + (n++); el.setAttribute('data-jobbot', key);
    fields.push({key, kind, label: labelOf(el), hint: ((el.id || '') + ' ' + (el.name || '')).trim(),
                 required: !!(el.required || el.getAttribute('aria-required') === 'true'), ...extra});
  };
  // Boutons Oui / Non (Ashby)
  for (const g of document.querySelectorAll('[data-field-path]')) {
    const opts = [...g.querySelectorAll('button[data-option]')];
    if (opts.length) add(g, 'boutons', {options: opts.map(b => clean(b.innerText))});
  }
  const radios = {}, boxes = {};
  for (const e of document.querySelectorAll('input, textarea, select')) {
    if (e.closest('[data-jobbot]') || e.getAttribute('aria-hidden') === 'true') continue;
    const t = (e.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'search', 'image', 'reset'].includes(t)) continue;
    if ((e.name || '').startsWith('g-recaptcha') || e.name === 'h-captcha-response') continue;
    if (t === 'radio') { (radios[e.name] = radios[e.name] || []).push(e); continue; }
    if (t === 'checkbox') { (boxes[e.name || e.id] = boxes[e.name || e.id] || []).push(e); continue; }
    if (t === 'file') { add(e, 'fichier'); continue; }
    if (!visible(e)) continue;
    if (e.tagName === 'SELECT') { add(e, 'liste', {options: [...e.options].map(o => clean(o.text)).filter(x => x && !/^select/i.test(x))}); continue; }
    if (e.getAttribute('role') === 'combobox') { add(e, 'liste_dynamique'); continue; }
    add(e, e.tagName === 'TEXTAREA' ? 'texte_long' : 'texte');
  }
  const optLabel = (e) => clean((e.labels && e.labels[0] ? e.labels[0].innerText : '') || e.value);
  for (const [name, group] of Object.entries(radios)) {
    const c = container(group[0]) || group[0].parentElement;
    group.forEach((e, i) => e.setAttribute('data-jobbot-opt', optLabel(e)));
    add(c, 'choix_unique', {options: group.map(optLabel), label: undefined});
    fields[fields.length - 1].label = labelOf(group[0]);
  }
  for (const [name, group] of Object.entries(boxes)) {
    if (group.length === 1) { add(group[0], 'case', {texte: optLabel(group[0])}); continue; }
    const c = container(group[0]) || group[0].parentElement;
    group.forEach(e => e.setAttribute('data-jobbot-opt', optLabel(e)));
    add(c, 'choix_multiple', {options: group.map(optLabel)});
    fields[fields.length - 1].label = labelOf(group[0]);
  }
  return fields;
}
"""


def supported(url: str) -> str | None:
    """Système de candidature reconnu dans l'adresse de l'offre, ou None."""
    for name, pattern in PATTERNS.items():
        if pattern.search(url or ""):
            return name
    return None


def application_url(url: str) -> str:
    ats = supported(url)
    base = PATTERNS[ats].search(url).group(0)
    return {"ashby": base + "/application", "lever": base + "/apply"}.get(ats, base)


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", text).strip()


def standard_answer(field: dict, facts: dict, files: dict) -> tuple[str, str] | None:
    """Champs d'identité et fichiers, remplis sans IA. Renvoie (valeur, statut) ou None."""
    label, kind = norm(field["label"]), field["kind"]
    if kind == "fichier":
        hint = f"{label} {norm(field.get('hint', ''))}"
        if "autofill" in hint or not field.get("hint"):
            return None  # champ « remplir depuis le CV » d'Ashby, ou champ inconnu
        if re.search(r"cover|lettre|motivation", hint):
            return (files["lettre"], "rempli") if files.get("lettre") else None
        if re.search(r"resume|\bcv\b|curriculum", hint):
            return (files["cv"], "rempli") if files.get("cv") else None
        return None
    rules = [
        (r"preferred (first )?name|middle name", ""),
        (r"first name|given name|prenom", facts.get("prenom")),
        (r"last name|family name|surname|^nom$", facts.get("nom")),
        (r"^(full )?name$|^nom complet|^your name", f"{facts.get('prenom', '')} {facts.get('nom', '')}".strip()),
        (r"e-?mail", facts.get("email")),
        (r"phone|telephone|mobile", facts.get("telephone")),
        (r"linkedin", facts.get("linkedin")),
        (r"github", facts.get("github")),
        (r"website|portfolio|personal site|site web", facts.get("site_web")),
        (r"current (company|employer)|^org|entreprise actuelle", facts.get("entreprise_actuelle")),
        (r"current (title|position|role)|poste actuel", facts.get("poste_actuel")),
        (r"^country|^pays", facts.get("pays")),
        (r"location|city|ville|where are you (based|located)", facts.get("ville")),
    ]
    if kind not in ("texte", "liste_dynamique", "liste"):
        return None
    for pattern, value in rules:
        if re.search(pattern, label):
            if value == "":
                return ("", "ignore")  # champ facultatif qu'on laisse vide
            return (value, "rempli") if value else None
    return None


class Filler:
    def __init__(self, page: Page, files: dict, facts: dict):
        self.page, self.files, self.facts = page, files, facts

    def fields(self) -> list[dict]:
        return self.page.evaluate(EXTRACT_JS)

    def read_dynamic_options(self, field: dict) -> None:
        """Les listes Greenhouse ne montrent leurs options qu'une fois ouvertes."""
        el = self.page.locator(f'[data-jobbot="{field["key"]}"]')
        try:
            el.click(timeout=3000)
            self.page.wait_for_timeout(400)
            field["options"] = [o.strip() for o in self.page.locator('[role="option"]:visible').all_inner_texts()][:60]
            el.press("Escape")
        except Exception:
            field["options"] = []

    def fill(self, field: dict, value: str) -> bool:
        el = self.page.locator(f'[data-jobbot="{field["key"]}"]')
        kind = field["kind"]
        try:
            if kind == "fichier":
                el.set_input_files(value)
                self.page.wait_for_timeout(1500)  # Ashby relit le CV juste après le dépôt
            elif kind in ("texte", "texte_long"):
                el.fill(value)
            elif kind == "liste":
                el.select_option(label=value)
            elif kind == "liste_dynamique":
                el.click()
                el.fill(value)
                self.page.wait_for_timeout(1200)
                option = self.page.locator('[role="option"]:visible').filter(has_text=value).first
                if option.count():
                    option.click()
                else:
                    first = self.page.locator('[role="option"]:visible').first
                    if not first.count():
                        el.press("Escape")
                        return False
                    first.click()
            elif kind == "boutons":
                el.locator("button[data-option]").filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)).first.click()
            elif kind in ("choix_unique", "choix_multiple"):
                for choice in value.split(" | "):
                    self.page.locator(f'[data-jobbot-opt="{choice.strip()}"]').first.check()
            else:
                return False
            return True
        except Exception as exc:
            log.warning("Champ « %s » non rempli : %s", field["label"][:60], exc)
            return False


def prefill(page: Page, job: dict, files: dict, claude=None) -> dict:
    """Remplit le formulaire ouvert dans `page`. Renvoie le bilan pour Telegram."""
    facts_text = (config.PROFIL / "candidature.yaml").read_text(encoding="utf-8")
    facts = yaml.safe_load(facts_text) or {}
    filler = Filler(page, files, facts)
    report = {"rempli": [], "a_verifier": [], "a_toi": [], "consentements": []}

    # 1. Fichiers d'abord : Ashby redessine le formulaire après le dépôt du CV,
    #    on relit donc les champs ensuite.
    for f in [f for f in filler.fields() if f["kind"] == "fichier"]:
        answer = standard_answer(f, facts, files)
        if answer and filler.fill(f, answer[0]):
            report["rempli"].append("CV" if answer[0] == files.get("cv") else "Lettre de motivation")
    page.wait_for_timeout(2500)
    fields = filler.fields()

    questions, by_key = [], {f["key"]: f for f in fields}
    for f in fields:
        if f["kind"] == "fichier":
            continue
        if f["kind"] == "case" and CONSENT.search(f.get("texte", "") + " " + f["label"]):
            report["consentements"].append(f.get("texte") or f["label"])
            continue
        if CONSENT.search(f["label"]) and f["kind"] in ("boutons", "choix_unique", "liste", "liste_dynamique", "case"):
            report["consentements"].append(f["label"])
            continue
        answer = standard_answer(f, facts, files)
        if answer and answer[1] == "ignore":
            continue
        if answer:
            if filler.fill(f, answer[0]):
                report["rempli"].append(f["label"])
            continue
        if f["kind"] == "liste_dynamique":
            filler.read_dynamic_options(f)
        if DIVERSITY.search(f["label"]) and f.get("options"):
            decline = next((o for o in f["options"] if DECLINE.search(o)), None)
            if decline and filler.fill(f, decline):
                report["rempli"].append(f["label"])
            else:
                report["a_toi"].append(f["label"])
            continue
        questions.append({k: f[k] for k in ("key", "kind", "label", "required", "options") if k in f})

    # 2. Questions propres à l'offre : rédigées par Claude, avec tes réponses factuelles.
    if questions and claude is not None:
        for q in questions:
            q["cle"] = q.pop("key")
        answers = claude.answer_form(job, facts_text, questions)
        for a in answers:
            f = by_key.get(a["cle"])
            if f is None:
                continue
            value = a["valeur"].strip()
            if f.get("options") and value and not all(v.strip() in f["options"] for v in value.split(" | ")):
                value = ""  # option inventée : on laisse la question à l'utilisateur
            if a["statut"] == "vide" or not value or not filler.fill(f, value):
                report["a_toi"].append(f["label"])
            elif a["statut"] == "a_verifier":
                report["a_verifier"].append(f"{f['label']} → {value[:80]}")
            else:
                report["rempli"].append(f["label"])
    else:
        report["a_toi"] += [q["label"] for q in questions]
    return report


def letter_pdf(job_id: int) -> Path | None:
    found = sorted((config.DATA / "lettres" / str(job_id)).glob("*.pdf"))
    return found[0] if found else None


def main() -> None:
    logging.basicConfig(level=logging.INFO, filename=config.DATA / "prefill.log",
                        format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    cfg = config.load_config()
    test = sys.argv[1] == "--essai"
    target = sys.argv[2] if test else sys.argv[1]
    if test and not target.isdigit():  # essai sur une simple adresse : identité et CV seulement
        url, job, files, claude = target, {"title": "test", "company": "test", "source": "essai"}, \
            {"cv": config.ROOT / cfg["cv"]["fr"]}, None
    else:
        from .db import Store
        from .llm import Claude
        from .pipeline import Pipeline
        store = Store(config.DATA / "jobbot.sqlite")
        job = store.get(int(target))
        lang = (sys.argv[3] if test else sys.argv[2]) if len(sys.argv) > (3 if test else 2) else "fr"
        cv = Pipeline(cfg, store).cv_path(lang) if lang in ("fr", "en") else None
        files = {"cv": str(cv) if cv else None, "lettre": str(letter_pdf(job["id"]) or "") or None}
        url, claude = job["url"], Claude(cfg["ia"], cfg["tri"]["preferences"], cfg["identite"]["nom"])
    headless = test
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless)
        page = browser.new_context(no_viewport=not headless).new_page()
        page.set_default_timeout(8000)
        try:
            page.goto(application_url(url), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            report = prefill(page, job, {k: str(v) for k, v in files.items() if v}, claude)
            report["ok"] = True
        except Exception as exc:
            log.exception("Pré-remplissage en échec")
            report = {"ok": False, "erreur": str(exc)[:300]}
        if headless:
            page.screenshot(path=str(config.DATA / "prefill_essai.png"), full_page=True)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not headless:
            try:  # on laisse la fenêtre ouverte jusqu'à ce que l'utilisateur la ferme
                page.wait_for_event("close", timeout=WAIT_FOR_USER_MS)
            except Exception:
                pass
        browser.close()


if __name__ == "__main__":
    main()
