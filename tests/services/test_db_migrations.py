"""
Tests for Alembic migration infrastructure.

Covers programmatic ``run_migrations()`` idempotency and upgrade/downgrade
round-trips against isolated SQLite files.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from alembic import command
from scene_scout.db import alembic_config, run_migrations
from scene_scout.db.models import feedback_events, recommendation_history
from scene_scout.db.urls import database_feedback_url, database_history_url


def _table_names(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_run_migrations_is_idempotent(migration_dirs: tuple[Path, Path]) -> None:
    run_migrations()
    feedback_after_first = _table_names(database_feedback_url())
    history_after_first = _table_names(database_history_url())

    run_migrations()
    feedback_after_second = _table_names(database_feedback_url())
    history_after_second = _table_names(database_history_url())

    assert feedback_events.name in feedback_after_first
    assert recommendation_history.name in history_after_first
    assert "alembic_version" in feedback_after_first
    assert "alembic_version" in history_after_first
    assert feedback_after_second == feedback_after_first
    assert history_after_second == history_after_first


def test_upgrade_and_downgrade_round_trip(migration_dirs: tuple[Path, Path]) -> None:
    config = alembic_config()

    command.upgrade(config, "head")
    assert feedback_events.name in _table_names(database_feedback_url())
    assert recommendation_history.name in _table_names(database_history_url())

    command.downgrade(config, "base")
    feedback_tables = _table_names(database_feedback_url())
    history_tables = _table_names(database_history_url())

    assert feedback_events.name not in feedback_tables
    assert recommendation_history.name not in history_tables
