"""
Tests for the ranking explanation prompt template.

Covers Jinja2 rendering with event fields, full score breakdown (including
source_coverage), and required template variables.
"""

from __future__ import annotations

from datetime import datetime, timezone

import jinja2
import pytest

from scene_scout.models.enrichment import EnrichedEvent, PerformerInfo
from scene_scout.services.prompt_loader import render_prompt
from tests.conftest import TEST_RUN_ID

NORMALIZED_AT = datetime(1993, 7, 4, 18, 0, tzinfo=timezone.utc)

SCORE_BREAKDOWN = {
    "category_fit": 0.85,
    "vibe_fit": 0.72,
    "semantic_similarity": 0.0,
    "performer_affinity": 0.65,
    "location": 0.80,
    "novelty": 0.90,
    "source_quality": 0.75,
    "source_coverage": 0.67,
    "description_quality": 0.88,
}


def _sample_event(**overrides: object) -> EnrichedEvent:
    payload = {
        "id": "event-123",
        "title": "Silver Lake Jazz Night",
        "start_datetime": NORMALIZED_AT,
        "venue": "The Sandlot",
        "neighborhood": "Silver Lake",
        "city": "Los Angeles",
        "url": "https://example.com/jazz-night",
        "is_free": False,
        "description": "An intimate jazz set under the floodlights.",
        "categories": ["Jazz"],
        "source_feeds": ["la_weekly_events", "kcrw_events"],
        "source_count": 2,
        "best_source_feed": "kcrw_events",
        "source_quality_score": 0.8,
        "description_quality_score": 0.88,
        "low_information": False,
        "run_id": TEST_RUN_ID,
        "normalized_at": NORMALIZED_AT,
        "performers": [
            PerformerInfo(
                name="Kamasi Washington",
                entity_type="musician",
                confidence=0.9,
                affinity_score=0.85,
            )
        ],
        "vibe_tags": ["intimate", "late-night"],
        "neighborhood_context": "Walkable cafes and record shops within a few blocks.",
    }
    payload.update(overrides)
    return EnrichedEvent.model_validate(payload)


def _render(**overrides: object) -> str:
    kwargs = {
        "event": _sample_event(),
        "score_breakdown": SCORE_BREAKDOWN,
        "total_score": 0.81,
        "stated_interests": ["jazz", "outdoor"],
        "vibe_preferences": ["intimate", "outdoor"],
    }
    kwargs.update(overrides)
    return render_prompt("ranking_explanation", **kwargs)


def test_ranking_explanation_prompt_renders_event_fields() -> None:
    rendered = _render()

    assert "Silver Lake Jazz Night" in rendered
    assert "The Sandlot (Silver Lake)" in rendered
    assert "Los Angeles" in rendered
    assert "Jazz" in rendered
    assert "intimate, late-night" in rendered
    assert "Kamasi Washington" in rendered
    assert "2 independent listings" in rendered
    assert "Walkable cafes and record shops" in rendered


def test_ranking_explanation_prompt_renders_full_score_breakdown() -> None:
    rendered = _render()

    for component, value in SCORE_BREAKDOWN.items():
        assert component in rendered
        assert f"{value:.2f}" in rendered

    assert "source_coverage: 0.67" in rendered
    assert "Total score: 0.81" in rendered


def test_ranking_explanation_prompt_renders_user_profile_signals() -> None:
    rendered = _render()

    assert "jazz, outdoor" in rendered
    assert "intimate, outdoor" in rendered
    assert "Do not write generic lines" in rendered
    assert '"explanation"' in rendered


def test_ranking_explanation_prompt_renders_empty_optional_event_fields() -> None:
    rendered = _render(
        event=_sample_event(
            performers=[],
            vibe_tags=[],
            categories=[],
            neighborhood=None,
            neighborhood_context=None,
            source_count=1,
        ),
        stated_interests=[],
        vibe_preferences=[],
    )

    assert "Categories: (none)" in rendered
    assert "Vibe tags: (none)" in rendered
    assert "Performers: (none)" in rendered
    assert "1 independent listing" in rendered
    assert "Stated interests: (none)" in rendered


def test_ranking_explanation_prompt_requires_event_and_score_breakdown() -> None:
    with pytest.raises(jinja2.UndefinedError):
        render_prompt("ranking_explanation", score_breakdown=SCORE_BREAKDOWN)

    with pytest.raises(jinja2.UndefinedError):
        render_prompt("ranking_explanation", event=_sample_event())
