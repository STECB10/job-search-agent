"""Interface Telegram : offres, lettres, boutons Valider / Modifier / Ignorer."""
import asyncio
import html
import json
import logging
import sys
from datetime import datetime, timezone
from functools import wraps

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import config
from .pipeline import Pipeline
from .prefill import supported as prefill_supported

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000      # un message Telegram fait 4 096 caractères au plus
SHORT_DESCRIPTION = 400    # en dessous, on demande le texte de l'offre avant d'écrire
FULL_LETTER = 700          # une réponse plus longue est une lettre corrigée, pas une consigne
REASONS = {"dom": "Pas mon domaine", "emp": "Employeur", "lieu": "Lieu",
           "niv": "Niveau", "sal": "Salaire", "aut": "Autre"}
SOURCES = {"greenhouse": "page carrière (Greenhouse)", "lever": "page carrière (Lever)",
           "ashby": "page carrière (Ashby)", "france_travail": "France Travail",
           "adzuna": "Adzuna", "linkedin": "alerte LinkedIn"}
STATUS_LABELS = {"a_noter": "en attente de tri", "filtre": "éliminées par les filtres",
                 "ecarte": "jugées hors cible par l'IA", "signale": "signalées", "propose": "lettre proposée",
                 "valide": "validées, à envoyer", "postule": "candidatures envoyées",
                 "ignore": "ignorées par toi", "erreur": "en erreur"}
HELP = (
    "Je surveille les offres et je t'envoie celles qui te correspondent.\n\n"
    "🟢🟡 Bonne note : la lettre est déjà écrite. ✅ Valider, ✏️ Modifier ou ❌ Ignorer.\n"
    "🟠 Note moyenne : ✍️ Rédiger si l'offre t'intéresse.\n"
    "✏️ Modifier : réponds par une consigne (« plus court », « parle de Medical Agent ») "
    "ou colle directement ta version corrigée.\n\n"
    "/scan : vérifier maintenant\n/stats : bilan\n/cv : recevoir tes CV (/cv fr ou /cv en pour un seul)\n"
    "/pause et /reprendre : couper ou relancer la veille"
)


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def owner_only(handler):
    """Le bot ne répond qu'à toi."""
    @wraps(handler)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if self.chat_id is not None and chat is not None and chat.id == self.chat_id:
            return await handler(self, update, context)
    return wrapper


class Bot:
    def __init__(self, pipeline: Pipeline, chat_id: str):
        self.p = pipeline
        self.chat_id = int(chat_id) if chat_id else None
        self.scan_lock = asyncio.Lock()
        self.pending: dict | None = None  # {"mode": "edit" | "description", "id": …}
        self.send_delay = 1.0  # Telegram limite le rythme des messages
        self.prefills: dict[int, asyncio.subprocess.Process] = {}

    # ─────────────── Mise en forme ───────────────

    def card(self, row: dict) -> str:
        a = row.get("analysis") or {}
        score = row.get("score") or 0
        icon = "🟢" if score >= 85 else "🟡" if score >= self.p.cfg["tri"]["seuil_lettre"] else "🟠"
        lines = [f"{icon} <b>{score}/100 · {esc(row['title'])}</b>", f"🏢 {esc(row['company'])}"]
        details = " · ".join(filter(None, [row.get("location"), row.get("contract"), row.get("salary_text")]))
        if details:
            lines.append(f"📍 {esc(details)}")
        lines.append(f"🔎 {SOURCES.get(row['source'], row['source'])}")
        if a.get("resume"):
            lines += ["", f"<i>{esc(a['resume'])}</i>"]
        lines += [f"✅ {esc(x)}" for x in a.get("points_forts", [])]
        lines += [f"⚠️ {esc(x)}" for x in a.get("points_faibles", [])]
        if self.p.sends_by_email(row):
            lines += ["", f"📧 Candidature par email à <b>{esc(row['apply_email'])}</b> : "
                          "« Valider » enverra ton CV et cette lettre à cette adresse."]
        return "\n".join(lines)

    @staticmethod
    def with_link(row: dict, rows: list, label: str = "🔗 Voir l'offre") -> InlineKeyboardMarkup:
        if (row.get("url") or "").startswith("http"):
            rows = rows + [[Btn(label, url=row["url"])]]
        return InlineKeyboardMarkup(rows)

    def kb_letter(self, row: dict) -> InlineKeyboardMarkup:
        i = row["id"]
        return self.with_link(row, [[Btn("✅ Valider", callback_data=f"ok:{i}"),
                                     Btn("✏️ Modifier", callback_data=f"mod:{i}"),
                                     Btn("❌ Ignorer", callback_data=f"no:{i}")]])

    def kb_signal(self, row: dict) -> InlineKeyboardMarkup:
        i = row["id"]
        return self.with_link(row, [[Btn("✍️ Rédiger la lettre", callback_data=f"write:{i}"),
                                     Btn("❌ Ignorer", callback_data=f"no:{i}")]])

    @staticmethod
    def kb_reasons(job_id: int) -> InlineKeyboardMarkup:
        buttons = [Btn(label, callback_data=f"why:{job_id}:{code}") for code, label in REASONS.items()]
        return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])

    def kb_applied(self, row: dict, cv_lang: str = "fr") -> InlineKeyboardMarkup:
        rows = [[Btn("✔️ J'ai postulé", callback_data=f"done:{row['id']}")]]
        if prefill_supported(row.get("url", "")):
            rows.insert(0, [Btn("🖥️ Pré-remplir sur mon Mac", callback_data=f"pre:{row['id']}:{cv_lang or 'non'}")])
        return self.with_link(row, rows, "🔗 Postuler")

    # ─────────────── Envois ───────────────

    async def send_letter(self, bot, row: dict) -> None:
        letter = row["letter"]
        text = f"✉️ {letter['objet']}\n\n{letter['corps']}"
        chunks = [text[i:i + TELEGRAM_LIMIT] for i in range(0, len(text), TELEGRAM_LIMIT)]
        for chunk in chunks[:-1]:
            await bot.send_message(self.chat_id, chunk)
        await bot.send_message(self.chat_id, chunks[-1], reply_markup=self.kb_letter(row))

    async def send_offer(self, bot, row: dict, with_letter: bool) -> None:
        await bot.send_message(self.chat_id, self.card(row), parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True,
                               reply_markup=None if with_letter else self.kb_signal(row))
        if with_letter:
            await self.send_letter(bot, row)

    async def push_pending(self, bot) -> None:
        for status, with_letter, new_status in (("a_envoyer", True, "propose"), ("a_signaler", False, "signale")):
            for row in self.p.store.by_status(status):
                try:
                    await self.send_offer(bot, row, with_letter)
                    self.p.store.set_status(row["id"], new_status)
                except Exception:
                    log.exception("Envoi Telegram impossible pour %s", row["uid"])
                await asyncio.sleep(self.send_delay)

    async def run_scan(self, bot, force: bool = False) -> dict | None:
        if self.scan_lock.locked():
            return None
        async with self.scan_lock:
            result = await asyncio.to_thread(self.p.run_cycle, force)
            await self.push_pending(bot)
            self.p.store.set_state("last_scan", datetime.now(timezone.utc).isoformat())
            return result

    async def tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.chat_id is None or self.p.store.get_state("pause") == "1":
            return
        try:
            await self.run_scan(context.bot)
        except Exception:
            log.exception("Passage en échec")

    # ─────────────── Commandes ───────────────

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        log.info("/start reçu de la conversation %s", chat_id)
        if self.chat_id is None:
            await update.message.reply_text(
                f"Ton identifiant de conversation est {chat_id}.\n"
                f"Mets TELEGRAM_CHAT_ID={chat_id} dans le fichier .env, puis relance le bot.")
        elif chat_id == self.chat_id:
            first_name = self.p.cfg["identite"]["nom"].split()[0]
            await update.message.reply_text(f"Bonjour {first_name} 👋\n\n" + HELP)

    @owner_only
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP)

    @owner_only
    async def scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🔎 Je vérifie toutes les sources…")
        result = await self.run_scan(context.bot, force=True)
        if result is None:
            await update.message.reply_text("Une vérification est déjà en cours.")
        else:
            await update.message.reply_text(
                f"Terminé : {result['nouvelles']} nouvelles offres retenues, {result['notees']} notées par l'IA.")

    @owner_only
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        counts = self.p.store.counts()
        lines = [f"• {STATUS_LABELS.get(k, k)} : {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
        last = self.p.store.get_state("last_scan")
        if last:
            lines.append(f"\nDernière vérification : {datetime.fromisoformat(last).astimezone():%d/%m à %H:%M}")
        if self.p.store.get_state("pause") == "1":
            lines.append("⏸ Veille en pause (/reprendre)")
        await update.message.reply_text("📊 Bilan\n" + "\n".join(lines or ["Aucune offre pour l'instant."]))

    @owner_only
    async def cv(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/cv envoie les deux CV ; /cv fr ou /cv en un seul."""
        wanted = [a.lower() for a in (context.args or []) if a.lower() in ("fr", "en")] or ["fr", "en"]
        labels = {"fr": "🇫🇷 CV français", "en": "🇬🇧 CV anglais"}
        sent = 0
        for lang in wanted:
            path = self.p.cv_path(lang)
            if path is None:
                await update.message.reply_text(f"{labels[lang]} : introuvable. Vérifie cv.{lang} dans config.yaml.")
                continue
            with open(path, "rb") as f:
                await update.message.reply_document(f, filename=path.name, caption=labels[lang])
            sent += 1
        if not sent and len(wanted) == 2:
            await update.message.reply_text("Aucun CV configuré.")

    @owner_only
    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.p.store.set_state("pause", "1")
        await update.message.reply_text("⏸ Veille en pause. /reprendre pour la relancer.")

    @owner_only
    async def resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.p.store.set_state("pause", "0")
        await update.message.reply_text("▶️ Veille relancée.")

    # ─────────────── Boutons ───────────────

    @owner_only
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        action, job_id, *extra = q.data.split(":")
        row = self.p.store.get(int(job_id))
        if row is None:
            await q.answer("Offre introuvable")
            return
        handler = {"ok": self._validate, "cv": self._cv, "pre": self._prefill, "mod": self._modify, "no": self._ignore, "why": self._reason,
                   "write": self._write, "done": self._applied}[action]
        await handler(q, context, row, *extra)

    def kb_cv(self, row: dict) -> InlineKeyboardMarkup:
        i = row["id"]
        english = (row.get("analysis") or {}).get("langue") == "en"
        fr, en = ("🇫🇷 CV français", "🇬🇧 CV anglais ⭐") if english else ("🇫🇷 CV français ⭐", "🇬🇧 CV anglais")
        return InlineKeyboardMarkup([[Btn(fr, callback_data=f"cv:{i}:fr"), Btn(en, callback_data=f"cv:{i}:en")],
                                     [Btn("Sans CV", callback_data=f"cv:{i}:non")]])

    async def _validate(self, q, context, row) -> None:
        """✅ : la lettre est validée, puis le bot demande quel CV fournir."""
        if row["status"] in ("valide", "postule"):
            await q.answer("Déjà validée")
            return
        await q.answer("Lettre validée")
        await q.edit_message_reply_markup(None)
        question = "✅ Lettre validée. Veux-tu aussi ton CV ? (⭐ = langue de la lettre)"
        if self.p.sends_by_email(row):
            question += f"\n📧 Ensuite, j'envoie la candidature à {row['apply_email']}."
        await q.message.reply_text(question, reply_markup=self.kb_cv(row))

    async def _cv(self, q, context, row, lang: str) -> None:
        if row["status"] in ("valide", "postule"):
            await q.answer("Déjà fait")
            return
        cv_lang = None if lang == "non" else lang
        if cv_lang and self.p.cv_path(cv_lang) is None:
            await q.answer("CV introuvable")
            await q.message.reply_text(
                f"Je n'ai pas de CV {'anglais' if cv_lang == 'en' else 'français'} en PDF. Dépose-le dans "
                f"le dossier profil/ et indique son nom dans cv.{cv_lang} de config.yaml. "
                "En attendant, choisis une autre option ci-dessus.")
            return
        await q.answer("Je prépare ta candidature…")
        await q.edit_message_reply_markup(None)
        try:
            mode, pdf, cv = await asyncio.to_thread(self.p.validate, row["id"], cv_lang)
        except Exception as exc:
            log.exception("Validation en échec")
            await q.message.reply_text(f"❗ Échec : {exc}\nLa candidature n'a pas été envoyée.",
                                       reply_markup=self.kb_cv(row))
            return
        if mode == "email":
            joined = "lettre + CV" if cv else "lettre seule"
            await q.message.reply_text(f"📧 Candidature envoyée à {row['apply_email']} ({joined}). Bonne chance !")
            return
        for document in (pdf, cv):
            if document:
                with open(document, "rb") as f:
                    await q.message.reply_document(f, filename=document.name)
        await q.message.reply_text(
            "📝 Message pour le recruteur (formulaire ou message LinkedIn) :\n\n"
            f"{row['letter']['message_court']}\n\n"
            "Postule avec le lien ci-dessous, puis appuie sur « J'ai postulé ».",
            reply_markup=self.kb_applied(row, cv_lang))

    async def _prefill(self, q, context, row, lang: str) -> None:
        """Ouvre le formulaire dans Chrome sur le Mac et le remplit ; l'utilisateur envoie lui-même."""
        await q.answer("J'ouvre le formulaire sur ton Mac…")
        await q.message.reply_text("🖥️ J'ouvre le formulaire sur ton Mac et je le remplis (environ 1 minute)…")
        log_file = open(config.DATA / "prefill.log", "a")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "jobbot.prefill", str(row["id"]), lang, cwd=config.ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=log_file)
        self.prefills[row["id"]] = process  # la fenêtre reste ouverte après le remplissage
        try:
            report = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=600))
        except Exception as exc:
            await q.message.reply_text(f"❗ Pré-remplissage impossible : {exc or 'pas de réponse'}")
            return
        if not report.get("ok"):
            await q.message.reply_text(f"❗ Pré-remplissage impossible : {report.get('erreur')}")
            return
        lines = [f"🖥️ Formulaire ouvert sur ton Mac : {len(report['rempli'])} champs remplis."]
        sections = [("a_verifier", "👀 À vérifier"), ("a_toi", "✍️ À remplir toi-même"),
                    ("consentements", "☑️ À cocher toi-même si tu es d'accord")]
        for key, title in sections:
            items = [x for x in report.get(key, []) if x]
            if items:
                lines += ["", title + " :"] + [f"• {x[:160]}" for x in items]
        lines += ["", "Relis tout dans la fenêtre, clique sur « Submit », puis appuie ici sur « J'ai postulé »."]
        await q.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(
            [[Btn("✔️ J'ai postulé", callback_data=f"done:{row['id']}")]]))

    async def _modify(self, q, context, row) -> None:
        self.pending = {"mode": "edit", "id": row["id"]}
        await q.answer()
        await q.message.reply_text(
            "✏️ Qu'est-ce que je change ? Donne une consigne (« plus court », « insiste sur les agents LLM », "
            "« enlève le paragraphe sur l'enseignement »), ou colle ta version corrigée en entier.")

    async def _ignore(self, q, context, row) -> None:
        self.p.store.set_status(row["id"], "ignore")
        await q.answer("Ignorée")
        await q.edit_message_reply_markup(self.kb_reasons(row["id"]))

    async def _reason(self, q, context, row, code: str) -> None:
        self.p.store.set_status(row["id"], "ignore", REASONS.get(code, code))
        await q.answer("Noté, j'en tiendrai compte")
        await q.edit_message_reply_markup(None)

    async def _write(self, q, context, row) -> None:
        await q.answer()
        await q.edit_message_reply_markup(None)
        if len(row.get("description") or "") < SHORT_DESCRIPTION:
            self.pending = {"mode": "description", "id": row["id"]}
            await q.message.reply_text(
                "📋 Je n'ai pas le texte de cette offre. Copie-le depuis la page de l'offre et colle-le ici : "
                "la lettre sera bien meilleure.\nSinon, réponds « go » et j'écris avec ce que j'ai.")
            return
        await self._produce_letter(q.message, row["id"])

    async def _applied(self, q, context, row) -> None:
        self.p.store.set_status(row["id"], "postule", "confirmé par l'utilisateur")
        await q.answer("Bravo, c'est noté !")
        await q.edit_message_reply_markup(None)

    async def _produce_letter(self, message, job_id: int, description: str | None = None) -> None:
        await message.reply_text("✍️ Je rédige la lettre…")
        try:
            row = await asyncio.to_thread(self.p.prepare_letter, job_id, description)
        except Exception as exc:
            log.exception("Rédaction en échec")
            await message.reply_text(f"❗ Rédaction impossible : {exc}")
            return
        self.p.store.set_status(job_id, "propose")
        await self.send_offer(message.get_bot(), row, with_letter=True)

    # ─────────────── Réponses texte ───────────────

    @owner_only
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pending, self.pending = self.pending, None
        message = update.message
        if pending is None:
            await message.reply_text("Utilise les boutons sous les offres, ou /aide.")
            return
        text = message.text.strip()
        if pending["mode"] == "description":
            go = text.lower() in ("go", "ok", "vas-y", "vasy")
            await self._produce_letter(message, pending["id"], None if go else text)
            return
        if len(text) > FULL_LETTER:
            row = self.p.replace_letter(pending["id"], text)
            await message.reply_text("👍 Ta version est enregistrée.")
        else:
            await message.reply_text("✍️ Je réécris…")
            try:
                row = await asyncio.to_thread(self.p.revise_letter, pending["id"], text)
            except Exception as exc:
                log.exception("Réécriture en échec")
                self.pending = pending
                await message.reply_text(f"❗ Réécriture impossible : {exc}\nRéessaie ta consigne.")
                return
        await self.send_letter(message.get_bot(), row)


def run_bot(pipeline: Pipeline, token: str, chat_id: str, tick_minutes: int = 5) -> None:
    bot = Bot(pipeline, chat_id)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler(["aide", "help"], bot.help))
    app.add_handler(CommandHandler("scan", bot.scan))
    app.add_handler(CommandHandler("stats", bot.stats))
    app.add_handler(CommandHandler("cv", bot.cv))
    app.add_handler(CommandHandler("pause", bot.pause))
    app.add_handler(CommandHandler("reprendre", bot.resume))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))
    app.job_queue.run_repeating(bot.tick, interval=tick_minutes * 60, first=15)
    log.info("Bot démarré, vérification toutes les %d minutes", tick_minutes)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
