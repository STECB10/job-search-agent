"""Le travail du bot, sans l'interface : collecter, filtrer, noter, rédiger, envoyer."""
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic

from . import config, documents
from .db import Store
from .filters import HardFilter
from .llm import ClaudeNotConnected, ClaudeUnavailable
from .models import Job, normalize
from .sources import adzuna, ats, linkedin_alerts
from .sources.france_travail import FranceTravail

log = logging.getLogger(__name__)

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
# Erreurs passagères de l'API : on retentera au prochain passage.
RETRYABLE = (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError,
             ClaudeUnavailable)


class Pipeline:
    def __init__(self, cfg: dict, store: Store, claude=None):
        self.cfg, self.store, self.claude = cfg, store, claude
        self.filter = HardFilter(cfg["tri"])
        ft_id, ft_secret = config.env("FRANCE_TRAVAIL_CLIENT_ID"), config.env("FRANCE_TRAVAIL_CLIENT_SECRET")
        self.france_travail = FranceTravail(ft_id, ft_secret) if ft_id and ft_secret else None

    # ─────────────── Collecte ───────────────

    def available_sources(self) -> list[str]:
        s = self.cfg["sources"]
        ready = {
            "ats": True,
            "france_travail": self.france_travail is not None,
            "adzuna": bool(config.env("ADZUNA_APP_ID") and config.env("ADZUNA_APP_KEY")),
            "linkedin_alertes": self.can_use_gmail(),
        }
        return [name for name, ok in ready.items() if ok and s[name]["actif"]]

    def _fetch(self, name: str, first_run: bool) -> list[Job]:
        s, age = self.cfg["sources"], self.cfg["tri"]["age_max_jours"]
        if name == "ats":
            return ats.fetch(s["ats"]["entreprises"])
        if name == "france_travail":
            return self.france_travail.fetch(s["france_travail"]["mots_cles"], age)
        if name == "adzuna":
            return adzuna.fetch(config.env("ADZUNA_APP_ID"), config.env("ADZUNA_APP_KEY"),
                                s["adzuna"]["pays"], age)
        li = s["linkedin_alertes"]
        return linkedin_alerts.fetch(config.env("GMAIL_ADRESSE"), config.env("GMAIL_MOT_DE_PASSE_APPLI"),
                                     li["expediteurs"], age if first_run else 2, li.get("dossier", "INBOX"))

    def collect(self, force: bool = False) -> int:
        """Interroge les sources dont l'intervalle est écoulé. Renvoie le nombre
        d'offres nouvelles qui passent les filtres."""
        now = datetime.now(timezone.utc)
        queued = 0
        for name in self.available_sources():
            last = self.store.get_state(f"last_run:{name}")
            interval = timedelta(minutes=self.cfg["sources"][name]["intervalle_minutes"])
            if not force and last and now - datetime.fromisoformat(last) < interval:
                continue
            try:
                jobs = self._fetch(name, first_run=not last)
            except Exception as exc:
                log.warning("Source %s en échec : %s", name, exc)
                continue
            self.store.set_state(f"last_run:{name}", now.isoformat())
            added = self.ingest(jobs)
            log.info("%s : %d offres lues, %d nouvelles retenues", name, len(jobs), added)
            queued += added
        return queued

    def ingest(self, jobs: list[Job]) -> int:
        added = 0
        for job in jobs:
            if not job.title:
                continue
            if self.store.is_known(job):
                self.store.enrich(job)
                continue
            reason = self.filter.reject_reason(job)
            self.store.add(job, "filtre" if reason else "a_noter", reason)
            added += reason is None
        return added

    # ─────────────── Tri par l'IA ───────────────

    def triage(self) -> int:
        if not self.claude:
            return 0
        rows = self.store.by_status("a_noter", self.cfg["tri"]["max_offres_ia_par_cycle"])
        rejections = self.store.recent_rejections()
        done = 0
        for row in rows:
            try:
                self._score_and_route(row, rejections)
            except RETRYABLE as exc:
                log.warning("API Claude indisponible, reprise au prochain passage : %s", exc)
                break
            except anthropic.AuthenticationError:
                log.error("Clé ANTHROPIC_API_KEY refusée : vérifie le fichier .env")
                break
            except ClaudeNotConnected as exc:
                log.error("%s", exc)
                break
            except Exception as exc:
                log.exception("Notation impossible pour %s", row["uid"])
                self.store.set_status(row["id"], "erreur", f"notation : {exc}")
            done += 1
        return done

    def _score_and_route(self, row: dict, rejections: list[dict]) -> None:
        tri = self.cfg["tri"]
        analysis = self.claude.score(row, rejections)
        self.store.save_analysis(row["id"], analysis, self._checked_email(row, analysis))
        score = analysis["score"]
        status = "ecarte"
        if score >= tri["seuil_lettre"]:
            try:
                self.write_letter(self.store.get(row["id"]))
                status = "a_envoyer"
            except RETRYABLE:
                raise
            except Exception:
                log.exception("Lettre impossible pour %s : offre signalée sans lettre", row["uid"])
                status = "a_signaler"
        elif score >= tri["seuil_signalement"]:
            status = "a_signaler"
        self.store.set_status(row["id"], status)
        log.info("%d/100 %s — %s -> %s", score, row["title"], row["company"], status)

    @staticmethod
    def _checked_email(row: dict, analysis: dict) -> str:
        """N'accepte qu'une adresse réellement écrite dans l'offre (pas d'adresse inventée)."""
        description = (row.get("description") or "").lower()
        for candidate in (row.get("apply_email") or "", analysis.get("email_candidature") or ""):
            candidate = candidate.strip()
            if EMAIL.fullmatch(candidate) and (candidate == row.get("apply_email")
                                               or candidate.lower() in description):
                return candidate
        return ""

    def run_cycle(self, force: bool = False) -> dict:
        return {"nouvelles": self.collect(force), "notees": self.triage()}

    # ─────────────── Lettres ───────────────

    def write_letter(self, row: dict, instruction: str = "") -> dict:
        letter = self.claude.write_letter(row, today=documents.long_date(date.today()),
                                          current=row.get("letter"), instruction=instruction)
        self.store.save_letter(row["id"], letter)
        return letter

    def prepare_letter(self, job_id: int, description: str | None = None) -> dict:
        """Lettre demandée par l'utilisateur pour une offre signalée, avec éventuellement
        le texte de l'offre qu'il a collé (l'offre est alors renotée)."""
        if description:
            self.store.update_description(job_id, description)
            row = self.store.get(job_id)
            analysis = self.claude.score(row, self.store.recent_rejections())
            self.store.save_analysis(job_id, analysis, self._checked_email(row, analysis))
        self.write_letter(self.store.get(job_id))
        return self.store.get(job_id)

    def revise_letter(self, job_id: int, instruction: str) -> dict:
        self.write_letter(self.store.get(job_id), instruction)
        return self.store.get(job_id)

    def replace_letter(self, job_id: int, text: str) -> dict:
        row = self.store.get(job_id)
        self.store.save_letter(job_id, {**(row.get("letter") or {"objet": row["title"], "message_court": ""}),
                                        "corps": text})
        return self.store.get(job_id)

    # ─────────────── Validation et envoi ───────────────

    def can_use_gmail(self) -> bool:
        return bool(config.env("GMAIL_ADRESSE") and config.env("GMAIL_MOT_DE_PASSE_APPLI"))

    def sends_by_email(self, row: dict) -> bool:
        return bool(row.get("apply_email") and self.cfg["envoi"]["email_auto"] and self.can_use_gmail())

    def validate(self, job_id: int, cv_lang: str | None) -> tuple[str, Path, Path | None]:
        """Après ✅ puis le choix du CV : PDF de la lettre, puis envoi par email si possible.
        cv_lang vaut « fr », « en » ou None (sans CV).
        Renvoie (« email » ou « manuel », PDF de la lettre, CV joint ou None)."""
        row = self.store.get(job_id)
        analysis = row.get("analysis") or {}
        english = analysis.get("langue") == "en"
        who = "_".join(self.cfg["identite"]["nom"].split())
        name = f"Cover_letter_{who}.pdf" if english else f"Lettre_de_motivation_{who}.pdf"
        pdf = documents.letter_pdf(config.DATA / "lettres" / str(job_id) / name, self.cfg["identite"],
                                   row["letter"], row["company"], analysis.get("destinataire", ""),
                                   english, date.today())
        cv = self.cv_path(cv_lang) if cv_lang else None
        if cv_lang and cv is None:
            raise ValueError(f"aucun CV « {cv_lang} » configuré")
        self._remember(row)
        if self.sends_by_email(row):
            documents.send_email(config.env("GMAIL_ADRESSE"), config.env("GMAIL_MOT_DE_PASSE_APPLI"),
                                 row["apply_email"], row["letter"]["objet"],
                                 documents.email_body(row["letter"], self.cfg["identite"], english),
                                 [f for f in (cv, pdf) if f])
            self.store.set_status(job_id, "postule", f"email envoyé à {row['apply_email']}")
            return "email", pdf, cv
        self.store.set_status(job_id, "valide")
        return "manuel", pdf, cv

    def cv_path(self, lang: str) -> Path | None:
        """CV en français (« fr ») ou en anglais (« en »), s'il est configuré et présent."""
        rel = (self.cfg.get("cv") or {}).get(lang)
        path = config.ROOT / rel if rel else None
        return path if path and path.exists() else None

    def _remember(self, row: dict) -> None:
        """Chaque lettre validée devient un exemple de style pour les suivantes."""
        folder = config.PROFIL / "lettres_validees"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{date.today().isoformat()}_{row['id']}_{normalize(row['company'])[:30]}.txt"
        path.write_text(f"Poste : {row['title']} ({row['company']})\n\n{row['letter']['corps']}\n",
                        encoding="utf-8")
