"""
Shared fixtures for service-layer tests that touch Alembic-managed SQLite stores.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scene_scout.db import run_migrations
from scene_scout.services import feedback as feedback_service
from scene_scout.services import history as history_service


@pytest.fixture
def migration_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Isolated feedback and history directories for migration-backed tests."""
    feedback_dir = tmp_path / "vol-feedback"
    history_dir = tmp_path / "vol-history"
    feedback_dir.mkdir()
    history_dir.mkdir()
    feedback_url = f"sqlite:///{feedback_dir / 'feedback.db'}"
    history_url = f"sqlite:///{history_dir / 'history.db'}"
    monkeypatch.setenv("DATABASE_FEEDBACK_URL", feedback_url)
    monkeypatch.setenv("DATABASE_HISTORY_URL", history_url)
    return feedback_dir, history_dir


@pytest.fixture
def migrated_databases(migration_dirs: tuple[Path, Path]) -> tuple[Path, Path]:
    """Run Alembic migrations and reset service engines for isolated DBs."""
    run_migrations()
    feedback_service.reset_feedback_engine()
    history_service.reset_history_engine()
    yield migration_dirs
    feedback_service.reset_feedback_engine()
    history_service.reset_history_engine()
