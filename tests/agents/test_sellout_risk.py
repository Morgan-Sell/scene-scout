"""
Tests for the Sell-Out Risk Agent.

Covers venue size, price, date proximity, description language, performer affinity,
risk band assignment, distribution logging, and end-to-end run().
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from scene_scout.agents import sellout_risk
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.ranking import RankedEvent
from tests.conftest import TEST_RUN_ID

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
EVENT_TIME = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)


def _event(**overrides: object) -> EnrichedEvent:
    payload = {
        "id": "sellout-event-1",
        "title": "Sandlot Summer Show",
        "start_datetime": EVENT_TIME,
        "venue": "The Sandlot Club",
        "city": "Los Angeles",
        "url": "https://example.com/sandlot-show",
        "is_free": False,
        "price_cents": 2500,
        "description": "An intimate show under the floodlights.",
        "categories": ["Music"],
        "source_feeds": ["sandlot-pickup-league"],
        "source_count": 1,
        "best_source_feed": "sandlot-pickup-league",
        "source_quality_score": 0.8,
        "description_quality_score": 0.85,
        "low_information": False,
        "run_id": TEST_RUN_ID,
        "normalized_at": NOW,
        "top_performer_affinity": 0.5,
        "vibe_tags": ["intimate"],
        "performers": [],
    }
    payload.update(overrides)
    return EnrichedEvent.model_validate(payload)


def _ranked(**event_overrides: object) -> RankedEvent:
    event = _event(**event_overrides)
    return RankedEvent(
        event=event,
        score=0.75,
        score_breakdown={
            "category_fit": 0.8,
            "vibe_fit": 0.7,
            "semantic_similarity": 0.0,
            "performer_affinity": event.top_performer_affinity,
            "location": 1.0,
            "novelty": 1.0,
            "source_quality": 0.8,
            "source_coverage": 0.33,
            "description_quality": 0.85,
        },
        explanation="Strong pick for the week.",
        run_id=TEST_RUN_ID,
    )


def test_classify_venue_size_category() -> None:
    assert sellout_risk.classify_venue_size_category("The Forum Arena") == "large"
    assert sellout_risk.classify_venue_size_category("Echo Park Club") == "small"
    assert sellout_risk.classify_venue_size_category("Greek Theatre") == "medium"
    assert sellout_risk.classify_venue_size_category("Mystery Spot") == "unknown"


def test_score_venue_size_orders_capacity_pressure() -> None:
    club_score = sellout_risk.score_venue_size("Neighborhood Club")
    theater_score = sellout_risk.score_venue_size("City Theater")
    arena_score = sellout_risk.score_venue_size("Downtown Arena")

    assert club_score > theater_score > arena_score


def test_score_price_free_and_low_price_raise_pressure() -> None:
    free_score = sellout_risk.score_price(price_cents=None, is_free=True)
    low_score = sellout_risk.score_price(price_cents=1500, is_free=False)
    high_score = sellout_risk.score_price(price_cents=8000, is_free=False)

    assert free_score == pytest.approx(0.85)
    assert low_score == pytest.approx(0.75)
    assert high_score == pytest.approx(0.35)


def test_score_date_proximity_increases_as_event_nears() -> None:
    soon = NOW + timedelta(days=1)
    far = NOW + timedelta(days=45)
    soon_score = sellout_risk.score_date_proximity(soon, now=NOW)
    far_score = sellout_risk.score_date_proximity(far, now=NOW)

    assert soon_score > far_score


def test_score_description_language_detects_urgency_phrases() -> None:
    urgent = "Limited tickets remain — this show is selling fast."
    relaxed = "Plenty of seating available. Walk-up welcome."
    urgent_score = sellout_risk.score_description_language(urgent)
    relaxed_score = sellout_risk.score_description_language(relaxed)

    assert urgent_score > relaxed_score


def test_score_performer_demand_tracks_top_performer_affinity() -> None:
    assert sellout_risk.score_performer_demand(0.9) == pytest.approx(0.9)
    assert sellout_risk.score_performer_demand(0.2) == pytest.approx(0.2)


def test_classify_risk_maps_score_to_bands() -> None:
    assert sellout_risk.classify_risk(0.80) == "high"
    assert sellout_risk.classify_risk(0.50) == "medium"
    assert sellout_risk.classify_risk(0.20) == "low"


def test_classify_event_risk_high_for_small_free_soon_urgent_headliner() -> None:
    ranked = _ranked(
        venue="Basement Club",
        is_free=True,
        start_datetime=NOW + timedelta(days=1),
        description="Final release — selling fast before doors open.",
        top_performer_affinity=0.95,
    )

    assert sellout_risk.classify_event_risk(ranked, now=NOW) == "high"


def test_classify_event_risk_low_for_large_paid_distant_event() -> None:
    ranked = _ranked(
        venue="Memorial Coliseum Stadium",
        is_free=False,
        price_cents=12000,
        start_datetime=NOW + timedelta(days=60),
        description="Tickets available all month.",
        top_performer_affinity=0.1,
    )

    assert sellout_risk.classify_event_risk(ranked, now=NOW) == "low"


def test_urgency_note_for_risk_only_on_high_band() -> None:
    assert (
        sellout_risk.urgency_note_for_risk("high")
        == sellout_risk.HIGH_RISK_URGENCY_NOTE
    )
    assert sellout_risk.urgency_note_for_risk("medium") is None
    assert sellout_risk.urgency_note_for_risk("low") is None


def test_annotate_event_risk_sets_urgency_note_for_high_events() -> None:
    ranked = _ranked(
        venue="Basement Club",
        is_free=True,
        start_datetime=NOW + timedelta(days=1),
        description="Final release — selling fast before doors open.",
        top_performer_affinity=0.95,
    )

    updated = sellout_risk.annotate_event_risk(ranked, now=NOW)

    assert updated.sellout_risk == "high"
    assert updated.sellout_urgency_note == sellout_risk.HIGH_RISK_URGENCY_NOTE


def test_annotate_event_risk_clears_urgency_note_for_low_events() -> None:
    ranked = _ranked(
        venue="Memorial Coliseum Stadium",
        is_free=False,
        price_cents=12000,
        start_datetime=NOW + timedelta(days=60),
        description="Tickets available all month.",
        top_performer_affinity=0.1,
    ).model_copy(
        update={
            "sellout_risk": "high",
            "sellout_urgency_note": "Stale note from an earlier run.",
        },
    )

    updated = sellout_risk.annotate_event_risk(ranked, now=NOW)

    assert updated.sellout_risk == "low"
    assert updated.sellout_urgency_note is None


@pytest.mark.asyncio
async def test_run_assigns_risk_to_every_ranked_event() -> None:
    events = [
        _ranked(id="high-risk"),
        _ranked(
            id="low-risk",
            venue="Memorial Coliseum Stadium",
            is_free=False,
            price_cents=15000,
            start_datetime=NOW + timedelta(days=90),
            description="Plenty of seating available.",
            top_performer_affinity=0.05,
        ),
    ]

    results = await sellout_risk.run(events, TEST_RUN_ID, now=NOW)

    assert len(results) == 2
    assert all(item.sellout_risk in {"low", "medium", "high"} for item in results)
    assert results[0].sellout_risk is not None
    assert results[1].sellout_risk is not None
    high_result = next(item for item in results if item.sellout_risk == "high")
    low_result = next(item for item in results if item.sellout_risk == "low")
    assert high_result.sellout_urgency_note == sellout_risk.HIGH_RISK_URGENCY_NOTE
    assert low_result.sellout_urgency_note is None


@pytest.mark.asyncio
async def test_run_logs_distribution() -> None:
    events = [
        _ranked(
            id="likely-high",
            venue="Backyard Club",
            is_free=True,
            start_datetime=NOW + timedelta(days=1),
            description="Selling fast — limited tickets.",
            top_performer_affinity=0.95,
        ),
        _ranked(
            id="likely-low",
            venue="Memorial Coliseum Stadium",
            is_free=False,
            price_cents=15000,
            start_datetime=NOW + timedelta(days=90),
            description="Tickets available all month.",
            top_performer_affinity=0.05,
        ),
    ]

    with patch.object(sellout_risk, "get_logger") as mock_get_logger:
        mock_logger = mock_get_logger.return_value
        results = await sellout_risk.run(events, TEST_RUN_ID, now=NOW)

    distribution_calls = [
        call
        for call in mock_logger.info.call_args_list
        if call.args and call.args[0] == "Sell-out risk distribution"
    ]
    assert len(distribution_calls) == 1
    distribution = distribution_calls[0].kwargs["data"]["distribution"]
    assert distribution == sellout_risk.compute_risk_distribution(results)
    assert sum(distribution.values()) == len(results)


def test_compute_risk_distribution_counts_bands() -> None:
    events = [
        _ranked().model_copy(update={"sellout_risk": "high"}),
        _ranked().model_copy(update={"sellout_risk": "medium"}),
        _ranked().model_copy(update={"sellout_risk": "low"}),
        _ranked().model_copy(update={"sellout_risk": "low"}),
    ]

    assert sellout_risk.compute_risk_distribution(events) == {
        "low": 2,
        "medium": 1,
        "high": 1,
    }
