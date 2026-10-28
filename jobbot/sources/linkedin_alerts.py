"""Alertes emploi LinkedIn lues dans Gmail (IMAP, lecture seule).

Le bot ne se connecte jamais à LinkedIn : il lit seulement les emails que
LinkedIn t'envoie. Ces emails donnent le titre, l'entreprise et le lieu, pas
la description complète.
"""
import email
import html
import imaplib
import logging
import re
from datetime import date, timedelta
from email.utils import parsedate_to_datetime

from ..models import Job

log = logging.getLogger(__name__)

# Bloc type dans la partie texte de l'email :
#   Titre du poste
#   Entreprise
#   Lieu
#   Voir l’offre d’emploi : https://www.linkedin.com/comm/jobs/view/4480269549/?...
BLOCK = re.compile(
    r"^(?P<title>[^\n]+)\n(?P<company>[^\n]+)\n(?P<location>[^\n]+)\n"
    r"(?:Voir l[’']offre d[’']emploi|View job)\s*:\s*\S*linkedin\.com/comm/jobs/view/(?P<id>\d+)",
    re.MULTILINE,
)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fetch(address: str, app_password: str, senders: list[str], since_days: int,
          folder: str = "INBOX") -> list[Job]:
    since = date.today() - timedelta(days=since_days)
    since_imap = f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}"  # format IMAP, indépendant de la langue
    jobs: dict[str, Job] = {}
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(address, app_password)
        imap.select(folder, readonly=True)  # ne marque rien comme lu
        ids: set[bytes] = set()
        for sender in senders:
            _, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", since_imap)
            ids.update(data[0].split())
        for msg_id in ids:
            _, data = imap.fetch(msg_id, "(BODY.PEEK[])")
            msg = email.message_from_bytes(data[0][1])
            received = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
            for job in parse_alert(_plain_text(msg)):
                job.posted_at = received
                jobs.setdefault(job.uid, job)
    return list(jobs.values())


def parse_alert(text: str) -> list[Job]:
    return [
        Job(source="linkedin", source_id=m["id"], title=html.unescape(m["title"]).strip(),
            company=html.unescape(m["company"]).strip(), location=m["location"].strip(),
            url=f"https://www.linkedin.com/jobs/view/{m['id']}/")
        for m in BLOCK.finditer(text.replace("\r\n", "\n"))
    ]


def _plain_text(msg: email.message.Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""
