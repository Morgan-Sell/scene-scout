"""
Tests for the pre-enrichment filter.

Covers low-information discard, coming-week window, recommendation exclude window,
logging, and discard count aggregation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from scene_scout.models.event import NormalizedEvent, compute_normalized_event_id
from scene_scout.orchestrator import (
    DISCARD_EXCLUDE_WINDOW,
    DISCARD_LOW_INFORMATION,
    DISCARD_OUTSIDE_WEEK,
    apply_pre_enrichment_filter,
)
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
REFERENCE_NOW = datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc)
TEST_HORIZON_DAYS = 7
EVENT_DATE = "Sat, Jun 7 2025"
EVENT_VENUE = "The Sandlot"
EVENT_TITLE = "The Great Bambino Night"
EVENT_ID = compute_normalized_event_id(EVENT_TITLE, EVENT_DATE, EVENT_VENUE)


def _event(**overrides: object) -> NormalizedEvent:
    payload = {
        "id": EVENT_ID,
        "title": EVENT_TITLE,
        "start_datetime": datetime(2025, 6, 7, 18, 0, tzinfo=timezone.utc),
        "venue": EVENT_VENUE,
        "city": "Los Angeles",
        "url": "https://example.com/great-bambino-night",
        "is_free": True,
        "description": "Legends retell the Babe Ruth story under the floodlights.",
        "categories": ["Sports"],
        "source_feeds": [SANDLOT_FEED],
        "source_count": 1,
        "best_source_feed": SANDLOT_FEED,
        "source_quality_score": 0.8,
        "description_quality_score": 0.8,
        "low_information": False,
        "run_id": TEST_RUN_ID,
        "normalized_at": REFERENCE_NOW,
    }
    payload.update(overrides)
    return NormalizedEvent.model_validate(payload)


def test_apply_pre_enrichment_filter_passes_valid_event() -> None:
    result = apply_pre_enrichment_filter(
        [_event()],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids=set(),
    )

    assert len(result.events) == 1
    assert result.total_discarded == 0


def test_apply_pre_enrichment_filter_discards_low_information() -> None:
    result = apply_pre_enrichment_filter(
        [_event(low_information=True, description_quality_score=0.1)],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids=set(),
    )

    assert result.events == []
    assert result.discards[DISCARD_LOW_INFORMATION] == 1


def test_apply_pre_enrichment_filter_discards_outside_coming_week() -> None:
    far_future = _event(
        start_datetime=datetime(2025, 7, 4, 18, 0, tzinfo=timezone.utc),
        id=compute_normalized_event_id(EVENT_TITLE, "Sat, Jul 4 2025", EVENT_VENUE),
    )

    result = apply_pre_enrichment_filter(
        [far_future],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids=set(),
    )

    assert result.events == []
    assert result.discards[DISCARD_OUTSIDE_WEEK] == 1


def test_apply_pre_enrichment_filter_discards_past_events() -> None:
    past_event = _event(
        start_datetime=datetime(2025, 6, 1, 18, 0, tzinfo=timezone.utc),
        id=compute_normalized_event_id(EVENT_TITLE, "Sun, Jun 1 2025", EVENT_VENUE),
    )

    result = apply_pre_enrichment_filter(
        [past_event],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids=set(),
    )

    assert result.events == []
    assert result.discards[DISCARD_OUTSIDE_WEEK] == 1


def test_apply_pre_enrichment_filter_discards_exclude_window_event_ids() -> None:
    result = apply_pre_enrichment_filter(
        [_event()],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids={EVENT_ID},
    )

    assert result.events == []
    assert result.discards[DISCARD_EXCLUDE_WINDOW] == 1


def test_apply_pre_enrichment_filter_low_information_takes_priority_over_exclude() -> (
    None
):
    result = apply_pre_enrichment_filter(
        [_event(low_information=True, description_quality_score=0.1)],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids={EVENT_ID},
    )

    assert result.discards[DISCARD_LOW_INFORMATION] == 1
    assert result.discards[DISCARD_EXCLUDE_WINDOW] == 0


def test_load_hard_exclude_event_ids_from_history_db(
    migrated_databases: tuple,
) -> None:
    from scene_scout.models.history import RecommendationRecord
    from scene_scout.services.feedback import generate_feedback_token
    from scene_scout.services.history import write_recommendations

    write_recommendations(
        [
            RecommendationRecord(
                feedback_token=generate_feedback_token(),
                event_id="recent-event",
                run_id=TEST_RUN_ID,
                rank=1,
                score=0.9,
                score_breakdown={"category_match": 0.9},
                event_title="Recent Sandlot Classic",
                explanation="Sent a few days ago.",
                recommended_at=REFERENCE_NOW - timedelta(days=3),
            ),
            RecommendationRecord(
                feedback_token=generate_feedback_token(),
                event_id="stale-event",
                run_id=TEST_RUN_ID,
                rank=1,
                score=0.9,
                score_breakdown={"category_match": 0.9},
                event_title="Stale Sandlot Classic",
                explanation="Sent three weeks ago.",
                recommended_at=REFERENCE_NOW - timedelta(days=20),
            ),
        ]
    )

    result = apply_pre_enrichment_filter(
        [
            _event(id="recent-event"),
            _event(
                id="stale-event",
                title="Stale Sandlot Classic",
            ),
            _event(
                id="fresh-event",
                title="Fresh Sandlot Classic",
            ),
        ],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
    )

    assert len(result.events) == 2
    assert {event.id for event in result.events} == {"stale-event", "fresh-event"}
    assert result.discards[DISCARD_EXCLUDE_WINDOW] == 1


def test_apply_pre_enrichment_filter_logs_discards(logs_dir) -> None:
    apply_pre_enrichment_filter(
        [_event(low_information=True, description_quality_score=0.1)],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids=set(),
    )

    log_entries = []
    for log_file in logs_dir.glob("*.jsonl"):
        log_entries.extend(
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
            if line.strip()
        )

    assert any(
        entry["message"].startswith("Pre-enrichment filter discard")
        for entry in log_entries
    )
    complete_entry = next(
        entry
        for entry in log_entries
        if entry["message"] == "Pre-enrichment filter complete"
    )
    assert complete_entry["data"]["discards"][DISCARD_LOW_INFORMATION] == 1


def test_apply_pre_enrichment_filter_keeps_event_at_horizon_boundary() -> None:
    at_horizon = _event(
        start_datetime=REFERENCE_NOW + timedelta(days=TEST_HORIZON_DAYS),
        id=compute_normalized_event_id(
            EVENT_TITLE,
            f"horizon-{TEST_HORIZON_DAYS}",
            EVENT_VENUE,
        ),
    )

    result = apply_pre_enrichment_filter(
        [at_horizon],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids=set(),
    )

    assert len(result.events) == 1
    assert result.discards[DISCARD_OUTSIDE_WEEK] == 0


def test_apply_pre_enrichment_filter_discards_event_beyond_horizon() -> None:
    beyond_horizon = _event(
        start_datetime=REFERENCE_NOW + timedelta(days=TEST_HORIZON_DAYS + 1),
        id=compute_normalized_event_id(
            EVENT_TITLE,
            f"beyond-{TEST_HORIZON_DAYS + 1}",
            EVENT_VENUE,
        ),
    )

    result = apply_pre_enrichment_filter(
        [beyond_horizon],
        TEST_RUN_ID,
        horizon_days=TEST_HORIZON_DAYS,
        now=REFERENCE_NOW,
        exclude_event_ids=set(),
    )

    assert result.events == []
    assert result.discards[DISCARD_OUTSIDE_WEEK] == 1
