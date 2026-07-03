"""
Tests for the event normalization agent.

Covers date parsing edge cases, venue cleanup, category standardization, URL
validation, ID generation, normalization window filtering, and source provenance.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from scene_scout.agents import event_normalization
from scene_scout.models.event import EventCandidate, compute_normalized_event_id
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
REFERENCE_NOW = datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc)
EVENT_DATE = "Sat, Jun 7 2025"
EVENT_TIME = "6:00 PM"
EVENT_VENUE = "The Sandlot."
EVENT_TITLE = "The Great Bambino Night"
EVENT_URL = "https://example.com/great-bambino-night"


def _candidate(**overrides: object) -> EventCandidate:
    payload = {
        "title": EVENT_TITLE,
        "date": EVENT_DATE,
        "time": EVENT_TIME,
        "venue": EVENT_VENUE,
        "neighborhood": "San Fernando Valley",
        "city": "Los Angeles",
        "url": EVENT_URL,
        "price": "Free",
        "description": "Legends retell the Babe Ruth story under the floodlights.",
        "categories": ["Baseball", "Legends"],
        "is_event": True,
        "extraction_confidence": 0.92,
        "source_feed": SANDLOT_FEED,
        "run_id": TEST_RUN_ID,
        "extracted_at": REFERENCE_NOW,
    }
    payload.update(overrides)
    return EventCandidate.model_validate(payload)


def test_normalize_venue_name_strips_trailing_punctuation_and_whitespace() -> None:
    assert event_normalization.normalize_venue_name("The Sandlot. ") == "The Sandlot"
    assert (
        event_normalization.normalize_venue_name("  The   Sandlot  ") == "The Sandlot"
    )


def test_standardize_categories_maps_aliases_and_deduplicates() -> None:
    categories = event_normalization.standardize_categories(
        ["Baseball", "Legends", "Music", "music", "Unknown Label"]
    )

    assert categories == ["Sports", "Community", "Music"]


def test_is_valid_url_rejects_malformed_urls() -> None:
    assert event_normalization.is_valid_url("https://example.com/event") is True
    assert event_normalization.is_valid_url("not-a-url") is False
    assert event_normalization.is_valid_url("ftp://example.com/event") is False


@pytest.mark.parametrize(
    ("date", "time", "expected_day"),
    [
        ("Sat, Jun 7 2025", "6:00 PM", 7),
        ("Jun 7, 2025", None, 7),
        ("2025-06-07", "18:30", 7),
        ("Friday, June 7", "8pm", 7),
    ],
)
def test_parse_event_datetime_handles_edge_case_strings(
    date: str,
    time: str | None,
    expected_day: int,
) -> None:
    default = REFERENCE_NOW.replace(hour=12, minute=0, second=0, microsecond=0)
    parsed = event_normalization.parse_event_datetime(date, time, default=default)

    assert parsed.value is not None
    assert parsed.value.day == expected_day
    assert parsed.value.tzinfo is not None
    assert parsed.time_dropped is False


def test_parse_event_datetime_returns_none_for_unparseable_input() -> None:
    result = event_normalization.parse_event_datetime("not a date", "also bad")
    assert result.value is None
    assert result.time_dropped is False


JULY_REFERENCE_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("date", "time", "expected_day"),
    [
        ("2026-07-07", "9:00 am – 1:00 pm", 7),
        ("2026-07-25", "1:00 to 2:00 PM", 25),
    ],
)
def test_parse_event_datetime_falls_back_to_date_only_for_time_ranges(
    date: str,
    time: str,
    expected_day: int,
) -> None:
    default = JULY_REFERENCE_NOW.replace(hour=12, minute=0, second=0, microsecond=0)
    parsed = event_normalization.parse_event_datetime(date, time, default=default)

    assert parsed.value is not None
    assert parsed.time_dropped is True
    assert parsed.value.day == expected_day
    assert parsed.value.hour == 12
    assert parsed.value.minute == 0


def test_parse_event_datetime_prefers_combined_parse_for_unambiguous_times() -> None:
    default = REFERENCE_NOW.replace(hour=12, minute=0, second=0, microsecond=0)
    parsed = event_normalization.parse_event_datetime(
        "Sat, Jun 7 2025",
        "6:00 PM",
        default=default,
    )

    assert parsed.value is not None
    assert parsed.time_dropped is False
    assert parsed.value.day == 7
    assert parsed.value.hour == 18


def test_normalize_candidate_accepts_time_range_with_date_only_fallback() -> None:
    candidate = _candidate(
        date="2026-07-07",
        time="9:00 am – 1:00 pm",
        city="New York",
    )

    result = event_normalization.normalize_candidate(
        candidate,
        run_id=TEST_RUN_ID,
        feed_quality_scores={SANDLOT_FEED: 0.75},
        now=JULY_REFERENCE_NOW,
    )

    assert result.event is not None
    assert result.time_dropped is True
    assert result.event.start_datetime.day == 7
    assert result.event.start_datetime.hour == 12


@pytest.mark.asyncio
async def test_run_logs_debug_when_time_dropped() -> None:
    candidate = _candidate(
        date="2026-07-07",
        time="9:00 am – 1:00 pm",
        city="New York",
    )
    logger = MagicMock()

    with (
        patch(
            "scene_scout.agents.event_normalization._utc_now",
            return_value=JULY_REFERENCE_NOW,
        ),
        patch(
            "scene_scout.agents.event_normalization._feed_quality_scores",
            return_value={SANDLOT_FEED: 0.75},
        ),
        patch(
            "scene_scout.agents.event_normalization.get_logger",
            return_value=logger,
        ),
    ):
        results = await event_normalization.run([candidate], run_id=TEST_RUN_ID)

    assert len(results) == 1
    time_drop_calls = [
        call
        for call in logger.debug.call_args_list
        if call.args
        and call.args[0] == "Dropped unparseable time; using date-only fallback"
    ]
    assert len(time_drop_calls) == 1
    assert time_drop_calls[0].kwargs["data"]["time"] == "9:00 am – 1:00 pm"
    assert not any(
        call.args[0].startswith("Normalization discards —")
        for call in logger.info.call_args_list
        if call.args
    )


def test_parse_price_handles_free_and_dollar_amounts() -> None:
    assert event_normalization.parse_price("Free") == (None, True)
    assert event_normalization.parse_price("$15") == (1500, False)
    assert event_normalization.parse_price("$10-$20") == (1000, False)


def test_normalize_candidate_populates_source_fields_and_stable_id() -> None:
    candidate = _candidate()
    feed_scores = {SANDLOT_FEED: 0.75}

    result = event_normalization.normalize_candidate(
        candidate,
        run_id=TEST_RUN_ID,
        feed_quality_scores=feed_scores,
        now=REFERENCE_NOW,
    )

    assert result.event is not None
    event = result.event
    assert event.id == compute_normalized_event_id(
        EVENT_TITLE,
        EVENT_DATE,
        "The Sandlot",
    )
    assert event.venue == "The Sandlot"
    assert event.source_feeds == [SANDLOT_FEED]
    assert event.source_count == 1
    assert event.best_source_feed == SANDLOT_FEED
    assert event.source_quality_score == 0.75
    assert event.is_free is True
    assert event.categories == ["Sports", "Community"]
    assert event.run_id == TEST_RUN_ID
    assert event.normalized_at == REFERENCE_NOW


def test_normalize_candidate_discards_events_outside_seven_day_window() -> None:
    candidate = _candidate(date="Sat, Jun 20 2025", time="6:00 PM")

    result = event_normalization.normalize_candidate(
        candidate,
        run_id=TEST_RUN_ID,
        feed_quality_scores={SANDLOT_FEED: 0.75},
        now=REFERENCE_NOW,
    )

    assert result.event is None
    assert result.discard_reason == event_normalization.DISCARD_OUTSIDE_WINDOW


def test_normalize_candidate_discards_events_in_the_past() -> None:
    candidate = _candidate(date="Sat, Jun 1 2025", time="6:00 PM")

    result = event_normalization.normalize_candidate(
        candidate,
        run_id=TEST_RUN_ID,
        feed_quality_scores={SANDLOT_FEED: 0.75},
        now=REFERENCE_NOW,
    )

    assert result.event is None
    assert result.discard_reason == event_normalization.DISCARD_OUTSIDE_WINDOW


@pytest.mark.asyncio
async def test_run_discards_unparseable_dates_and_logs_warning(logs_dir) -> None:
    candidate = _candidate(date="totally invalid", time="nope")

    with (
        patch(
            "scene_scout.agents.event_normalization._utc_now",
            return_value=REFERENCE_NOW,
        ),
        patch(
            "scene_scout.agents.event_normalization._feed_quality_scores",
            return_value={SANDLOT_FEED: 0.75},
        ),
    ):
        results = await event_normalization.run([candidate], run_id=TEST_RUN_ID)

    assert results == []
    log_entries = _read_all_log_entries(logs_dir)
    discard_logs = [
        entry
        for entry in log_entries
        if entry["message"].startswith("Normalization discards —")
    ]
    assert len(discard_logs) == 1
    assert discard_logs[0]["data"]["discard_reason"] == (
        event_normalization.DISCARD_UNPARSEABLE_DATE
    )
    assert discard_logs[0]["data"]["count"] == 1
    assert any(
        entry["message"] == "Normalization discard summary" for entry in log_entries
    )
    assert not any(
        entry["message"].startswith("Discarding candidate with unparseable date")
        for entry in log_entries
    )


@pytest.mark.asyncio
async def test_run_returns_normalized_events_within_window() -> None:
    in_window = _candidate()
    out_of_window = _candidate(
        title="Future Sandlot Classic",
        date="Sat, Jul 4 2025",
        url="https://example.com/future-classic",
    )

    with (
        patch(
            "scene_scout.agents.event_normalization._utc_now",
            return_value=REFERENCE_NOW,
        ),
        patch(
            "scene_scout.agents.event_normalization._feed_quality_scores",
            return_value={SANDLOT_FEED: 0.75},
        ),
    ):
        results = await event_normalization.run(
            [in_window, out_of_window],
            run_id=TEST_RUN_ID,
        )

    assert len(results) == 1
    assert results[0].title == EVENT_TITLE


@pytest.mark.asyncio
async def test_run_aggregates_discards_by_reason(logs_dir) -> None:
    candidates = [
        _candidate(title=f"Bad Date {index}", date="not-a-date", time="nope")
        for index in range(3)
    ] + [
        _candidate(
            title=f"No Venue {index}",
            venue="  ",
            url=f"https://example.com/no-venue-{index}",
        )
        for index in range(2)
    ]

    with (
        patch(
            "scene_scout.agents.event_normalization._utc_now",
            return_value=REFERENCE_NOW,
        ),
        patch(
            "scene_scout.agents.event_normalization._feed_quality_scores",
            return_value={SANDLOT_FEED: 0.75},
        ),
    ):
        results = await event_normalization.run(candidates, run_id=TEST_RUN_ID)

    assert results == []
    log_entries = _read_all_log_entries(logs_dir)
    reason_logs = {
        entry["data"]["discard_reason"]: entry
        for entry in log_entries
        if entry["message"].startswith("Normalization discards —")
    }
    unparseable = event_normalization.DISCARD_UNPARSEABLE_DATE
    missing_venue = event_normalization.DISCARD_MISSING_VENUE
    assert reason_logs[unparseable]["data"]["count"] == 3
    assert reason_logs[missing_venue]["data"]["count"] == 2
    assert len(reason_logs) == 2

    summary = next(
        entry
        for entry in log_entries
        if entry["message"] == "Normalization discard summary"
    )
    assert summary["data"]["total_discarded"] == 5


def test_discard_collector_samples_titles_for_terminal_message() -> None:
    collector = event_normalization.NormalizationDiscardCollector()
    for index in range(7):
        collector.record(
            event_normalization.DISCARD_OUTSIDE_WINDOW,
            {"title": f"Event {index}", "source_feed": SANDLOT_FEED},
        )

    messages: list[str] = []

    class _CaptureLogger:
        def info(self, message: str, *args, data=None) -> None:
            formatted = message % args if args else message
            messages.append(formatted)

    collector.emit(_CaptureLogger())

    assert messages[0].startswith(
        "Normalization discards — outside normalization window: 7"
    )
    assert "Event 0" in messages[0]
    assert "Event 4" in messages[0]
    assert "Event 5" not in messages[0]
    assert messages[1] == "Normalization discard summary"


def _read_all_log_entries(logs_dir) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for log_file in logs_dir.glob("*.jsonl"):
        entries.extend(
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
            if line.strip()
        )
    return entries
