"""
Tests for event domain models.

Covers EventCandidateLLMOutput validation, EventCandidate merge from LLM output,
optional null fields, and required-field enforcement.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from scene_scout.models.event import (
    EventCandidate,
    EventCandidateLLMOutput,
)
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
EXTRACTED_AT = datetime(1993, 7, 4, 12, 0, tzinfo=timezone.utc)


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
