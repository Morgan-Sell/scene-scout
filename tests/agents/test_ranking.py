"""
Tests for the Ranking Agent.

Covers deterministic scoring, source_coverage calculation, component isolation,
wildcard assignment, explanation fallback, and excluded-category filtering.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scene_scout.agents import ranking
from scene_scout.config import RANKING_COMPONENT_WEIGHTS, SOURCE_COVERAGE_MAX
from scene_scout.models.enrichment import EnrichedEvent, PerformerInfo
from scene_scout.models.ranking import RankedEvent, RankingExplanationLLMOutput
from scene_scout.models.user import UserProfile
from scene_scout.ranking_config import (
    WILDCARD_MIN_NOVELTY,
    WILDCARD_SCORE_MAX,
    WILDCARD_SCORE_MIN,
)
from scene_scout.services.llm import LLMValidationError
from tests.conftest import TEST_RUN_ID

PROFILE_TIMESTAMP = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
EVENT_TIME = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)


def _profile(**overrides: object) -> UserProfile:
    payload = {
        "user_id": "user-123",
        "name": "Morgan",
        "email": "morgan@example.com",
        "stated_interests": ["jazz"],
        "preferred_neighborhoods": ["Silver Lake"],
        "category_weights": {"jazz": 0.9, "rock": 0.2},
        "vibe_preferences": ["intimate", "outdoor"],
        "excluded_categories": ["nightclub"],
        "created_at": PROFILE_TIMESTAMP,
        "last_updated": PROFILE_TIMESTAMP,
    }
    payload.update(overrides)
    return UserProfile.model_validate(payload)


def _event(**overrides: object) -> EnrichedEvent:
    payload = {
        "id": "event-jazz-1",
        "title": "Silver Lake Jazz Night",
        "start_datetime": EVENT_TIME,
        "venue": "The Sandlot",
        "neighborhood": "Silver Lake",
        "city": "Los Angeles",
        "url": "https://example.com/jazz-night",
        "is_free": False,
        "description": "An intimate jazz set under the floodlights.",
        "categories": ["Jazz"],
        "source_feeds": ["la_weekly_events"],
        "source_count": 1,
        "best_source_feed": "la_weekly_events",
        "source_quality_score": 0.8,
        "description_quality_score": 0.9,
        "low_information": False,
        "run_id": TEST_RUN_ID,
        "normalized_at": PROFILE_TIMESTAMP,
        "top_performer_affinity": 0.7,
        "vibe_tags": ["intimate"],
        "performers": [
            PerformerInfo(
                name="Kamasi Washington",
                entity_type="musician",
                confidence=0.9,
                affinity_score=0.85,
            )
        ],
    }
    payload.update(overrides)
    return EnrichedEvent.model_validate(payload)


def test_compute_source_coverage_for_one_two_three_sources() -> None:
    assert ranking.compute_source_coverage(1) == pytest.approx(1 / SOURCE_COVERAGE_MAX)
    assert ranking.compute_source_coverage(2) == pytest.approx(2 / SOURCE_COVERAGE_MAX)
    assert ranking.compute_source_coverage(3) == pytest.approx(1.0)
    assert ranking.compute_source_coverage(5) == pytest.approx(1.0)


def test_compute_category_fit_uses_profile_weights() -> None:
    profile = _profile()
    event = _event(categories=["Jazz"])

    assert ranking.compute_category_fit(event, profile) == pytest.approx(0.9)


def test_compute_vibe_fit_counts_preference_overlap() -> None:
    profile = _profile()
    event = _event(vibe_tags=["intimate", "late-night"])

    assert ranking.compute_vibe_fit(event, profile) == pytest.approx(0.5)


def test_compute_location_fit_matches_preferred_neighborhood() -> None:
    profile = _profile()
    event = _event(neighborhood="Silver Lake")

    assert ranking.compute_location_fit(event, profile) == pytest.approx(1.0)


def test_compute_novelty_penalizes_previous_recommendations() -> None:
    profile = _profile()
    event = _event(id="seen-before")

    score, is_previous, penalty = ranking.compute_novelty(
        event,
        profile,
        previously_recommended_ids={"seen-before"},
    )

    assert is_previous is True
    assert penalty is True
    assert score < 1.0


def test_composite_score_is_deterministic_weighted_sum() -> None:
    breakdown = {
        "category_fit": 0.8,
        "vibe_fit": 0.6,
        "semantic_similarity": 0.0,
        "performer_affinity": 0.7,
        "location": 1.0,
        "novelty": 1.0,
        "source_quality": 0.8,
        "source_coverage": 0.33,
        "description_quality": 0.9,
    }
    expected = sum(breakdown[key] * RANKING_COMPONENT_WEIGHTS[key] for key in breakdown)

    assert ranking.composite_score(breakdown) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_run_returns_ranked_events_sorted_by_score() -> None:
    profile = _profile()
    high = _event(id="high", categories=["Jazz"], vibe_tags=["intimate", "outdoor"])
    low = _event(
        id="low",
        title="Generic Listing",
        categories=["Misc"],
        vibe_tags=["touristy"],
        neighborhood="Downtown",
        top_performer_affinity=0.1,
        source_quality_score=0.3,
        description_quality_score=0.2,
    )

    mock_complete = AsyncMock(
        side_effect=lambda **kwargs: RankingExplanationLLMOutput(
            explanation=f"Because {kwargs['prompt'].splitlines()[8]}"
        )
    )

    with (
        patch("scene_scout.agents.ranking.similarity_score", return_value=0.0),
        patch("scene_scout.agents.ranking.complete", mock_complete),
        patch("scene_scout.agents.ranking.get_liked_events_collection"),
    ):
        results = await ranking.run([low, high], profile, TEST_RUN_ID)

    assert len(results) == 2
    assert results[0].score >= results[1].score
    assert results[0].event.id == "high"
    assert all(isinstance(item, RankedEvent) for item in results)
    assert mock_complete.await_count == 2


@pytest.mark.asyncio
async def test_run_skips_excluded_categories() -> None:
    profile = _profile()
    excluded = _event(id="excluded", categories=["nightclub"])

    with (
        patch("scene_scout.agents.ranking.similarity_score", return_value=0.0),
        patch(
            "scene_scout.agents.ranking.complete",
            AsyncMock(
                return_value=RankingExplanationLLMOutput(explanation="Should not run.")
            ),
        ),
        patch("scene_scout.agents.ranking.get_liked_events_collection"),
    ):
        results = await ranking.run([excluded], profile, TEST_RUN_ID)

    assert results == []


@pytest.mark.asyncio
async def test_run_uses_fallback_explanation_on_validation_error() -> None:
    profile = _profile()
    event = _event()

    with (
        patch("scene_scout.agents.ranking.similarity_score", return_value=0.0),
        patch(
            "scene_scout.agents.ranking.complete",
            AsyncMock(side_effect=LLMValidationError("invalid json")),
        ),
        patch("scene_scout.agents.ranking.get_liked_events_collection"),
    ):
        results = await ranking.run([event], profile, TEST_RUN_ID)

    assert len(results) == 1
    assert results[0].explanation == ranking.fallback_explanation(event)


def test_assign_wildcard_slots_marks_moderate_fit_high_novelty_events() -> None:
    profile = _profile()
    breakdown = ranking.compute_score_breakdown(
        _event(id="wildcard"),
        profile,
        semantic_similarity=0.0,
        previously_recommended_ids=set(),
    )[0]
    breakdown["novelty"] = max(breakdown["novelty"], WILDCARD_MIN_NOVELTY)
    score = (WILDCARD_SCORE_MIN + WILDCARD_SCORE_MAX) / 2

    ranked = [
        RankedEvent(
            event=_event(id="wildcard"),
            score=score,
            score_breakdown=breakdown,
            explanation="Wildcard candidate.",
            run_id=TEST_RUN_ID,
        ),
        RankedEvent(
            event=_event(id="top", title="Top Pick"),
            score=0.95,
            score_breakdown=breakdown,
            explanation="Top pick.",
            run_id=TEST_RUN_ID,
        ),
    ]

    updated = ranking.assign_wildcard_slots(ranked)

    wildcard_flags = {item.event.id: item.wildcard_slot for item in updated}
    assert wildcard_flags["wildcard"] is True
    assert wildcard_flags["top"] is False


def test_load_previously_recommended_event_ids_reads_history_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_dir = tmp_path / "vol-history"
    history_dir.mkdir()
    now = datetime(2026, 6, 12, tzinfo=timezone.utc)
    payload = {
        "entries": [
            {
                "event_id": "recent",
                "recommended_at": (now - timedelta(days=3)).isoformat(),
            },
            {
                "event_id": "old",
                "recommended_at": (now - timedelta(days=40)).isoformat(),
            },
        ]
    }
    (history_dir / "hard_exclude_index.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(ranking, "vol_history_dir", lambda: history_dir)

    recent_ids = ranking.load_previously_recommended_event_ids(now=now)

    assert recent_ids == {"recent"}
