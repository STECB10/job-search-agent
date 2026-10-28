"""Outils partagés par les sources d'offres."""
import html
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
})
TIMEOUT = 20


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    # Greenhouse renvoie du HTML lui-même échappé (&lt;p&gt;).
    soup = BeautifulSoup(html.unescape(raw), "html.parser")
    text = soup.get_text("\n").replace("\xa0", " ")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
