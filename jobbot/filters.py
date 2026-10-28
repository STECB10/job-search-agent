"""Filtres stricts appliqués avant l'IA : gratuits et instantanés."""
import re
from datetime import datetime, timedelta, timezone

from .models import Job

# Un CHU ou un hôpital universitaire n'est pas un employeur « enseignement ».
HOSPITAL = re.compile(r"h[ôo]pita|hospital|\bchu\b|\bnhs\b|clinique|clinic|spital", re.I)


class HardFilter:
    def __init__(self, tri: dict):
        self.targets = [re.compile(p, re.I) for p in tri["titres_cibles"]]
        self.excluded_titles = [re.compile(p, re.I) for p in tri["titres_exclus"]]
        self.excluded_employers = [re.compile(p, re.I) for p in tri["employeurs_exclus"]]
        self.accepted_places = [re.compile(p, re.I) for p in tri.get("lieux_acceptes", [])]
        self.excluded_places = [re.compile(p, re.I) for p in tri.get("lieux_exclus", [])]
        self.max_age = timedelta(days=tri["age_max_jours"])
        self.salary_floor = tri.get("salaire_min_annuel") or {}

    def reject_reason(self, job: Job, now: datetime | None = None) -> str | None:
        """Motif d'élimination, ou None si l'offre mérite d'être notée par l'IA."""
        now = now or datetime.now(timezone.utc)
        if not any(p.search(job.title) for p in self.targets):
            return "titre hors cible"
        for p in self.excluded_titles:
            if m := p.search(job.title):
                return f"titre exclu ({m.group(0)})"
        if not HOSPITAL.search(job.company):
            for p in self.excluded_employers:
                if m := p.search(job.company):
                    return f"employeur exclu ({m.group(0)})"
        if not any(p.search(job.location) for p in self.accepted_places):
            for p in self.excluded_places:
                if m := p.search(job.location):
                    return f"lieu hors zone ({m.group(0)})"
        if job.posted_at and now - job.posted_at > self.max_age:
            return "offre trop ancienne"
        floor = self.salary_floor.get(job.currency)
        if floor and job.salary_max and job.salary_max < floor:
            return f"salaire trop bas ({job.salary_max:,.0f} {job.currency})"
        return None
