"""
Tests for the description quality agent.

Covers all seven rubric signal boundary conditions, composite scoring,
low_information thresholding, and run() population of quality fields.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scene_scout.agents import description_quality
from scene_scout.config import DESCRIPTION_QUALITY_THRESHOLD
from scene_scout.models.event import NormalizedEvent, compute_normalized_event_id
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
START_WITH_TIME = datetime(2025, 6, 7, 18, 0, tzinfo=timezone.utc)
START_DATE_ONLY = datetime(2025, 6, 7, 0, 0, tzinfo=timezone.utc)
EVENT_ID = compute_normalized_event_id(
    "The Great Bambino Night",
    "Sat, Jun 7 2025",
    "The Sandlot",
)


def _event(**overrides: object) -> NormalizedEvent:
    payload = {
        "id": EVENT_ID,
        "title": "The Great Bambino Night",
        "start_datetime": START_WITH_TIME,
        "venue": "The Sandlot",
        "city": "Los Angeles",
        "url": "https://example.com/great-bambino-night",
        "is_free": True,
        "description": "Legends retell the Babe Ruth story under the floodlights.",
        "categories": ["Sports"],
        "source_feeds": [SANDLOT_FEED],
        "source_count": 1,
        "best_source_feed": SANDLOT_FEED,
        "source_quality_score": 0.8,
        "run_id": TEST_RUN_ID,
        "normalized_at": START_WITH_TIME,
    }
    payload.update(overrides)
    return NormalizedEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("", 0.0),
        ("x" * 49, 0.3),
        ("x" * 50, 0.7),
        ("x" * 149, 0.7),
        ("x" * 150, 1.0),
    ],
)
def test_score_description_length_boundary_conditions(
    description: str,
    expected: float,
) -> None:
    assert description_quality.score_description_length(description) == expected


@pytest.mark.parametrize(
    ("venue", "expected"),
    [
        ("The Sandlot", 1.0),
        ("TBA", 0.0),
        ("Los Angeles", 0.0),
        ("", 0.0),
    ],
)
def test_score_venue_presence_boundary_conditions(venue: str, expected: float) -> None:
    assert description_quality.score_venue_presence(venue) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (START_WITH_TIME, None, 1.0),
        (START_DATE_ONLY, None, 0.5),
        (START_DATE_ONLY, START_WITH_TIME, 1.0),
    ],
)
def test_score_date_time_present_boundary_conditions(
    start: datetime,
    end: datetime | None,
    expected: float,
) -> None:
    event = _event(start_datetime=start, end_datetime=end)
    assert description_quality.score_date_time_present(event) == expected


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("Benny Rodriguez Live", "A sandlot showcase.", 0.0),
        ("Summer Showdown", "Featuring Ham Porter under the lights.", 1.0),
        ("Open Mic", "With local artists on stage.", 0.0),
        ("Night Session", "DJ Smalls spins vinyl all night.", 1.0),
    ],
)
def test_score_performer_named_boundary_conditions(
    title: str,
    description: str,
    expected: float,
) -> None:
    assert description_quality.score_performer_named(title, description) == expected


@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        (["Music"], 1.0),
        (["General"], 0.0),
        ([], 0.0),
        (["General", "Music"], 1.0),
    ],
)
def test_score_category_coverage_boundary_conditions(
    categories: list[str],
    expected: float,
) -> None:
    assert description_quality.score_category_coverage(categories) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/event", 1.0),
        ("not-a-url", 0.0),
    ],
)
def test_score_url_validity_boundary_conditions(url: str, expected: float) -> None:
    assert description_quality.score_url_validity(url) == expected


@pytest.mark.parametrize(
    ("price_cents", "is_free", "expected"),
    [
        (1500, False, 1.0),
        (None, True, 1.0),
        (None, False, 0.0),
    ],
)
def test_score_price_clarity_boundary_conditions(
    price_cents: int | None,
    is_free: bool,
    expected: float,
) -> None:
    assert description_quality.score_price_clarity(price_cents, is_free) == expected


def test_composite_score_weights_all_seven_signals() -> None:
    signals = description_quality.DescriptionQualitySignals(
        description_length=1.0,
        venue_presence=1.0,
        date_time_present=1.0,
        performer_named=1.0,
        category_coverage=1.0,
        url_validity=1.0,
        price_clarity=1.0,
    )

    assert signals.composite_score() == 1.0


def test_apply_quality_scores_sets_low_information_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scene_scout.config.DESCRIPTION_QUALITY_THRESHOLD", 0.3)
    sparse = _event(
        description="",
        venue="TBA",
        categories=[],
        is_free=False,
        url="not-a-url",
    )

    scored = description_quality.apply_quality_scores(sparse)

    assert scored.description_quality_score < 0.3
    assert scored.low_information is True


def test_apply_quality_scores_clears_low_information_at_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scene_scout.config.DESCRIPTION_QUALITY_THRESHOLD", 0.3)
    rich = _event(
        description="x" * 150,
        categories=["Music"],
    )

    scored = description_quality.apply_quality_scores(rich)

    assert scored.description_quality_score >= 0.3
    assert scored.low_information is False


@pytest.mark.asyncio
async def test_run_populates_quality_fields_on_every_record() -> None:
    events = [
        _event(title="Sparse Event", description="", venue="TBA", categories=[]),
        _event(description="x" * 150, categories=["Music"]),
    ]

    results = await description_quality.run(events, run_id=TEST_RUN_ID)

    assert len(results) == 2
    for event in results:
        assert event.description_quality_score >= 0.0
        assert isinstance(event.low_information, bool)


def test_config_exposes_description_quality_threshold() -> None:
    assert DESCRIPTION_QUALITY_THRESHOLD == 0.3
