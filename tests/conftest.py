"""Les tests utilisent votre propre configuration et votre profil (config.yaml, profil/).

Tant qu'ils n'existent pas (dépôt fraîchement cloné), les tests sont ignorés :
copiez d'abord le dossier exemple/ comme indiqué dans le README.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if not (ROOT / "config.yaml").exists() or not (ROOT / "profil" / "profil.md").exists():
    collect_ignore = ["test_jobbot.py", "test_telegram.py"]
