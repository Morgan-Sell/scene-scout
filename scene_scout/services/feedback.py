"""
Feedback signal persistence for SceneScout.

Writes validated :class:`FeedbackEvent` records to ``vol-feedback/feedback.db``.
Schema is managed by Alembic — callers must run :func:`run_migrations` before use.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import Engine, create_engine, insert
from sqlalchemy.engine import Connection

from scene_scout.db.models import feedback_events
from scene_scout.db.urls import database_feedback_url
from scene_scout.logging import get_logger
from scene_scout.models.feedback import FeedbackEvent

_engine: Engine | None = None


def reset_feedback_engine() -> None:
    """Clear the cached SQLAlchemy engine (for tests)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(database_feedback_url())
    return _engine


def _format_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def generate_feedback_token() -> str:
    """Return a new UUID4 feedback token string."""
    return str(uuid.uuid4())


def log_signal(
    event: FeedbackEvent,
    *,
    conn: Connection | None = None,
    run_id: str | None = None,
) -> None:
    """Persist a feedback event to ``feedback_events``.

    Parameters
    ----------
    event : FeedbackEvent
        Validated feedback payload.
    conn : Connection, optional
        Existing SQLAlchemy connection for tests or transactions.
    run_id : str, optional
        Pipeline run identifier for structured logging.
    """
    values = {
        "token": event.token,
        "signal": event.signal,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "rank": event.rank,
        "categories_json": json.dumps(event.categories),
        "score_breakdown_json": (
            json.dumps(event.score_breakdown)
            if event.score_breakdown is not None
            else None
        ),
        "redirect_url": event.redirect_url,
        "received_at": _format_dt(event.received_at),
    }

    if conn is not None:
        conn.execute(insert(feedback_events).values(**values))
    else:
        with _get_engine().begin() as connection:
            connection.execute(insert(feedback_events).values(**values))

    if run_id is not None:
        logger = get_logger("feedback", run_id=run_id)
        logger.info(
            "Logged feedback signal",
            data={
                "token": event.token,
                "signal": event.signal,
                "event_id": event.event_id,
            },
        )
