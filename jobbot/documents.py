"""Mise en forme : date, lettre en PDF, email de candidature."""
import re
import smtplib
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def long_date(day: date, english: bool = False) -> str:
    if english:
        return f"{day.day} {MONTHS[day.month - 1]} {day.year}"
    return f"{'1er' if day.day == 1 else day.day} {MOIS[day.month - 1]} {day.year}"


def letter_pdf(path: Path, identite: dict, letter: dict, company: str, recipient: str,
               english: bool, day: date) -> Path:
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10.5, leading=14.5,
                          alignment=TA_JUSTIFY, spaceAfter=8)
    small = ParagraphStyle("small", parent=body, fontSize=9.5, leading=12.5, spaceAfter=0,
                           alignment=TA_LEFT)
    name = ParagraphStyle("name", parent=small, fontName="Helvetica-Bold", fontSize=13, leading=16)
    right = ParagraphStyle("right", parent=small, alignment=TA_RIGHT)
    bold = ParagraphStyle("bold", parent=body, fontName="Helvetica-Bold", alignment=TA_LEFT)

    def p(text: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(escape(text), style)

    contact = " · ".join(filter(None, [identite.get("telephone"), identite.get("email")]))
    story = [p(identite["nom"], name), p(identite.get("titre", ""), small)]
    story += [p(line, small) for line in (identite.get("adresse"), contact) if line]
    story += [Spacer(1, 0.6 * cm), p(company, right)]
    if recipient:
        story.append(p(recipient, right))
    place = identite.get("ville", "")
    dated = f"{place}, {long_date(day, english)}" if english else f"{place}, le {long_date(day)}"
    story += [Spacer(1, 0.4 * cm), p(dated, right), Spacer(1, 0.6 * cm)]
    subject = re.sub(r"^(objet|subject)\s*:\s*", "", letter["objet"].strip(), flags=re.I)
    story.append(p(("Subject: " if english else "Objet : ") + subject, bold))
    for block in re.split(r"\n\s*\n|\n", letter["corps"].strip()):
        if block.strip():
            story.append(p(block.strip(), body))

    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm,
                      topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                      title=letter["objet"], author=identite["nom"]).build(story)
    return path


def email_body(letter: dict, identite: dict, english: bool) -> str:
    greeting, closing = ("Dear Hiring Team,", "Kind regards,") if english else \
        ("Madame, Monsieur,", "Bien cordialement,")
    signature = "\n".join(filter(None, [identite["nom"], identite.get("telephone"), identite.get("linkedin")]))
    return f"{greeting}\n\n{letter['message_court'].strip()}\n\n{closing}\n{signature}\n"


def send_email(sender: str, app_password: str, to: str, subject: str, body: str,
               attachments: list[Path]) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    for path in attachments:
        msg.add_attachment(path.read_bytes(), maintype="application", subtype="pdf", filename=path.name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(msg)
