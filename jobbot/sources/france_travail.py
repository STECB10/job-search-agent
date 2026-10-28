"""API officielle « Offres d'emploi v2 » de France Travail."""
import logging
import re
import time

from ..models import Job
from . import TIMEOUT, parse_iso, session

log = logging.getLogger(__name__)

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
DETAIL_URL = "https://candidat.francetravail.fr/offres/recherche/detail/{}"
ALLOWED_DAYS = (1, 3, 7, 14, 31)  # valeurs acceptées par « publieeDepuis »


class FranceTravail:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id, self.client_secret = client_id, client_secret
        self._token, self._expires = "", 0.0

    def _auth(self) -> str:
        if time.time() < self._expires - 60:
            return self._token
        r = session.post(TOKEN_URL, params={"realm": "/partenaire"}, timeout=TIMEOUT, data={
            "grant_type": "client_credentials", "client_id": self.client_id,
            "client_secret": self.client_secret, "scope": "api_offresdemploiv2 o2dsoffre"})
        r.raise_for_status()
        data = r.json()
        self._token, self._expires = data["access_token"], time.time() + data.get("expires_in", 1500)
        return self._token

    def fetch(self, mots_cles: list[str], max_age_days: int) -> list[Job]:
        days = next((d for d in ALLOWED_DAYS if d >= max_age_days), 31)
        jobs = []
        for mc in mots_cles:
            try:
                r = session.get(SEARCH_URL, timeout=TIMEOUT, headers={
                    "Authorization": f"Bearer {self._auth()}", "Accept": "application/json"},
                    # Mots séparés par des virgules = tous requis.
                    params={"motsCles": ",".join(mc.split()), "publieeDepuis": days,
                            "range": "0-149", "sort": 1})
                if r.status_code == 204:
                    continue
                r.raise_for_status()
                jobs += [_to_job(o) for o in r.json().get("resultats", [])]
            except Exception as exc:
                log.warning("France Travail « %s » : %s", mc, exc)
        return jobs


def _to_job(o: dict) -> Job:
    salary_text = (o.get("salaire") or {}).get("libelle", "")
    low, high = parse_salary(salary_text)
    contact = o.get("contact") or {}
    return Job(
        source="france_travail", source_id=o["id"], title=o.get("intitule", "").strip(),
        company=(o.get("entreprise") or {}).get("nom", "") or "Entreprise non précisée",
        location=(o.get("lieuTravail") or {}).get("libelle", ""),
        url=(o.get("origineOffre") or {}).get("urlOrigine") or DETAIL_URL.format(o["id"]),
        description=o.get("description", ""), salary_text=salary_text,
        salary_min=low, salary_max=high, currency="EUR" if high else "",
        contract=o.get("typeContratLibelle", ""), posted_at=parse_iso(o.get("dateCreation")),
        apply_email=contact.get("courriel", ""),
    )


def parse_salary(libelle: str) -> tuple[float | None, float | None]:
    """« Annuel de 40000.0 Euros à 50000.0 Euros sur 12 mois » -> (40000, 50000)."""
    amounts = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*Euros", libelle or "")]
    if not amounts:
        return None, None
    if libelle.lower().startswith("mensuel"):
        months = re.search(r"sur\s+(\d+(?:\.\d+)?)\s*mois", libelle)
        factor = float(months.group(1)) if months else 12.0
        amounts = [a * factor for a in amounts]
    elif libelle.lower().startswith("horaire"):
        amounts = [a * 1607 for a in amounts]  # durée légale annuelle
    return min(amounts), max(amounts)
