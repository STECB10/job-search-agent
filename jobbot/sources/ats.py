"""Pages carrières publiques : Greenhouse, Lever, Ashby (aucune clé nécessaire)."""
import logging
from datetime import datetime, timezone

from ..models import Job
from . import TIMEOUT, html_to_text, parse_iso, session

log = logging.getLogger(__name__)


def fetch(entreprises: list[dict]) -> list[Job]:
    jobs: list[Job] = []
    for e in entreprises:
        try:
            jobs += FETCHERS[e["ats"]](e["id"], e["nom"])
        except Exception as exc:  # une page en panne ne doit pas bloquer les autres
            log.warning("ATS %s (%s/%s) : %s", e["nom"], e["ats"], e["id"], exc)
    return jobs


def _greenhouse(board: str, company: str) -> list[Job]:
    r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                    params={"content": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    return [
        Job(source="greenhouse", source_id=f"{board}-{j['id']}", title=j["title"].strip(),
            company=company, location=(j.get("location") or {}).get("name", ""),
            url=j["absolute_url"], description=html_to_text(j.get("content", "")),
            posted_at=parse_iso(j.get("first_published") or j.get("updated_at")))
        for j in r.json().get("jobs", [])
    ]


def _lever(site: str, company: str) -> list[Job]:
    r = session.get(f"https://api.lever.co/v0/postings/{site}", params={"mode": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain", "")]
        parts += [f"{s.get('text', '')}\n{html_to_text(s.get('content', ''))}" for s in j.get("lists", [])]
        parts.append(j.get("additionalPlain", ""))
        job = Job(source="lever", source_id=j["id"], title=j["text"].strip(), company=company,
                  location=cats.get("location", ""), url=j["hostedUrl"],
                  description="\n\n".join(p.strip() for p in parts if p and p.strip()),
                  contract=cats.get("commitment", ""),
                  posted_at=datetime.fromtimestamp(j["createdAt"] / 1000, tz=timezone.utc))
        salary = j.get("salaryRange") or {}
        if salary.get("interval") == "per-year-salary":
            job.salary_min, job.salary_max = salary.get("min"), salary.get("max")
            job.currency = salary.get("currency", "")
        jobs.append(job)
    return jobs


def _ashby(board: str, company: str) -> list[Job]:
    r = session.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                    params={"includeCompensation": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        if not j.get("isListed", True):
            continue
        location = (j.get("location") or "").strip()
        if j.get("isRemote"):
            location = f"{location} (télétravail possible)".strip()
        job = Job(source="ashby", source_id=j["id"], title=j["title"].strip(), company=company,
                  location=location, url=j["jobUrl"], description=j.get("descriptionPlain", ""),
                  contract=j.get("employmentType", ""), posted_at=parse_iso(j.get("publishedAt")))
        comp = j.get("compensation") or {}
        for c in comp.get("summaryComponents") or []:
            if c.get("compensationType") == "Salary" and c.get("interval") == "1 YEAR":
                job.salary_min, job.salary_max = c.get("minValue"), c.get("maxValue")
                job.currency = c.get("currencyCode", "")
        job.salary_text = comp.get("scrapeableCompensationSalarySummary") or ""
        jobs.append(job)
    return jobs


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}
