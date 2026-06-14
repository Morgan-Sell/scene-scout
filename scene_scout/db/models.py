"""
SQLAlchemy Core table definitions for Alembic-managed stores.

``feedback_events`` lives in ``vol-feedback/feedback.db``. ``recommendation_history``
lives in ``vol-history/history.db``. ``cache.db`` remains managed by
``cache_schema.py``.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
)

metadata = MetaData()

feedback_events = Table(
    "feedback_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("token", String(36), nullable=False, index=True),
    Column("signal", String(32), nullable=False),
    Column("event_id", String(64), nullable=True),
    Column("run_id", String(32), nullable=False, index=True),
    Column("rank", Integer, nullable=True),
    Column("categories_json", Text, nullable=False, server_default=text("'[]'")),
    Column("score_breakdown_json", Text, nullable=True),
    Column("redirect_url", Text, nullable=True),
    Column("received_at", String(32), nullable=False),
)

recommendation_history = Table(
    "recommendation_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("feedback_token", String(36), nullable=False, unique=True),
    Column("event_id", String(64), nullable=False, index=True),
    Column("run_id", String(32), nullable=False, index=True),
    Column("rank", Integer, nullable=False),
    Column("score", Float, nullable=False),
    Column("score_breakdown_json", Text, nullable=False),
    Column("event_title", Text, nullable=False),
    Column("categories_json", Text, nullable=False),
    Column("explanation", Text, nullable=False),
    Column("neighborhood_context", Text, nullable=True),
    Column("sellout_risk", String(16), nullable=True),
    Column("sellout_urgency_note", Text, nullable=True),
    Column("is_wildcard", Boolean, nullable=False, server_default=text("0")),
    Column("recommended_at", String(32), nullable=False),
    Column("feedback_signal", String(32), nullable=True),
)

FEEDBACK_TABLES = frozenset({feedback_events.name})
HISTORY_TABLES = frozenset({recommendation_history.name})
