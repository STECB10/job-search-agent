from datetime import date, datetime, timedelta, timezone

import pytest

from jobbot import config
from jobbot.db import Store
from jobbot.documents import email_body, letter_pdf, long_date
from jobbot.filters import HardFilter
from jobbot.models import Job
from jobbot.pipeline import Pipeline
from jobbot.sources.france_travail import parse_salary
from jobbot.sources.linkedin_alerts import parse_alert

CFG = config.load_config()
CFG_NO_EN = {**CFG, "cv": {**CFG["cv"], "en": ""}}  # pour tester l'absence de CV anglais
NOW = datetime.now(timezone.utc)

# Extrait réel (raccourci) de la partie texte d'un email d'alerte LinkedIn.
ALERT = """Offres d’emploi similaires à Ingénieur de Recherche en IA H/F chez CEA https://www.linkedin.com/comm/jobs/view/4480265750?refId=x

Ingénieur de Recherche en IA génératives (images et son) H/F
CEA
Le Barp
Voir l’offre d’emploi : https://www.linkedin.com/comm/jobs/view/4480269549/?trackingId=K1K5&refId=mvRR

---------------------------------------------------------

Responsable Etudes et Conception d&apos;Algorithmes de Traitement d&apos;Image et IA F/H
MBDA
Le Plessis-Robinson
Voir l’offre d’emploi : https://www.linkedin.com/comm/jobs/view/4472798905/?trackingId=ggcz

---------------------------------------------------------

Voir toutes les offres d’emploi https://www.linkedin.com/comm/jobs/search-results/?keywords=x
"""


def job(title="Data Scientist", company="Owkin", location="Paris", **kw) -> Job:
    return Job(source="ashby", source_id=kw.pop("source_id", title), title=title, company=company,
               location=location, posted_at=kw.pop("posted_at", NOW), **kw)


def test_linkedin_alert_parsing():
    jobs = parse_alert(ALERT)
    assert [(j.source_id, j.company, j.location) for j in jobs] == [
        ("4480269549", "CEA", "Le Barp"), ("4472798905", "MBDA", "Le Plessis-Robinson")]
    assert jobs[1].title == "Responsable Etudes et Conception d'Algorithmes de Traitement d'Image et IA F/H"
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4480269549/"


@pytest.mark.parametrize("candidate, reason", [
    (job(), None),
    (job("Biostatisticien H/F", "CHU de Rennes - Centre Hospitalier Universitaire"), None),
    (job("Ingénieur IA - Agents LLM", "Medical Startup", "Grenoble"), None),
    (job("International Data Scientist"), None),
    (job("Office Manager"), "titre hors cible"),
    (job("Stage - Data Scientist"), "titre exclu"),
    (job("Data Scientist Intern"), "titre exclu"),
    (job("Staff Data Scientist"), "titre exclu"),
    (job("Member of Technical Staff, North Modelling (Evals)", "Cohere", "London"), None),
    (job("Research Scientist/Engineer (Evaluations)", "Apollo Research", "London & San Francisco"), None),
    (job("Red Team Engineer, Safeguards", "Anthropic", "San Francisco, CA"), None),
    (job("Ingénieur évaluation des modèles d'IA"), None),
    (job("LLM Evaluation Expert (Contract)"), "titre exclu"),
    (job("AI Security Engineer"), "titre exclu"),
    (job("Data Center Technician"), "titre exclu"),
    (job("Ingénieur de recherche IA", "Université Grenoble Alpes"), "employeur exclu"),
    (job("Data Scientist", location="Berlin (télétravail possible)"), "lieu hors zone"),
    (job("Data Scientist", location="Anywhere in France, Belgium, Spain"), None),
    (job(posted_at=NOW - timedelta(days=30)), "offre trop ancienne"),
    (job(salary_max=30000, currency="EUR"), "salaire trop bas"),
    (job(salary_max=60000, currency="EUR"), None),
])
def test_hard_filter(candidate, reason):
    got = HardFilter(CFG["tri"]).reject_reason(candidate, NOW)
    assert (got.split(" (")[0] if got else None) == reason


def test_france_travail_salary():
    assert parse_salary("Annuel de 40000.0 Euros à 50000.0 Euros sur 12.0 mois") == (40000, 50000)
    assert parse_salary("Mensuel de 2500.0 Euros sur 13 mois") == (32500, 32500)
    assert parse_salary("Selon profil") == (None, None)


class FakeClaude:
    def __init__(self, score):
        self.value = score

    def score(self, row, rejections):
        return {"score": self.value, "resume": "r", "points_forts": ["a"], "points_faibles": [],
                "type_employeur": "startup", "langue": "fr", "destinataire": "",
                "email_candidature": "rh@inventee.fr"}

    def write_letter(self, row, today, current=None, instruction=""):
        return {"objet": "Candidature", "corps": f"Madame, Monsieur,\n\n{instruction or 'Texte'}\n\nSteven Crombecque",
                "message_court": "Court."}


@pytest.mark.parametrize("score, status", [(90, "a_envoyer"), (65, "a_signaler"), (40, "ecarte")])
def test_pipeline_routes_by_score(tmp_path, score, status):
    store = Store(tmp_path / "t.sqlite")
    pipeline = Pipeline(CFG, store, FakeClaude(score))
    assert pipeline.ingest([job(), job("Office Manager"), job()]) == 1  # doublon et hors cible écartés
    assert pipeline.triage() == 1
    row = store.by_status(status)[0]
    assert row["score"] == score
    assert row["apply_email"] == ""  # adresse absente du texte de l'offre : refusée
    assert (row["letter"] is not None) == (status == "a_envoyer")


def test_linkedin_offer_gets_description_from_other_source(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    pipeline = Pipeline(CFG, store)
    pipeline.ingest([Job(source="linkedin", source_id="1", title="Data Scientist", company="Owkin",
                         posted_at=NOW)])
    pipeline.ingest([job(description="Mission détaillée. " * 50)])
    assert len(store.by_status("a_noter")[0]["description"]) > 500


def test_letter_revision_and_replacement(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    pipeline = Pipeline(CFG, store, FakeClaude(90))
    pipeline.ingest([job()])
    pipeline.triage()
    job_id = store.by_status("a_envoyer")[0]["id"]
    assert "plus court" in pipeline.revise_letter(job_id, "plus court")["letter"]["corps"]
    assert pipeline.replace_letter(job_id, "Ma version")["letter"]["corps"] == "Ma version"


def test_letter_pdf_and_email(tmp_path):
    letter = {"objet": "Objet : Candidature au poste de Data Scientist",
              "corps": "Madame, Monsieur,\n\nLe cœur du poste, à 45 000 € : « parfait ».\n\nSteven Crombecque",
              "message_court": "Je vous écris pour le poste."}
    pdf = letter_pdf(tmp_path / "l.pdf", CFG["identite"], letter, "Owkin", "", False, date(2026, 11, 1))
    assert pdf.read_bytes().startswith(b"%PDF") and pdf.stat().st_size > 1500
    assert long_date(date(2026, 11, 1)) == "1er novembre 2026"
    assert email_body(letter, CFG["identite"], english=True).startswith("Dear Hiring Team,")


def test_claude_code_engine(tmp_path, monkeypatch):
    """Le moteur « abonnement » appelle `claude -p` et lit sa réponse JSON."""
    import json
    import stat
    from jobbot.llm import Claude, ClaudeNotConnected
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\ncat > /dev/null\n'
                    'case "$*" in *--json-schema*) ;; *) exit 1;; esac\n'
                    'if [ -n "$ANTHROPIC_API_KEY" ]; then echo \'{"is_error": true, "result": "cle API utilisee"}\'; exit 0; fi\n'
                    'echo \'{"is_error": false, "structured_output": {"score": 120, "resume": "ok", '
                    '"points_forts": [], "points_faibles": [], "type_employeur": "startup", "langue": "fr", '
                    '"email_candidature": "", "destinataire": ""}}\'\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    claude = Claude({**CFG["ia"], "moteur": "abonnement", "claude_code": str(fake)}, "")
    row = {"title": "Data Scientist", "company": "Owkin", "source": "ashby", "description": "x"}
    assert claude.score(row, [])["score"] == 100  # borné à 100, et la clé API n'est pas transmise

    fake.write_text('#!/bin/sh\necho \'{"is_error": true, "api_error_status": 401, "result": "revoked"}\'\n')
    with pytest.raises(ClaudeNotConnected):
        claude.score(row, [])


def test_validate_with_or_without_cv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "PROFIL", tmp_path / "profil")
    store = Store(tmp_path / "t.sqlite")
    pipeline = Pipeline(CFG_NO_EN, store, FakeClaude(90))
    pipeline.ingest([job(), job("Data Scientist NLP", "Nabla")])
    pipeline.triage()
    first, second = store.by_status("a_envoyer")
    assert Pipeline(CFG, store).cv_path("en").name == "CV_Steven_Crombecque_2026_EN.pdf"
    with pytest.raises(ValueError):
        pipeline.validate(first["id"], "en")  # pas de CV anglais configuré
    assert pipeline.validate(first["id"], None)[2] is None
    assert pipeline.validate(second["id"], "fr")[2].name == "CV_Steven_Crombecque_2026.pdf"


def test_prefill_helpers():
    from jobbot.prefill import application_url, standard_answer, supported
    assert supported("https://jobs.lever.co/ekimetrics/967bbddc-8921-4e58-b122-2fc4e322e44f") == "lever"
    assert supported("https://www.linkedin.com/jobs/view/1/") is None
    assert application_url("https://jobs.ashbyhq.com/inato/6059da1d-8e0e-4d3b-81f6-2dab6bc72cd7").endswith("/application")
    facts = {"prenom": "Steven", "nom": "Crombecque", "email": "s@x.fr", "pays": "France"}
    files = {"cv": "cv.pdf", "lettre": "lm.pdf"}
    assert standard_answer({"kind": "texte", "label": "First Name*"}, facts, files) == ("Steven", "rempli")
    assert standard_answer({"kind": "texte", "label": "Preferred First Name"}, facts, files)[1] == "ignore"
    assert standard_answer({"kind": "fichier", "label": "Attach", "hint": "cover_letter"}, facts, files)[0] == "lm.pdf"
    assert standard_answer({"kind": "fichier", "label": "Attach", "hint": "resume"}, facts, files)[0] == "cv.pdf"
    assert standard_answer({"kind": "fichier", "label": "Autofill from resume", "hint": ""}, facts, files) is None
    assert standard_answer({"kind": "texte", "label": "Salary expectations"}, facts, files) is None
