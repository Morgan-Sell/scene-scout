"""
Tests for enrichment domain models.

Covers PerformerInfo validation, EnrichedEvent inheritance from NormalizedEvent,
enrichment field defaults, and provenance field preservation.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from scene_scout.models.enrichment import EnrichedEvent, PerformerInfo
from scene_scout.models.event import NormalizedEvent, compute_normalized_event_id
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
RIVAL_FEED = "rival-neighborhood-league"
NORMALIZED_AT = datetime(1993, 7, 4, 18, 0, tzinfo=timezone.utc)
EVENT_TITLE = "The Great Bambino Night"
EVENT_DATE = "Sat, Jul 4 1993"
EVENT_VENUE = "The Sandlot"
EVENT_ID = compute_normalized_event_id(EVENT_TITLE, EVENT_DATE, EVENT_VENUE)


def _valid_normalized_event(**overrides: object) -> NormalizedEvent:
    payload = {
        "id": EVENT_ID,
        "title": EVENT_TITLE,
        "start_datetime": NORMALIZED_AT,
        "venue": EVENT_VENUE,
        "city": "Los Angeles",
        "url": "https://example.com/great-bambino-night",
        "is_free": True,
        "description": "Legends retell the Babe Ruth story under the floodlights.",
        "source_feeds": [SANDLOT_FEED],
        "source_count": 1,
        "best_source_feed": SANDLOT_FEED,
        "source_quality_score": 0.8,
        "description_quality_score": 0.75,
        "low_information": False,
        "run_id": TEST_RUN_ID,
        "normalized_at": NORMALIZED_AT,
    }
    payload.update(overrides)
    return NormalizedEvent.model_validate(payload)


def _valid_performer(**overrides: object) -> PerformerInfo:
    payload = {
        "name": "Benny Rodriguez",
        "entity_type": "athlete",
        "genre_tags": ["base-stealing"],
        "one_line_summary": "The Jet",
        "confidence": 0.95,
        "affinity_score": 0.88,
    }
    payload.update(overrides)
    return PerformerInfo.model_validate(payload)


def test_performer_info_validates_full_payload() -> None:
    performer = _valid_performer()

    assert performer.name == "Benny Rodriguez"
    assert performer.entity_type == "athlete"
    assert performer.genre_tags == ["base-stealing"]
    assert performer.one_line_summary == "The Jet"
    assert performer.confidence == 0.95
    assert performer.affinity_score == 0.88


def test_performer_info_applies_defaults() -> None:
    performer = PerformerInfo(name="Smalls", entity_type="other")

    assert performer.genre_tags == []
    assert performer.one_line_summary is None
    assert performer.confidence == 0.0
    assert performer.affinity_score == 0.0


def test_performer_info_rejects_scores_out_of_range() -> None:
    with pytest.raises(ValidationError, match="confidence and affinity_score"):
        _valid_performer(confidence=1.5)

    with pytest.raises(ValidationError, match="confidence and affinity_score"):
        _valid_performer(affinity_score=-0.1)


def test_enriched_event_applies_enrichment_defaults() -> None:
    event = EnrichedEvent(
        id=EVENT_ID,
        title=EVENT_TITLE,
        start_datetime=NORMALIZED_AT,
        venue=EVENT_VENUE,
        city="Los Angeles",
        url="https://example.com/great-bambino-night",
        is_free=True,
        description="Legends retell the Babe Ruth story.",
    )

    assert event.performers == []
    assert event.top_performer_affinity == 0.0
    assert event.vibe_tags == []
    assert event.neighborhood_context is None
    assert event.neighborhood_confidence == 0.0
    assert event.venue_coordinates is None


def test_enriched_event_inherits_normalized_event_fields() -> None:
    normalized = _valid_normalized_event(
        neighborhood="San Fernando Valley",
        categories=["Baseball", "Legends"],
        end_datetime=datetime(1993, 7, 4, 22, 0, tzinfo=timezone.utc),
    )
    event = EnrichedEvent.model_validate(normalized.model_dump())

    assert event.id == EVENT_ID
    assert event.title == EVENT_TITLE
    assert event.neighborhood == "San Fernando Valley"
    assert event.categories == ["Baseball", "Legends"]
    assert event.source_feeds == [SANDLOT_FEED]
    assert event.source_count == 1
    assert event.best_source_feed == SANDLOT_FEED
    assert event.source_quality_score == 0.8
    assert event.description_quality_score == 0.75
    assert event.low_information is False
    assert event.run_id == TEST_RUN_ID
    assert event.normalized_at == NORMALIZED_AT


def test_enriched_event_from_normalized_preserves_provenance() -> None:
    normalized = _valid_normalized_event(
        source_feeds=[SANDLOT_FEED, RIVAL_FEED],
        source_count=2,
        best_source_feed=RIVAL_FEED,
        source_quality_score=0.95,
    )

    event = EnrichedEvent.from_normalized(normalized)

    assert event.source_feeds == [SANDLOT_FEED, RIVAL_FEED]
    assert event.source_count == 2
    assert event.best_source_feed == RIVAL_FEED
    assert event.source_quality_score == 0.95
    assert event.performers == []
    assert event.vibe_tags == []


def test_enriched_event_validates_enrichment_fields() -> None:
    performer = _valid_performer()
    event = EnrichedEvent(
        id=EVENT_ID,
        title=EVENT_TITLE,
        start_datetime=NORMALIZED_AT,
        venue=EVENT_VENUE,
        city="Los Angeles",
        url="https://example.com/great-bambino-night",
        is_free=True,
        description="Legends retell the Babe Ruth story.",
        performers=[performer],
        top_performer_affinity=0.88,
        vibe_tags=["outdoor", "nostalgic"],
        neighborhood_context="Classic suburban sandlot vibes.",
        neighborhood_confidence=0.82,
        venue_coordinates=(34.05, -118.25),
    )

    assert event.performers == [performer]
    assert event.top_performer_affinity == 0.88
    assert event.vibe_tags == ["outdoor", "nostalgic"]
    assert event.neighborhood_context == "Classic suburban sandlot vibes."
    assert event.neighborhood_confidence == 0.82
    assert event.venue_coordinates == (34.05, -118.25)


def test_enriched_event_rejects_enrichment_scores_out_of_range() -> None:
    base = {
        "id": EVENT_ID,
        "title": EVENT_TITLE,
        "start_datetime": NORMALIZED_AT,
        "venue": EVENT_VENUE,
        "city": "Los Angeles",
        "url": "https://example.com/great-bambino-night",
        "is_free": True,
        "description": "Legends retell the Babe Ruth story.",
    }

    with pytest.raises(ValidationError, match="top_performer_affinity"):
        EnrichedEvent.model_validate({**base, "top_performer_affinity": 1.5})

    with pytest.raises(ValidationError, match="neighborhood_confidence"):
        EnrichedEvent.model_validate({**base, "neighborhood_confidence": -0.1})
