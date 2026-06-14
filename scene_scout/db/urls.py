"""Database URL helpers for Alembic-managed SQLite stores."""

from __future__ import annotations

import os
from pathlib import Path

from scene_scout.config import vol_feedback_dir, vol_history_dir

FEEDBACK_DB_FILENAME = "feedback.db"
HISTORY_DB_FILENAME = "history.db"


def _sqlite_url(path: Path) -> str:
    """Return a SQLAlchemy SQLite URL for ``path``."""
    return f"sqlite:///{path.resolve()}"


def database_feedback_url() -> str:
    """Return the feedback database URL."""
    configured = os.getenv("DATABASE_FEEDBACK_URL")
    if configured:
        return configured
    return _sqlite_url(vol_feedback_dir() / FEEDBACK_DB_FILENAME)


def database_history_url() -> str:
    """Return the recommendation history database URL."""
    configured = os.getenv("DATABASE_HISTORY_URL")
    if configured:
        return configured
    return _sqlite_url(vol_history_dir() / HISTORY_DB_FILENAME)


def is_feedback_database_url(url: str) -> bool:
    """Return True when ``url`` points at the feedback SQLite file."""
    return url.rstrip("/").endswith(FEEDBACK_DB_FILENAME)


def is_history_database_url(url: str) -> bool:
    """Return True when ``url`` points at the history SQLite file."""
    return url.rstrip("/").endswith(HISTORY_DB_FILENAME)
