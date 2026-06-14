"""
Tests for feedback token generation and signal persistence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select

from scene_scout.db.models import feedback_events
from scene_scout.db.urls import database_feedback_url
from scene_scout.models.feedback import FeedbackEvent
from scene_scout.services.feedback import generate_feedback_token, log_signal
from tests.conftest import TEST_RUN_ID

CLICKED_AT = datetime(2026, 6, 12, 18, 0, tzinfo=timezone.utc)


def test_generate_feedback_token_returns_uuid() -> None:
    token = generate_feedback_token()
    parsed = uuid.UUID(token)
    assert str(parsed) == token


def test_feedback_event_validates_full_payload() -> None:
    token = generate_feedback_token()
    event = FeedbackEvent(
        token=token,
        signal="click",
        run_id=TEST_RUN_ID,
        event_id="sandlot-reunion",
        rank=3,
        categories=["baseball", "nostalgia"],
        score_breakdown={"category_match": 0.8, "novelty": 0.6},
        redirect_url="https://example.com/tickets",
        received_at=CLICKED_AT,
    )

    assert event.token == token
    assert event.signal == "click"
    assert event.categories == ["baseball", "nostalgia"]
    assert event.score_breakdown == {"category_match": 0.8, "novelty": 0.6}


def test_feedback_event_rejects_invalid_token() -> None:
    with pytest.raises(ValidationError, match="token"):
        FeedbackEvent(token="not-a-uuid", signal="click", run_id=TEST_RUN_ID)


def test_feedback_event_rejects_invalid_signal() -> None:
    token = generate_feedback_token()
    with pytest.raises(ValidationError):
        FeedbackEvent(token=token, signal="like", run_id=TEST_RUN_ID)  # type: ignore[arg-type]


def test_log_signal_writes_to_vol_feedback(migrated_databases: tuple) -> None:
    feedback_dir, _ = migrated_databases
    token = generate_feedback_token()
    event = FeedbackEvent(
        token=token,
        signal="negative",
        run_id=TEST_RUN_ID,
        event_id="beast-yard-party",
        rank=7,
        categories=["horror"],
        score_breakdown={"vibe_match": 0.2},
        received_at=CLICKED_AT,
    )

    log_signal(event)

    db_path = feedback_dir / "feedback.db"
    assert db_path.is_file()

    engine = create_engine(database_feedback_url())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(feedback_events).where(feedback_events.c.token == token),
            ).one()
    finally:
        engine.dispose()

    assert row.signal == "negative"
    assert row.event_id == "beast-yard-party"
    assert row.run_id == TEST_RUN_ID
    assert row.rank == 7
