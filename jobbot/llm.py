"""Appels à Claude : noter une offre et rédiger une lettre.

Deux moteurs possibles (réglage `ia.moteur` dans config.yaml) :
- « abonnement » : passe par Claude Code et ton abonnement Claude, sans clé API ;
- « api » : appelle l'API Anthropic avec ANTHROPIC_API_KEY, facturée à l'usage.
"""
import json
import os
import shutil
import subprocess

import anthropic

from .config import DATA, read_profile, validated_letters


class ClaudeUnavailable(Exception):
    """Erreur passagère (quota de l'abonnement atteint, réseau) : on retentera plus tard."""


class ClaudeNotConnected(Exception):
    """Claude Code n'est pas connecté à ton compte."""

# Si un filtre de sécurité refuse une requête par erreur, l'API la relance
# automatiquement sur le modèle de secours recommandé par Anthropic.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_DESCRIPTION = 15000  # caractères ; une offre normale en fait 2 000 à 6 000

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "resume": {"type": "string"},
        "points_forts": {"type": "array", "items": {"type": "string"}},
        "points_faibles": {"type": "array", "items": {"type": "string"}},
        "type_employeur": {"type": "string", "enum": [
            "startup", "entreprise", "cabinet de conseil", "hôpital ou public",
            "recherche académique", "enseignement", "autre"]},
        "langue": {"type": "string", "enum": ["fr", "en", "autre"]},
        "email_candidature": {"type": "string"},
        "destinataire": {"type": "string"},
    },
    "required": ["score", "resume", "points_forts", "points_faibles", "type_employeur",
                 "langue", "email_candidature", "destinataire"],
    "additionalProperties": False,
}

LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "objet": {"type": "string"},
        "corps": {"type": "string"},
        "message_court": {"type": "string"},
    },
    "required": ["objet", "corps", "message_court"],
    "additionalProperties": False,
}

SCORING_RULES = """Barème de la note (0 à 100) :
- 85 à 100 : excellente correspondance. Le cœur du poste (IA, data, statistiques) colle à ses
  compétences, l'employeur construit quelque chose, le lieu et le niveau conviennent.
- 75 à 84 : bonne correspondance, écarts mineurs.
- 60 à 74 : possible, avec un écart net (niveau, secteur, employeur moins attirant,
  compétence clé manquante).
- Moins de 60 : ne vaut pas une candidature.
- Moins de 40 : critère éliminatoire (employeur d'enseignement ou université, stage, alternance,
  thèse, postdoc académique, poste sans data ni IA, poste de direction, langue exigée qu'il ne
  parle pas, lieu hors des zones acceptées).

Lis les missions et les exigences : un titre « Data Scientist » ne suffit pas.
Si la description manque ou se limite à quelques lignes, ne dépasse pas 70 et signale-le dans
les points faibles.
Les offres que {prenom} a déjà écartées, s'il y en a, montrent ce qu'il ne veut pas : tiens-en compte.

Champs à remplir :
- resume : le poste en une phrase simple.
- points_forts : 2 à 4 raisons concrètes de postuler, liées à son parcours.
- points_faibles : 0 à 3 réserves concrètes.
- langue : langue de la lettre. Celle de l'offre, sauf une offre en anglais pour un poste en
  France ou en Suisse romande qui demande le français : alors « fr ».
- email_candidature : seulement si l'offre demande explicitement d'envoyer la candidature à une
  adresse email écrite dans le texte ; sinon chaîne vide.
- destinataire : nom ou fonction de la personne à qui adresser la lettre si l'offre le donne ;
  sinon chaîne vide.
Réponds en français, de façon concise."""

LETTER_OUTPUT = """Ce que tu rends :
- objet : la ligne d'objet (« Candidature au poste de … », en anglais « Application for … »),
  avec la référence de l'offre si elle existe.
- corps : la lettre complète, de la formule d'appel (« Madame, Monsieur, » ou
  « Dear Hiring Team, ») jusqu'à la signature « {nom} ». Un paragraphe par bloc,
  blocs séparés par une ligne vide. Ni en-tête, ni date, ni adresse : ils sont ajoutés
  automatiquement.
- message_court : 3 ou 4 phrases dans la même voix, pour le champ « message au recruteur »
  d'un formulaire ou le corps d'un email de candidature. Sans formule d'appel ni signature."""


FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "reponses": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "cle": {"type": "string"},
                "valeur": {"type": "string"},
                "statut": {"type": "string", "enum": ["rempli", "a_verifier", "vide"]},
                "raison": {"type": "string"},
            },
            "required": ["cle", "valeur", "statut", "raison"],
            "additionalProperties": False,
        }},
    },
    "required": ["reponses"],
    "additionalProperties": False,
}

FORM_RULES = """Tu remplis les questions d'un formulaire de candidature à la place de
{nom}. Il relit tout avant d'envoyer lui-même le formulaire.

Règles :
- Réponds dans la langue du formulaire (anglais si les questions sont en anglais).
- Faits : uniquement ceux de la fiche de faits et des réponses factuelles. Salaire, préavis,
  date de disponibilité, nationalité, visa ou autorisation de travail : seulement si la réponse
  factuelle correspondante est remplie. Sinon statut « vide » et valeur vide.
- Questions libres (motivation, expérience, technologies) : 2 à 5 phrases dans sa voix, simples,
  concrètes, cohérentes avec sa lettre. Aucune formule toute faite.
- Choix fermés (options fournies) : la valeur doit reprendre exactement le texte d'une option.
  Pour plusieurs choix, sépare les options par « | ».
- Questions de diversité (genre, origine, handicap…) : l'option qui refuse de répondre si elle
  existe, sinon « vide ».
- Mets « a_verifier » quand la réponse suppose un choix de {prenom} (déménagement, présence au
  bureau, mobilité) ou une interprétation. « rempli » seulement quand c'est sûr.
- raison : quelques mots en français pour {prenom}, surtout pour « a_verifier » et « vide ».
Rends une réponse pour chaque question reçue, avec sa clé."""


class Claude:
    def __init__(self, ia: dict, preferences: str, name: str = "le candidat"):
        self.ia = ia
        self.name, self.first_name = name, name.split()[0]
        self.engine = ia.get("moteur", "abonnement")
        if self.engine == "api":
            self.client = anthropic.Anthropic()
        else:
            self.claude_bin = shutil.which(ia.get("claude_code", "claude")) or ia.get("claude_code", "claude")
        self.profile, self.style, self.reference = read_profile()
        self.scoring_system = self._who("\n\n".join([
            "Tu tries des offres d'emploi pour {nom}. Il préfère recevoir peu "
            "d'offres, mais bonnes : sois exigeant.",
            f"<fiche_de_faits>\n{self.profile}\n</fiche_de_faits>",
            f"<preferences>\n{preferences}\n</preferences>",
            SCORING_RULES,
        ]))

    def score(self, job: dict, rejections: list[dict]) -> dict:
        parts = []
        if rejections:
            lines = [f"- {r['title']} ({r['company']}) : {r['reason']}" for r in rejections]
            parts.append(f"Offres récemment écartées par {self.first_name}, avec son motif :\n" + "\n".join(lines))
        parts.append(format_offer(job))
        result = self._call(self.ia["modele_tri"], self.ia["effort_tri"], self.scoring_system,
                            "\n\n".join(parts), SCORE_SCHEMA, max_tokens=8000)
        result["score"] = max(0, min(100, int(result["score"])))
        return result

    def write_letter(self, job: dict, today: str, current: dict | None = None,
                     instruction: str = "") -> dict:
        analysis = job.get("analysis") or {}
        english = analysis.get("langue") == "en"
        parts = [
            f"Date du jour : {today}",
            f"Langue de la lettre : {'anglais' if english else 'français'}",
            f"Destinataire : {analysis.get('destinataire') or 'non précisé'}",
            format_offer(job),
        ]
        if analysis:
            parts.append("<analyse_du_tri>\nPoints forts : " + " ; ".join(analysis.get("points_forts", []))
                         + "\nPoints faibles : " + " ; ".join(analysis.get("points_faibles", []))
                         + "\n</analyse_du_tri>")
        if current and instruction:
            parts.append(f"<lettre_actuelle>\n{current['corps']}\n</lettre_actuelle>")
            parts.append(f"<consigne_de_steven>\n{instruction}\n</consigne_de_steven>")
            parts.append("Réécris la lettre en suivant sa consigne. Garde ce que la consigne ne touche pas.")
        else:
            parts.append("Écris la lettre.")
        return self._call(self.ia["modele_lettre"], self.ia["effort_lettre"], self._letter_system(),
                          "\n\n".join(parts), LETTER_SCHEMA, max_tokens=16000)

    def answer_form(self, job: dict, facts: str, questions: list[dict]) -> list[dict]:
        system = self._who("\n\n".join([FORM_RULES, f"<guide_de_style>\n{self.style}\n</guide_de_style>",
                                f"<fiche_de_faits>\n{self.profile}\n</fiche_de_faits>"]))
        letter = (job.get("letter") or {}).get("corps", "")
        user = "\n\n".join([
            f"<reponses_factuelles>\n{facts}\n</reponses_factuelles>",
            format_offer(job),
            f"<lettre_envoyee>\n{letter}\n</lettre_envoyee>",
            "<questions>\n" + json.dumps(questions, ensure_ascii=False, indent=1) + "\n</questions>",
        ])
        result = self._call(self.ia["modele_lettre"], "medium", system, user, FORM_SCHEMA, max_tokens=16000)
        return result["reponses"]

    def _letter_system(self) -> str:
        # Reconstruit à chaque lettre pour intégrer les dernières lettres validées.
        parts = [
            "Tu écris les lettres de motivation de {nom}, avec sa voix à lui. "
            "Il relit chaque lettre avant de l'envoyer.",
            f"<guide_de_style>\n{self.style}\n</guide_de_style>",
            f"<fiche_de_faits>\n{self.profile}\n</fiche_de_faits>",
            "<lettre_de_reference>\nLettre écrite par {prenom}. Reproduis son ton, "
            f"pas son contenu, qui concerne un autre poste.\n\n{self.reference}\n</lettre_de_reference>",
        ]
        validated = validated_letters()
        if validated:
            parts.append("<lettres_validees>\nLettres récentes relues et validées par {prenom}, parfois "
                         "corrigées de sa main. Elles comptent plus que la lettre de référence.\n\n"
                         + "\n\n---\n\n".join(validated) + "\n</lettres_validees>")
        parts.append(LETTER_OUTPUT)
        return self._who("\n\n".join(parts))

    def _who(self, text: str) -> str:
        """Remplace {nom} et {prenom} dans les consignes, sans toucher au reste du texte."""
        return text.replace("{nom}", self.name).replace("{prenom}", self.first_name)

    def _call(self, model: str, effort: str, system: str, user: str, schema: dict,
              max_tokens: int) -> dict:
        if self.engine != "api":
            return self._call_claude_code(model, effort, system, user, schema)
        response = self.client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            # Le profil et les consignes ne changent pas d'un appel à l'autre : mis en cache
            # une heure, ce qui couvre plusieurs passages du bot.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Claude a refusé la requête : {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Réponse coupée avant la fin (max_tokens atteint)")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def _call_claude_code(self, model: str, effort: str, system: str, user: str, schema: dict) -> dict:
        """Claude Code en mode non interactif (`claude -p`), avec ton abonnement."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # sinon facturé en API
        DATA.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(
                [self.claude_bin, "-p", "--output-format", "json", "--model", model, "--effort", effort,
                 "--tools", "", "--no-session-persistence", "--system-prompt", system,
                 "--json-schema", json.dumps(schema)],
                input=user, capture_output=True, text=True, timeout=600, env=env,
                cwd=DATA,  # dossier neutre : aucun fichier de projet chargé
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeUnavailable("Claude Code n'a pas répondu en 10 minutes") from exc
        except FileNotFoundError as exc:
            raise RuntimeError("Claude Code introuvable : installe-le ou indique son chemin dans "
                               "ia.claude_code (config.yaml)") from exc
        try:
            out = json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeUnavailable(f"Réponse illisible de Claude Code : {(done.stderr or done.stdout)[:300]}") from exc
        if out.get("is_error"):
            message = str(out.get("result", ""))
            if out.get("api_error_status") in (401, 403) or "authenticat" in message.lower():
                raise ClaudeNotConnected("Claude Code n'est pas connecté à ton compte : lance `claude` dans un "
                                   "terminal, tape /login, puis relance le bot")
            raise ClaudeUnavailable(f"Claude Code : {message[:300]}")
        result = out.get("structured_output")
        if isinstance(result, dict):
            return result
        text = str(out.get("result", "")).strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        return json.loads(text)


def format_offer(job: dict) -> str:
    description = job.get("description") or ""
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION] + "\n[… suite de la description coupée]"
    lines = [
        f"Titre : {job['title']}",
        f"Entreprise : {job['company']}",
        f"Lieu : {job.get('location') or 'non précisé'}",
    ]
    if job.get("contract"):
        lines.append(f"Contrat : {job['contract']}")
    if job.get("salary_text"):
        lines.append(f"Salaire : {job['salary_text']}")
    lines.append(f"Source : {job['source']}")
    lines.append("Description :\n" + (description or
                 "(aucune description : seuls le titre, l'entreprise et le lieu sont connus)"))
    return "<offre>\n" + "\n".join(lines) + "\n</offre>"
