"""Parcours Telegram simulé, sans réseau : offre -> modification -> validation."""
import asyncio
import json
from types import SimpleNamespace

from telegram.ext import Application

from jobbot import config
from jobbot.db import Store
from jobbot.pipeline import Pipeline
from jobbot.telegram_bot import Bot
from test_jobbot import CFG, CFG_NO_EN, FakeClaude, job

CHAT = 42


class FakeBotAPI:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))


class FakeMessage:
    def __init__(self, api, text=""):
        self.api, self.text, self.replies = api, text, []

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    async def reply_document(self, f, filename=None, caption=None):
        self.replies.append(f"document:{filename}")

    def get_bot(self):
        return self.api


class FakeQuery:
    def __init__(self, data, message):
        self.data, self.message, self.answers, self.markups = data, message, [], []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, markup):
        self.markups.append(markup)


def update(chat_id=CHAT, query=None, message=None):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), callback_query=query, message=message)


def test_full_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "PROFIL", tmp_path / "profil")
    store = Store(tmp_path / "t.sqlite")
    pipeline = Pipeline(CFG_NO_EN, store, FakeClaude(90))
    bot, api = Bot(pipeline, str(CHAT)), FakeBotAPI()
    bot.send_delay = 0

    async def scenario():
        pipeline.ingest([job(), job("Data Scientist NLP", "Nabla")])
        pipeline.triage()
        await bot.push_pending(api)
        assert len(store.by_status("propose")) == 2
        assert any(markup and "Valider" in str(markup.to_dict()) for _, _, markup in api.sent)
        first, second = store.by_status("propose")

        # Un inconnu ne peut rien faire
        await bot.on_button(update(999, FakeQuery(f"ok:{first['id']}", FakeMessage(api))), None)
        assert store.get(first["id"])["status"] == "propose"

        # ✏️ Modifier puis consigne
        await bot.on_button(update(query=FakeQuery(f"mod:{first['id']}", FakeMessage(api))), None)
        await bot.on_text(update(message=FakeMessage(api, "plus court")), None)
        assert "plus court" in store.get(first["id"])["letter"]["corps"]

        # ✅ Valider : le bot demande quel CV fournir
        message = FakeMessage(api)
        await bot.on_button(update(query=FakeQuery(f"ok:{first['id']}", message)), None)
        assert "Veux-tu aussi ton CV" in message.replies[0]
        assert store.get(first["id"])["status"] == "propose"

        # Pas de CV anglais configuré : on le dit, rien n'est envoyé
        message = FakeMessage(api)
        await bot.on_button(update(query=FakeQuery(f"cv:{first['id']}:en", message)), None)
        assert "pas de CV anglais" in message.replies[0]
        assert store.get(first["id"])["status"] == "propose"

        # CV français : lettre PDF + CV + kit, puis « J'ai postulé »
        message = FakeMessage(api)
        await bot.on_button(update(query=FakeQuery(f"cv:{first['id']}:fr", message)), None)
        assert message.replies[:2] == ["document:Lettre_de_motivation_Steven_Crombecque.pdf",
                                       "document:CV_Steven_Crombecque_2026.pdf"]
        assert store.get(first["id"])["status"] == "valide"
        assert len(list((tmp_path / "profil" / "lettres_validees").glob("*.txt"))) == 1
        await bot.on_button(update(query=FakeQuery(f"done:{first['id']}", FakeMessage(api))), None)
        assert store.get(first["id"])["status"] == "postule"

        # ❌ Ignorer avec un motif, réutilisé ensuite par le tri
        await bot.on_button(update(query=FakeQuery(f"no:{second['id']}", FakeMessage(api))), None)
        await bot.on_button(update(query=FakeQuery(f"why:{second['id']}:emp", FakeMessage(api))), None)
        assert store.recent_rejections()[0]["reason"] == "Employeur"

    asyncio.run(scenario())


def test_signal_without_description_asks_for_text(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    pipeline = Pipeline(CFG, store, FakeClaude(65))
    bot, api = Bot(pipeline, str(CHAT)), FakeBotAPI()

    async def scenario():
        pipeline.ingest([job()])
        pipeline.triage()
        row = store.by_status("a_signaler")[0]
        message = FakeMessage(api)
        await bot.on_button(update(query=FakeQuery(f"write:{row['id']}", message)), None)
        assert "colle-le ici" in message.replies[0]
        await bot.on_text(update(message=FakeMessage(api, "Texte complet de l'offre. " * 30)), None)
        assert store.get(row["id"])["status"] == "propose"
        assert store.get(row["id"])["letter"] is not None

    asyncio.run(scenario())


def test_application_builds_with_job_queue():
    app = Application.builder().token("123:ABC").build()
    assert app.job_queue is not None


def test_cv_command(tmp_path):
    bot = Bot(Pipeline(CFG, Store(tmp_path / "t.sqlite")), str(CHAT))

    async def scenario():
        both, only_en, stranger = FakeMessage(None), FakeMessage(None), FakeMessage(None)
        await bot.cv(update(message=both), SimpleNamespace(args=[]))
        await bot.cv(update(message=only_en), SimpleNamespace(args=["en"]))
        await bot.cv(update(999, message=stranger), SimpleNamespace(args=[]))
        assert both.replies == ["document:CV_Steven_Crombecque_2026.pdf", "document:CV_Steven_Crombecque_2026_EN.pdf"]
        assert only_en.replies == ["document:CV_Steven_Crombecque_2026_EN.pdf"]
        assert stranger.replies == []  # le bot ne répond qu'à toi

    asyncio.run(scenario())


def test_prefill_button_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    store = Store(tmp_path / "t.sqlite")
    bot = Bot(Pipeline(CFG, store), str(CHAT))
    ashby = {"id": 1, "url": "https://jobs.ashbyhq.com/inato/6059da1d-8e0e-4d3b-81f6-2dab6bc72cd7"}
    linkedin = {"id": 2, "url": "https://www.linkedin.com/jobs/view/4480269549/"}
    assert "pre:1:en" in str(bot.kb_applied(ashby, "en").to_dict())
    assert "pre:" not in str(bot.kb_applied(linkedin).to_dict())

    class FakeProcess:
        stdout = SimpleNamespace(readline=lambda: _value(json.dumps({
            "ok": True, "rempli": ["CV", "Name"], "a_verifier": ["Bilingual? → Yes"],
            "a_toi": ["Work authorization"], "consentements": ["Privacy notice"]}).encode()))

    async def fake_exec(*args, **kw):
        assert args[1:4] == ("-m", "jobbot.prefill", "1")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    store.conn.execute("INSERT INTO jobs (id, uid, url, status) VALUES (1, 'ashby:x', ?, 'valide')", (ashby["url"],))
    message = FakeMessage(None)
    asyncio.run(bot.on_button(update(query=FakeQuery("pre:1:fr", message)), None))
    report = message.replies[-1]
    assert "2 champs remplis" in report and "Work authorization" in report and "Privacy notice" in report


async def _value(v):
    return v
