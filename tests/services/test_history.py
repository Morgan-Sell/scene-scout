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
from scene_scout.history_config import (
    HARD_RECENCY_DAYS,
    SOFT_RECENCY_DAYS,
    SOFT_RECENCY_SCORE_MULTIPLIER,
)
from scene_scout.models.history import RecommendationRecord
from scene_scout.services.feedback import generate_feedback_token
from scene_scout.services.history import (
    HistoryEntryNotFoundError,
    apply_recency_penalty,
    apply_soft_recency_penalty,
    build_recency_lookup,
    classify_recency_penalty,
    get_hard_exclude_event_ids,
    get_last_recommended_at,
    get_recent,
    get_soft_recency_event_ids,
    update_feedback,
    write_recommendations,
)
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


def test_get_soft_and_hard_recency_event_ids(migrated_databases: tuple) -> None:
    hard_cutoff_event = _record(
        event_id="hard-window",
        recommended_at=NOW - timedelta(days=HARD_RECENCY_DAYS - 1),
    )
    soft_only_event = _record(
        event_id="soft-window",
        recommended_at=NOW - timedelta(days=HARD_RECENCY_DAYS + 3),
    )
    outside_event = _record(
        event_id="outside-window",
        recommended_at=NOW - timedelta(days=SOFT_RECENCY_DAYS + 5),
    )
    write_recommendations([hard_cutoff_event, soft_only_event, outside_event])

    soft_ids = get_soft_recency_event_ids(now=NOW)
    hard_ids = get_hard_exclude_event_ids(now=NOW)

    assert soft_ids == {"hard-window", "soft-window"}
    assert hard_ids == {"hard-window"}


def test_classify_recency_penalty_prefers_hard_over_soft(
    migrated_databases: tuple,
) -> None:
    write_recommendations(
        [
            _record(
                event_id="recent-repeat",
                recommended_at=NOW - timedelta(days=5),
            )
        ]
    )

    assert classify_recency_penalty("recent-repeat", now=NOW) == "hard"
    assert classify_recency_penalty("never-sent", now=NOW) == "none"


def test_classify_recency_penalty_soft_only_outside_hard_window(
    migrated_databases: tuple,
) -> None:
    write_recommendations(
        [
            _record(
                event_id="three-weeks-ago",
                recommended_at=NOW - timedelta(days=20),
            )
        ]
    )

    assert classify_recency_penalty("three-weeks-ago", now=NOW) == "soft"


def test_apply_recency_penalty_multiplies_score_for_soft_band(
    migrated_databases: tuple,
) -> None:
    write_recommendations(
        [
            _record(
                event_id="soft-penalty",
                recommended_at=NOW - timedelta(days=20),
            )
        ]
    )

    adjusted, band = apply_recency_penalty(0.8, "soft-penalty", now=NOW)

    assert band == "soft"
    assert adjusted == pytest.approx(0.8 * SOFT_RECENCY_SCORE_MULTIPLIER)


def test_apply_recency_penalty_leaves_score_for_hard_band(
    migrated_databases: tuple,
) -> None:
    write_recommendations(
        [
            _record(
                event_id="hard-exclude",
                recommended_at=NOW - timedelta(days=3),
            )
        ]
    )

    adjusted, band = apply_recency_penalty(0.8, "hard-exclude", now=NOW)

    assert band == "hard"
    assert adjusted == pytest.approx(0.8)


def test_apply_soft_recency_penalty_multiplies_valid_scores() -> None:
    assert apply_soft_recency_penalty(0.8) == pytest.approx(
        0.8 * SOFT_RECENCY_SCORE_MULTIPLIER
    )
    assert apply_soft_recency_penalty(1.0) == pytest.approx(
        SOFT_RECENCY_SCORE_MULTIPLIER
    )


def test_build_recency_lookup_returns_latest_recommended_at(
    migrated_databases: tuple,
) -> None:
    older = _record(
        event_id="repeat-show",
        recommended_at=NOW - timedelta(days=20),
    )
    newer = _record(
        event_id="repeat-show",
        recommended_at=NOW - timedelta(days=10),
    )
    outside = _record(
        event_id="ancient-show",
        recommended_at=NOW - timedelta(days=SOFT_RECENCY_DAYS + 1),
    )
    write_recommendations([older, newer, outside])

    lookup = build_recency_lookup(now=NOW)

    assert lookup == {"repeat-show": newer.recommended_at}
    assert "ancient-show" not in lookup


def test_get_last_recommended_at_returns_none_for_unknown_event(
    migrated_databases: tuple,
) -> None:
    write_recommendations(
        [_record(event_id="known-show", recommended_at=NOW - timedelta(days=5))]
    )

    assert get_last_recommended_at("known-show", now=NOW) == NOW - timedelta(days=5)
    assert get_last_recommended_at("unknown-show", now=NOW) is None


def test_update_feedback_populates_feedback_signal(migrated_databases: tuple) -> None:
    token = generate_feedback_token()
    write_recommendations([_record(feedback_token=token, event_id="feedback-target")])

    updated = update_feedback(token, "click")

    assert updated.feedback_signal == "click"
    assert updated.event_id == "feedback-target"

    entries = get_recent(days=30, now=NOW)
    assert entries[0].feedback_signal == "click"


def test_update_feedback_raises_for_unknown_token(migrated_databases: tuple) -> None:
    with pytest.raises(HistoryEntryNotFoundError):
        update_feedback(generate_feedback_token(), "negative")
