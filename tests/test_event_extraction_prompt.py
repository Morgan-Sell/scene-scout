"""
Tests for the event extraction prompt template.

Covers Jinja2 rendering with a sample RawFeedEntry and schema alignment with
EventCandidateLLMOutput (merged into EventCandidate by the agent).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import jinja2
import pytest
from pydantic import ValidationError

from scene_scout.models.event import EventCandidate, EventCandidateLLMOutput
from scene_scout.models.feed import RawFeedEntry
from scene_scout.services.prompt_loader import render_prompt
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED_ID = "sandlot-pickup-league"
EXTRACTED_AT = datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc)


def _sample_raw_entry(**overrides: object) -> RawFeedEntry:
    payload = {
        "feed_id": SANDLOT_FEED_ID,
        "feed_name": "Mr. Mertle's Events Feed",
        "source_url": "https://example.com/sandlot-feed.xml",
        "run_id": TEST_RUN_ID,
        "title": "The Great Bambino Night",
        "link": "https://example.com/great-bambino-night",
        "description": "Sandlot legends retell the Babe Ruth story.",
        "published_raw": "Fri, 06 Jun 2025 20:00:00 +0000",
        "author": "Ham Porter",
        "categories": ["Baseball", "Legends"],
        "fetched_at": datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return RawFeedEntry.model_validate(payload)


def test_event_extraction_prompt_renders_sample_raw_feed_entry() -> None:
    entry = _sample_raw_entry()
    rendered = render_prompt("event_extraction", entry=entry)

    assert "The Great Bambino Night" in rendered
    assert "https://example.com/great-bambino-night" in rendered
    assert "Fri, 06 Jun 2025 20:00:00 +0000" in rendered
    assert "Ham Porter" in rendered
    assert "Baseball, Legends" in rendered
    assert SANDLOT_FEED_ID in rendered
    assert "Sandlot legends retell the Babe Ruth story." in rendered
    assert "extraction_confidence" in rendered
    assert "is_event" in rendered


def test_event_extraction_prompt_renders_null_optional_fields() -> None:
    entry = _sample_raw_entry(
        title=None,
        link=None,
        description=None,
        published_raw=None,
        author=None,
        categories=[],
    )
    rendered = render_prompt("event_extraction", entry=entry)

    assert "(not provided)" in rendered
    assert "Categories: (none)" in rendered


def test_event_extraction_prompt_requires_entry_variable() -> None:
    with pytest.raises(jinja2.UndefinedError):
        render_prompt("event_extraction")


def test_llm_json_validates_as_event_candidate_llm_output() -> None:
    llm_payload = {
        "title": "The Great Bambino Night",
        "date": "Fri, 06 Jun 2025",
        "time": "8:00 PM",
        "venue": "The Sandlot",
        "neighborhood": "San Fernando Valley",
        "city": "Los Angeles",
        "url": "https://example.com/great-bambino-night",
        "price": "Free",
        "description": "Legends retell the Babe Ruth story under the floodlights.",
        "categories": ["Baseball", "Legends"],
        "is_event": True,
        "extraction_confidence": 0.91,
    }
    llm_output = EventCandidateLLMOutput.model_validate(llm_payload)
    candidate = EventCandidate.from_llm_output(
        llm_output,
        source_feed=SANDLOT_FEED_ID,
        run_id=TEST_RUN_ID,
        extracted_at=EXTRACTED_AT,
    )

    assert candidate.title == llm_payload["title"]
    assert candidate.source_feed == SANDLOT_FEED_ID
    assert json.loads(json.dumps(llm_payload)) == llm_payload


def test_llm_json_missing_required_field_fails_llm_output_validation() -> None:
    llm_payload = {
        "title": "Pool Party at the Rec Center",
        "date": None,
        "time": None,
        "venue": None,
        "neighborhood": None,
        "city": "Los Angeles",
        "url": "https://example.com/pool-party",
        "price": None,
        "description": None,
        "categories": [],
        # is_event omitted intentionally
        "extraction_confidence": 0.4,
    }

    with pytest.raises(ValidationError, match="is_event"):
        EventCandidateLLMOutput.model_validate(llm_payload)
