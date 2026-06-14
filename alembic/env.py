"""Alembic migration environment for SceneScout feedback and history stores."""

from __future__ import annotations

import logging
from logging.config import fileConfig
from typing import Iterable

from sqlalchemy import engine_from_config, pool

from alembic import context
from scene_scout.db.models import (
    FEEDBACK_TABLES,
    HISTORY_TABLES,
    metadata,
)
from scene_scout.db.urls import database_feedback_url, database_history_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

DATABASES: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("feedback", database_feedback_url(), FEEDBACK_TABLES),
    ("history", database_history_url(), HISTORY_TABLES),
)


def include_name(name: str | None, type_: str, parent_names: Iterable[str]) -> bool:
    """Filter autogenerate and offline operations to the active database tables."""
    allowed = context.config.attributes.get("allowed_tables")
    if allowed is None:
        return True
    if type_ == "table":
        return name in allowed
    return True


def run_migrations_for_database(
    database_name: str,
    database_url: str,
    allowed_tables: frozenset[str],
) -> None:
    """Run Alembic migrations against a single SQLite database."""
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["allowed_tables"] = allowed_tables
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            include_name=include_name,
            render_as_batch=True,
            compare_type=True,
        )

        with context.begin_transaction():
            logger.info("Applying migrations to %s database", database_name)
            context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations to both feedback and history databases."""
    for database_name, database_url, allowed_tables in DATABASES:
        run_migrations_for_database(database_name, database_url, allowed_tables)


run_migrations_online()
