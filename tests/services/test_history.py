"""
Tests for recommendation history persistence and retrieval.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select

from scene_scout.db.models import recommendation_history
from scene_scout.db.urls import database_history_url
from scene_scout.models.history import RecommendationRecord
from scene_scout.services.feedback import generate_feedback_token
from scene_scout.services.history import get_recent, write_recommendations
from tests.conftest import TEST_RUN_ID

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
RECENT_AT = NOW - timedelta(days=3)
OLDER_AT = NOW - timedelta(days=20)


def _record(
    *,
    feedback_token: str | None = None,
    event_id: str = "sandlot-classic",
    recommended_at: datetime = RECENT_AT,
    rank: int = 1,
    **overrides: object,
) -> RecommendationRecord:
    payload = {
        "feedback_token": feedback_token or generate_feedback_token(),
        "event_id": event_id,
        "run_id": TEST_RUN_ID,
        "rank": rank,
        "score": 0.85,
        "score_breakdown": {"category_match": 0.9, "novelty": 0.7},
        "event_title": "Pickle the Beast Day",
        "categories": ["family", "baseball"],
        "explanation": "Perfect nostalgia pick for a summer afternoon.",
        "neighborhood_context": "Glendale-adjacent backyard vibes.",
        "sellout_risk": "low",
        "is_wildcard": False,
        "recommended_at": recommended_at,
    }
    payload.update(overrides)
    return RecommendationRecord.model_validate(payload)


def test_recommendation_record_validates() -> None:
    record = _record()
    assert record.event_title == "Pickle the Beast Day"
    assert record.score == 0.85


def test_recommendation_record_rejects_invalid_score() -> None:
    with pytest.raises(ValidationError, match="score"):
        _record(score=1.5)


def test_write_recommendations_persists_rows(migrated_databases: tuple) -> None:
    _, history_dir = migrated_databases
    records = [
        _record(event_id="event-a", rank=1),
        _record(event_id="event-b", rank=2),
    ]

    write_recommendations(records)

    db_path = history_dir / "history.db"
    assert db_path.is_file()

    engine = create_engine(database_history_url())
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(recommendation_history)).fetchall()
    finally:
        engine.dispose()

    assert len(rows) == 2
    event_ids = {row.event_id for row in rows}
    assert event_ids == {"event-a", "event-b"}


def test_get_recent_filters_by_days(migrated_databases: tuple) -> None:
    recent = _record(event_id="recent-hit", recommended_at=RECENT_AT)
    stale = _record(event_id="old-news", recommended_at=OLDER_AT)
    write_recommendations([recent, stale])

    entries = get_recent(days=7, now=NOW)

    assert len(entries) == 1
    assert entries[0].event_id == "recent-hit"


def test_get_recent_orders_newest_first(migrated_databases: tuple) -> None:
    older = _record(
        event_id="older-pick",
        recommended_at=NOW - timedelta(days=2),
        rank=2,
    )
    newer = _record(
        event_id="newer-pick",
        recommended_at=NOW - timedelta(hours=6),
        rank=1,
    )
    write_recommendations([older, newer])

    entries = get_recent(days=7, now=NOW)

    assert [entry.event_id for entry in entries] == ["newer-pick", "older-pick"]
    assert entries[0].recommended_at > entries[1].recommended_at
