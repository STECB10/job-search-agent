"""Agrégateur Adzuna : France, Suisse et pays anglophones.

Adzuna ne fournit qu'un extrait de la description : ces offres arrivent donc
souvent en « signalées », à ouvrir avant de demander une lettre.
"""
import logging

from ..models import Job
from . import TIMEOUT, html_to_text, parse_iso, session

log = logging.getLogger(__name__)

CURRENCIES = {"fr": "EUR", "ch": "CHF", "gb": "GBP", "us": "USD", "ca": "CAD", "au": "AUD", "nz": "NZD"}


def fetch(app_id: str, app_key: str, pays: dict[str, list[str]], max_age_days: int) -> list[Job]:
    jobs = []
    for country, queries in pays.items():
        for query in queries:
            try:
                r = session.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", timeout=TIMEOUT,
                                params={"app_id": app_id, "app_key": app_key, "what": query,
                                        "results_per_page": 50, "max_days_old": max_age_days,
                                        "sort_by": "date", "content-type": "application/json"})
                r.raise_for_status()
                jobs += [_to_job(country, o) for o in r.json().get("results", [])]
            except Exception as exc:
                log.warning("Adzuna %s « %s » : %s", country, query, exc)
    return jobs


def _to_job(country: str, o: dict) -> Job:
    job = Job(
        source="adzuna", source_id=f"{country}-{o['id']}", title=html_to_text(o.get("title", "")),
        company=(o.get("company") or {}).get("display_name", ""),
        location=(o.get("location") or {}).get("display_name", ""),
        url=o.get("redirect_url", ""), description=html_to_text(o.get("description", "")),
        contract=" ".join(filter(None, [o.get("contract_type"), o.get("contract_time")])),
        posted_at=parse_iso(o.get("created")),
    )
    if o.get("salary_max") and str(o.get("salary_is_predicted")) != "1":
        job.salary_min, job.salary_max = o.get("salary_min"), o["salary_max"]
        job.currency = CURRENCIES.get(country, "")
        job.salary_text = f"{job.salary_min:,.0f} – {job.salary_max:,.0f} {job.currency} / an"
    return job
