"""Point d'entrée.

    python -m jobbot            lance le bot (veille + Telegram)
    python -m jobbot essai      passage à blanc : sources + filtres, sans IA ni Telegram
    python -m jobbot verifier   vérifie les clés et les connexions du fichier .env
"""
import logging
import sys
import tempfile
from collections import Counter
from pathlib import Path

from . import config
from .db import Store
from .pipeline import Pipeline


def essai(cfg: dict) -> None:
    """Montre ce que le bot trouverait maintenant, sans rien dépenser ni envoyer."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "essai.sqlite")
        pipeline = Pipeline(cfg, store)
        print("Sources actives :", ", ".join(pipeline.available_sources()))
        pipeline.collect(force=True)
        counts = store.counts()
        rejected = Counter(r["reason"].split(" (")[0] for r in store.by_status("filtre", 100000))
        kept = store.by_status("a_noter", 100000)
        print(f"\nOffres lues : {sum(counts.values())}")
        for reason, n in rejected.most_common():
            print(f"  éliminées, {reason} : {n}")
        print(f"\n{len(kept)} offres seraient envoyées à l'IA pour être notées :")
        for row in kept:
            print(f"  • {row['title']} — {row['company']} ({row['location'] or '?'}) [{row['source']}]")


def verifier(cfg: dict) -> None:
    checks = []

    def check(name, func):
        try:
            detail = func()
            checks.append(f"✅ {name}" + (f" : {detail}" if detail else ""))
        except Exception as exc:
            checks.append(f"❌ {name} : {exc}")

    def anthropic_key():
        from .llm import SCORE_SCHEMA, Claude
        claude = Claude(cfg["ia"], "", cfg["identite"]["nom"])
        if claude.engine == "api":
            import anthropic
            return "API, " + anthropic.Anthropic().models.retrieve(cfg["ia"]["modele_lettre"]).display_name
        claude._call(cfg["ia"]["modele_tri"], "low", "Réponds au format demandé.",
                     "Mets score à 1 et tous les textes vides.", SCORE_SCHEMA, 1000)
        return "abonnement via Claude Code, appel test réussi"

    def telegram():
        import requests
        token = config.env("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN manquant")
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
        if not me.get("ok"):
            raise RuntimeError(me.get("description", "jeton refusé"))
        chat = config.env("TELEGRAM_CHAT_ID") or "TELEGRAM_CHAT_ID manquant : envoie /start au bot"
        return f"@{me['result']['username']}, conversation {chat}"

    def gmail():
        import imaplib
        if not config.env("GMAIL_MOT_DE_PASSE_APPLI"):
            raise RuntimeError("non configuré (alertes LinkedIn et envoi par email désactivés)")
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(config.env("GMAIL_ADRESSE"), config.env("GMAIL_MOT_DE_PASSE_APPLI"))
        return config.env("GMAIL_ADRESSE")

    def france_travail():
        pipeline = Pipeline(cfg, Store(Path(tempfile.mkdtemp()) / "v.sqlite"))
        if not pipeline.france_travail:
            raise RuntimeError("non configuré (source désactivée)")
        pipeline.france_travail._auth()

    def adzuna():
        if not config.env("ADZUNA_APP_ID"):
            raise RuntimeError("non configuré (source désactivée)")
        from .sources import adzuna as az
        return f"{len(az.fetch(config.env('ADZUNA_APP_ID'), config.env('ADZUNA_APP_KEY'), {'fr': ['data scientist']}, 3))} offres test"

    def cv():
        for lang, rel in cfg["cv"].items():
            if rel and not (config.ROOT / rel).exists():
                raise RuntimeError(f"CV {lang} introuvable : {rel}")
        return ", ".join(f"{lang} : {Path(rel).name}" for lang, rel in cfg["cv"].items() if rel)

    check("Claude", anthropic_key)
    check("Bot Telegram", telegram)
    check("Gmail", gmail)
    check("France Travail", france_travail)
    check("Adzuna", adzuna)
    check("CV", cv)
    print("\n".join(checks))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config.load_config()
    command = sys.argv[1] if len(sys.argv) > 1 else "bot"
    if command == "essai":
        logging.getLogger().setLevel(logging.WARNING)
        essai(cfg)
    elif command == "verifier":
        verifier(cfg)
    elif command == "bot":
        from .llm import Claude
        from .telegram_bot import run_bot
        token = config.env("TELEGRAM_BOT_TOKEN")
        if not token:
            sys.exit("TELEGRAM_BOT_TOKEN manquant dans .env (voir README).")
        store = Store(config.DATA / "jobbot.sqlite")
        claude = Claude(cfg["ia"], cfg["tri"]["preferences"], cfg["identite"]["nom"])
        run_bot(Pipeline(cfg, store, claude), token, config.env("TELEGRAM_CHAT_ID"))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
