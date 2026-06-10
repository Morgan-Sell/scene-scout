"""
Tests for the deduplication agent.

Covers exact ID collapse, fuzzy title matching, multi-feed merge provenance,
non-duplicate preservation, and merge logging.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scene_scout.agents import deduplication
from scene_scout.models.event import NormalizedEvent, compute_normalized_event_id
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
RIVAL_FEED = "rival-neighborhood-league"
KCET_FEED = "kcet_arts"
START = datetime(2025, 6, 7, 18, 0, tzinfo=timezone.utc)
EVENT_DATE = "Sat, Jun 7 2025"
EVENT_VENUE = "The Sandlot"
BASE_TITLE = "The Great Bambino Night"
BASE_ID = compute_normalized_event_id(BASE_TITLE, EVENT_DATE, EVENT_VENUE)


def _event(
    *,
    feed: str,
    quality: float,
    title: str = BASE_TITLE,
    description: str = "Legends retell the Babe Ruth story.",
    event_id: str = BASE_ID,
    url: str | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        id=event_id,
        title=title,
        start_datetime=START,
        venue=EVENT_VENUE,
        city="Los Angeles",
        url=url or f"https://example.com/{feed}/great-bambino-night",
        is_free=True,
        description=description,
        source_feeds=[feed],
        source_count=1,
        best_source_feed=feed,
        source_quality_score=quality,
        run_id=TEST_RUN_ID,
        normalized_at=START,
    )


def test_merge_events_keeps_content_from_highest_quality_source() -> None:
    sandlot = _event(
        feed=SANDLOT_FEED,
        quality=0.75,
        description="Sandlot legends version.",
    )
    rival = _event(
        feed=RIVAL_FEED,
        quality=0.95,
        description="Rival league premium write-up.",
    )

    merged = deduplication.merge_events([sandlot, rival])

    assert merged.title == BASE_TITLE
    assert merged.description == "Rival league premium write-up."
    assert merged.venue == EVENT_VENUE
    assert merged.best_source_feed == RIVAL_FEED
    assert merged.source_quality_score == 0.95
    assert merged.source_feeds == [RIVAL_FEED, SANDLOT_FEED]
    assert merged.source_count == 2


def test_exact_id_match_collapses_identical_events() -> None:
    sandlot = _event(feed=SANDLOT_FEED, quality=0.75)
    rival = _event(feed=RIVAL_FEED, quality=0.80)

    results = deduplication.deduplicate_events([sandlot, rival])

    assert len(results) == 1
    assert results[0].source_count == 2
    assert set(results[0].source_feeds) == {SANDLOT_FEED, RIVAL_FEED}


def test_fuzzy_match_merges_near_duplicate_titles() -> None:
    sandlot = _event(feed=SANDLOT_FEED, quality=0.75, title="Great Bambino Night")
    rival = _event(
        feed=RIVAL_FEED,
        quality=0.80,
        title="The Great Bambino Night!",
        event_id=compute_normalized_event_id(
            "The Great Bambino Night!",
            EVENT_DATE,
            EVENT_VENUE,
        ),
    )

    assert deduplication.is_fuzzy_duplicate(sandlot, rival) is True

    results = deduplication.deduplicate_events([sandlot, rival])

    assert len(results) == 1
    assert results[0].source_count == 2


def test_fuzzy_match_requires_same_date_and_venue() -> None:
    left = _event(feed=SANDLOT_FEED, quality=0.75, title="Great Bambino Night")
    right = _event(
        feed=RIVAL_FEED,
        quality=0.80,
        title="The Great Bambino Night",
        event_id=compute_normalized_event_id(BASE_TITLE, EVENT_DATE, "Rec Center"),
    )
    right = right.model_copy(update={"venue": "Rec Center"})

    assert deduplication.is_fuzzy_duplicate(left, right) is False


def test_different_events_are_not_merged() -> None:
    bambino = _event(feed=SANDLOT_FEED, quality=0.75)
    pool_party = _event(
        feed=RIVAL_FEED,
        quality=0.80,
        title="Pool Party at the Rec Center",
        description="Squints-approved summer hangout.",
        event_id=compute_normalized_event_id(
            "Pool Party at the Rec Center",
            EVENT_DATE,
            "Rec Center",
        ),
        url="https://example.com/pool-party",
    )
    pool_party = pool_party.model_copy(update={"venue": "Rec Center"})

    results = deduplication.deduplicate_events([bambino, pool_party])

    assert len(results) == 2


def test_multi_feed_merge_carries_union_provenance() -> None:
    sandlot = _event(feed=SANDLOT_FEED, quality=0.75)
    rival = _event(feed=RIVAL_FEED, quality=0.80)
    kcet = _event(feed=KCET_FEED, quality=0.70)

    results = deduplication.deduplicate_events([sandlot, rival, kcet])

    assert len(results) == 1
    merged = results[0]
    assert merged.source_feeds == [KCET_FEED, RIVAL_FEED, SANDLOT_FEED]
    assert merged.source_count == 3
    assert merged.best_source_feed == RIVAL_FEED
    assert merged.source_quality_score == 0.80


@pytest.mark.asyncio
async def test_run_logs_merge_with_source_feed_ids_and_source_count(logs_dir) -> None:
    sandlot = _event(feed=SANDLOT_FEED, quality=0.75)
    rival = _event(feed=RIVAL_FEED, quality=0.80)

    results = await deduplication.run([sandlot, rival], run_id=TEST_RUN_ID)

    assert len(results) == 1
    log_entries = _read_all_log_entries(logs_dir)
    merge_entry = next(
        entry for entry in log_entries if entry["message"] == "Merged duplicate events"
    )
    assert merge_entry["data"]["source_feed_ids"] == [RIVAL_FEED, SANDLOT_FEED]
    assert merge_entry["data"]["source_count"] == 2
    assert merge_entry["data"]["merge_type"] == "exact_id"


def _read_all_log_entries(logs_dir) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for log_file in logs_dir.glob("*.jsonl"):
        entries.extend(
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
            if line.strip()
        )
    return entries
