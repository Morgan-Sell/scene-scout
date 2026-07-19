"""
Tests for event domain models.

Covers EventCandidateLLMOutput validation, EventCandidate merge from LLM output,
NormalizedEvent validation and defaults, and required-field enforcement.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from scene_scout.models.event import (
    STRUCTURED_INGEST_CONFIDENCE,
    EventCandidate,
    EventCandidateLLMOutput,
    NormalizedEvent,
    candidate_from_structured_entry,
    compute_normalized_event_id,
    has_structured_ingest_fields,
    structured_ingest_applies,
)
from scene_scout.models.feed import RawFeedEntry
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
EXTRACTED_AT = datetime(1993, 7, 4, 12, 0, tzinfo=timezone.utc)
NORMALIZED_AT = datetime(1993, 7, 4, 18, 0, tzinfo=timezone.utc)
EVENT_TITLE = "The Great Bambino Night"
EVENT_DATE = "Sat, Jul 4 1993"
EVENT_VENUE = "The Sandlot"
EVENT_ID = compute_normalized_event_id(EVENT_TITLE, EVENT_DATE, EVENT_VENUE)


def _valid_llm_output(**overrides: object) -> EventCandidateLLMOutput:
    payload = {
        "title": "The Great Bambino Night",
        "date": "Sat, Jul 4 1993",
        "time": "6:00 PM",
        "venue": "The Sandlot",
        "neighborhood": "San Fernando Valley",
        "city": "Los Angeles",
        "url": "https://example.com/great-bambino-night",
        "price": "Free",
        "description": "Legends retell the Babe Ruth story under the floodlights.",
        "categories": ["Baseball", "Legends"],
        "is_event": True,
        "extraction_confidence": 0.92,
    }
    payload.update(overrides)
    return EventCandidateLLMOutput.model_validate(payload)


def _valid_candidate(**overrides: object) -> EventCandidate:
    llm_fields = {
        "title",
        "date",
        "time",
        "venue",
        "neighborhood",
        "city",
        "url",
        "price",
        "description",
        "categories",
        "is_event",
        "extraction_confidence",
    }
    llm_overrides = {k: overrides[k] for k in llm_fields if k in overrides}
    llm_output = _valid_llm_output(**llm_overrides)
    extracted_at = overrides.get("extracted_at", EXTRACTED_AT)
    assert isinstance(extracted_at, datetime)
    return EventCandidate.from_llm_output(
        llm_output,
        source_feed=str(overrides.get("source_feed", SANDLOT_FEED)),
        run_id=str(overrides.get("run_id", TEST_RUN_ID)),
        extracted_at=extracted_at,
    )


def test_event_candidate_llm_output_validates_full_payload() -> None:
    output = _valid_llm_output()

    assert output.title == "The Great Bambino Night"
    assert output.is_event is True
    assert output.extraction_confidence == 0.92
    assert output.categories == ["Baseball", "Legends"]


def test_event_candidate_from_llm_output_merges_metadata() -> None:
    llm_output = _valid_llm_output()
    candidate = EventCandidate.from_llm_output(
        llm_output,
        source_feed=SANDLOT_FEED,
        run_id=TEST_RUN_ID,
        extracted_at=EXTRACTED_AT,
    )

    assert candidate.title == llm_output.title
    assert candidate.source_feed == SANDLOT_FEED
    assert candidate.run_id == TEST_RUN_ID
    assert candidate.extracted_at == EXTRACTED_AT


def test_event_candidate_accepts_none_for_optional_fields() -> None:
    candidate = _valid_candidate(
        date=None,
        time=None,
        venue=None,
        neighborhood=None,
        price=None,
        description=None,
        categories=[],
        is_event=False,
        extraction_confidence=0.15,
    )

    assert candidate.date is None
    assert candidate.time is None
    assert candidate.venue is None
    assert candidate.neighborhood is None
    assert candidate.price is None
    assert candidate.description is None
    assert candidate.is_event is False


def test_event_candidate_llm_output_requires_is_event() -> None:
    payload = _valid_llm_output().model_dump()
    del payload["is_event"]

    with pytest.raises(ValidationError, match="is_event"):
        EventCandidateLLMOutput.model_validate(payload)


def test_event_candidate_llm_output_requires_extraction_confidence() -> None:
    payload = _valid_llm_output().model_dump()
    del payload["extraction_confidence"]

    with pytest.raises(ValidationError, match="extraction_confidence"):
        EventCandidateLLMOutput.model_validate(payload)


def test_event_candidate_llm_output_rejects_confidence_out_of_range() -> None:
    with pytest.raises(ValidationError, match="extraction_confidence"):
        _valid_llm_output(extraction_confidence=1.5)

    with pytest.raises(ValidationError, match="extraction_confidence"):
        _valid_llm_output(extraction_confidence=-0.1)


def test_event_candidate_llm_output_rejects_missing_required_string_fields() -> None:
    payload = _valid_llm_output().model_dump()
    del payload["title"]

    with pytest.raises(ValidationError, match="title"):
        EventCandidateLLMOutput.model_validate(payload)


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


def test_compute_normalized_event_id_is_stable_sha256() -> None:
    event_id = compute_normalized_event_id(EVENT_TITLE, EVENT_DATE, EVENT_VENUE)

    assert event_id == EVENT_ID
    assert len(event_id) == 64
    assert event_id == compute_normalized_event_id(EVENT_TITLE, EVENT_DATE, EVENT_VENUE)


def test_normalized_event_validates_full_payload() -> None:
    event = _valid_normalized_event(
        neighborhood="San Fernando Valley",
        price_cents=0,
        categories=["Baseball", "Legends"],
        end_datetime=datetime(1993, 7, 4, 22, 0, tzinfo=timezone.utc),
    )

    assert event.id == EVENT_ID
    assert event.title == EVENT_TITLE
    assert event.source_feeds == [SANDLOT_FEED]
    assert event.source_count == 1
    assert event.best_source_feed == SANDLOT_FEED
    assert event.source_quality_score == 0.8
    assert event.description_quality_score == 0.75
    assert event.low_information is False


def test_normalized_event_applies_defaults_for_optional_fields() -> None:
    event = NormalizedEvent(
        id=EVENT_ID,
        title=EVENT_TITLE,
        start_datetime=NORMALIZED_AT,
        venue=EVENT_VENUE,
        city="Los Angeles",
        url="https://example.com/great-bambino-night",
        is_free=True,
        description="Legends retell the Babe Ruth story.",
    )

    assert event.end_datetime is None
    assert event.neighborhood is None
    assert event.price_cents is None
    assert event.categories == []
    assert event.source_feeds == []
    assert event.source_count == 1
    assert event.best_source_feed == ""
    assert event.source_quality_score == 0.0
    assert event.description_quality_score == 0.0
    assert event.low_information is False
    assert event.run_id == ""
    assert event.normalized_at is None


def test_normalized_event_requires_core_fields() -> None:
    payload = _valid_normalized_event().model_dump()
    required_fields = (
        "id",
        "title",
        "start_datetime",
        "venue",
        "city",
        "url",
        "description",
    )
    for field in required_fields:
        invalid = dict(payload)
        del invalid[field]
        with pytest.raises(ValidationError, match=field):
            NormalizedEvent.model_validate(invalid)


def test_normalized_event_requires_is_free() -> None:
    payload = _valid_normalized_event().model_dump()
    del payload["is_free"]

    with pytest.raises(ValidationError, match="is_free"):
        NormalizedEvent.model_validate(payload)


def test_normalized_event_rejects_quality_scores_out_of_range() -> None:
    with pytest.raises(ValidationError, match="quality scores"):
        _valid_normalized_event(source_quality_score=1.5)

    with pytest.raises(ValidationError, match="quality scores"):
        _valid_normalized_event(description_quality_score=-0.1)


def test_normalized_event_rejects_source_count_below_one() -> None:
    with pytest.raises(ValidationError, match="source_count"):
        _valid_normalized_event(source_count=0)


def test_normalized_event_accepts_multi_feed_provenance() -> None:
    rival_feed = "rival-neighborhood-league"
    event = _valid_normalized_event(
        source_feeds=[SANDLOT_FEED, rival_feed],
        source_count=2,
        best_source_feed=rival_feed,
        source_quality_score=0.95,
    )

    assert event.source_feeds == [SANDLOT_FEED, rival_feed]
    assert event.source_count == 2
    assert event.best_source_feed == rival_feed


def _structured_raw_entry(**overrides: object) -> RawFeedEntry:
    payload = {
        "feed_id": SANDLOT_FEED,
        "feed_name": "Structured API Feed",
        "source_url": "https://example.com/api",
        "run_id": TEST_RUN_ID,
        "source_type": "api",
        "title": EVENT_TITLE,
        "link": "https://example.com/great-bambino-night",
        "description": "Legends retell the Babe Ruth story.",
        "published_raw": "2026-07-18T19:00:00",
        "event_venue": EVENT_VENUE,
        "event_city": "Los Angeles",
        "fetched_at": EXTRACTED_AT,
    }
    payload.update(overrides)
    return RawFeedEntry.model_validate(payload)


def test_structured_ingest_applies_to_api_and_ical_only() -> None:
    api_entry = _structured_raw_entry(source_type="api")
    ical_entry = _structured_raw_entry(source_type="ical")
    rss_entry = _structured_raw_entry(source_type="rss")
    scrape_entry = _structured_raw_entry(source_type="scrape")

    assert structured_ingest_applies(api_entry) is True
    assert structured_ingest_applies(ical_entry) is True
    assert structured_ingest_applies(rss_entry) is False
    assert structured_ingest_applies(scrape_entry) is False
    assert (
        structured_ingest_applies(scrape_entry, scrape_structured_ingest=True) is True
    )


def test_has_structured_ingest_fields_requires_title_venue_city_date_url() -> None:
    complete = _structured_raw_entry()
    assert has_structured_ingest_fields(complete, feed_city="New York") is True

    assert (
        has_structured_ingest_fields(
            _structured_raw_entry(event_venue=None),
            feed_city="New York",
        )
        is False
    )
    assert (
        has_structured_ingest_fields(
            _structured_raw_entry(link="not-a-url"),
            feed_city="New York",
        )
        is False
    )
    assert (
        has_structured_ingest_fields(
            _structured_raw_entry(event_city=None),
            feed_city="",
        )
        is False
    )
    assert (
        has_structured_ingest_fields(
            _structured_raw_entry(event_city=None),
            feed_city="New York",
        )
        is True
    )


def test_candidate_from_structured_entry_maps_adapter_fields() -> None:
    entry = _structured_raw_entry()
    candidate = candidate_from_structured_entry(
        entry,
        feed_city="New York",
        run_id=TEST_RUN_ID,
        extracted_at=EXTRACTED_AT,
    )

    assert candidate is not None
    assert candidate.title == EVENT_TITLE
    assert candidate.venue == EVENT_VENUE
    assert candidate.city == "Los Angeles"
    assert candidate.url == entry.link
    assert candidate.date == "2026-07-18T19:00:00"
    assert candidate.is_event is True
    assert candidate.extraction_confidence == STRUCTURED_INGEST_CONFIDENCE
    assert candidate.source_feed == SANDLOT_FEED
    assert candidate.run_id == TEST_RUN_ID
    assert candidate.extracted_at == EXTRACTED_AT


def test_candidate_from_structured_entry_infers_categories_when_missing() -> None:
    entry = _structured_raw_entry()
    entry = entry.model_copy(
        update={"title": "Jazz Night at the Sandlot", "categories": []}
    )
    candidate = candidate_from_structured_entry(
        entry,
        feed_city="New York",
        run_id=TEST_RUN_ID,
        extracted_at=EXTRACTED_AT,
    )

    assert candidate is not None
    assert "Jazz" in candidate.categories
