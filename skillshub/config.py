"""Configuration : lecture du fichier .env et chemins du projet."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "").strip()
ENRICH_BACKEND: str = (os.getenv("ENRICH_BACKEND", "") or "auto").strip().lower()
DB_PATH: Path = _path(os.getenv("DB_PATH"), ROOT / "data" / "skills.db")
SEEDS_PATH: Path = _path(os.getenv("SEEDS_PATH"), ROOT / "data" / "seeds.txt")

# Nombre de sous-processus claude/codex lancés en parallèle (2 à 4 conseillé).
ENRICH_CONCURRENCY: int = max(1, min(8, _int("ENRICH_CONCURRENCY", 3)))
# Délai maximal d'un appel CLI d'enrichissement, en secondes.
ENRICH_TIMEOUT: int = _int("ENRICH_TIMEOUT", 120)
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "haiku").strip() or "haiku"
# Garde-fou : certains dépôts agrègent des milliers de SKILL.md.
MAX_SKILLS_PER_REPO: int = _int("MAX_SKILLS_PER_REPO", 1000)

OFFICIAL_OWNERS = {"anthropics"}
