"""Chargement de config.yaml, des secrets (.env) et des fichiers du profil."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROFIL = ROOT / "profil"


def load_config(path: Path | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def read_profile() -> tuple[str, str, str]:
    """Fiche de faits, guide de style et lettre de référence."""
    return (
        (PROFIL / "profil.md").read_text(encoding="utf-8"),
        (PROFIL / "style.md").read_text(encoding="utf-8"),
        (PROFIL / "lettres" / "reference_TRANSPOP.txt").read_text(encoding="utf-8"),
    )


def validated_letters(limit: int = 3) -> list[str]:
    """Les dernières lettres validées par l'utilisateur : elles affinent le style."""
    files = sorted((PROFIL / "lettres_validees").glob("*.txt"), reverse=True)[:limit]
    return [f.read_text(encoding="utf-8") for f in files]
