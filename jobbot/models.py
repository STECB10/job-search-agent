"""Représentation commune d'une offre, quelle que soit sa source."""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Job:
    source: str            # ashby, lever, greenhouse, france_travail, adzuna, linkedin
    source_id: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    description: str = ""
    salary_text: str = ""
    salary_min: float | None = None   # annuel
    salary_max: float | None = None   # annuel
    currency: str = ""
    contract: str = ""
    posted_at: datetime | None = None  # toujours avec fuseau horaire
    apply_email: str = ""

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def dedupe_key(self) -> str:
        """Même poste publié sur plusieurs sources = même clé."""
        return f"{normalize(self.company)}|{normalize(self.title)}"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())
