"""Database migration helpers for SceneScout."""

from __future__ import annotations

from alembic.config import Config

from alembic import command
from scene_scout.config import PROJECT_ROOT

_ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def alembic_config() -> Config:
    """Return Alembic configuration rooted at the project ``alembic.ini``."""
    return Config(str(_ALEMBIC_INI))


def run_migrations() -> None:
    """Apply all pending Alembic migrations for feedback and history databases."""
    command.upgrade(alembic_config(), "head")
