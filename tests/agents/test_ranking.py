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

GOLDEN_DIR = Path(__file__).parent.parent / "fixtures" / "golden" / "ranking"

PROFILE_KEYS = ("jazz_lover", "family_explorer", "nightlife_seeker")
EVENT_TYPES = (
    "strong_match",
    "weak_match",
    "single_source",
    "triple_source",
    "wildcard_candidate",
)
GOLDEN_FIXTURE_NAMES = [
    f"{profile}_{event_type}" for profile in PROFILE_KEYS for event_type in EVENT_TYPES
]

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


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _profile_from_golden(data: dict) -> UserProfile:
    return UserProfile.model_validate(data["profile"])


def _event_from_golden(data: dict) -> EnrichedEvent:
    return EnrichedEvent.model_validate(data["event"])


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


def _assert_score_approx(actual: float, expected: float) -> None:
    assert actual == pytest.approx(expected, abs=1e-6, rel=1e-6)


@pytest.mark.parametrize("fixture_name", GOLDEN_FIXTURE_NAMES)
def test_golden_fixture_deterministic_scoring(fixture_name: str) -> None:
    """Regression: fixed profile/event pairs yield stable score breakdowns."""
    golden = _load_golden(fixture_name)
    profile = _profile_from_golden(golden)
    event = _event_from_golden(golden)
    previous_ids = set(golden["previously_recommended_ids"])

    breakdown, is_previous, penalty = ranking.compute_score_breakdown(
        event,
        profile,
        semantic_similarity=golden["semantic_similarity"],
        previously_recommended_ids=previous_ids,
    )
    score = ranking.composite_score(breakdown)

    for component, expected in golden["expected_score_breakdown"].items():
        _assert_score_approx(breakdown[component], expected)
    _assert_score_approx(score, golden["expected_score"])
    assert is_previous is golden["expected_is_previously_recommended"]
    assert penalty is golden["expected_novelty_penalty_applied"]


@pytest.mark.parametrize("profile_key", PROFILE_KEYS)
def test_golden_source_coverage_isolates_feed_count(profile_key: str) -> None:
    """Only source_coverage shifts when feed count changes on a fixed event."""
    golden = _load_golden(f"{profile_key}_single_source")
    profile = _profile_from_golden(golden)
    base_event = _event_from_golden(golden)
    one_feed = base_event.model_copy(
        update={"source_count": 1, "source_feeds": ["feed_a"]},
    )
    two_feeds = base_event.model_copy(
        update={"source_count": 2, "source_feeds": ["feed_a", "feed_b"]},
    )
    three_feeds = base_event.model_copy(
        update={
            "source_count": 3,
            "source_feeds": ["feed_a", "feed_b", "feed_c"],
        },
    )

    one_breakdown, _, _ = ranking.compute_score_breakdown(
        one_feed,
        profile,
        semantic_similarity=0.0,
        previously_recommended_ids=set(),
    )
    two_breakdown, _, _ = ranking.compute_score_breakdown(
        two_feeds,
        profile,
        semantic_similarity=0.0,
        previously_recommended_ids=set(),
    )
    three_breakdown, _, _ = ranking.compute_score_breakdown(
        three_feeds,
        profile,
        semantic_similarity=0.0,
        previously_recommended_ids=set(),
    )

    assert one_breakdown["source_coverage"] == pytest.approx(1 / SOURCE_COVERAGE_MAX)
    assert two_breakdown["source_coverage"] == pytest.approx(2 / SOURCE_COVERAGE_MAX)
    assert three_breakdown["source_coverage"] == pytest.approx(1.0)

    for component in one_breakdown:
        if component == "source_coverage":
            continue
        assert one_breakdown[component] == pytest.approx(two_breakdown[component])
        assert one_breakdown[component] == pytest.approx(three_breakdown[component])


def test_score_component_isolation_category_fit() -> None:
    """Changing categories affects category_fit without moving unrelated components."""
    golden = _load_golden("jazz_lover_strong_match")
    profile = _profile_from_golden(golden)
    matched = _event_from_golden(golden)
    mismatched = matched.model_copy(update={"categories": ["Classical"]})

    matched_breakdown, _, _ = ranking.compute_score_breakdown(
        matched,
        profile,
        semantic_similarity=0.0,
        previously_recommended_ids=set(),
    )
    mismatched_breakdown, _, _ = ranking.compute_score_breakdown(
        mismatched,
        profile,
        semantic_similarity=0.0,
        previously_recommended_ids=set(),
    )

    assert matched_breakdown["category_fit"] > mismatched_breakdown["category_fit"]
    for component in matched_breakdown:
        if component in {"category_fit", "novelty"}:
            continue
        assert matched_breakdown[component] == pytest.approx(
            mismatched_breakdown[component],
        )


@pytest.mark.parametrize(
    "fixture_name",
    [f"{profile}_wildcard_candidate" for profile in PROFILE_KEYS],
)
def test_golden_wildcard_candidate_is_eligible(fixture_name: str) -> None:
    """Wildcard golden events sit in the moderate-score band with high novelty."""
    golden = _load_golden(fixture_name)

    assert golden["expected_wildcard_eligible"] is True
    assert WILDCARD_SCORE_MIN <= golden["expected_score"] <= WILDCARD_SCORE_MAX
    assert golden["expected_score_breakdown"]["novelty"] >= WILDCARD_MIN_NOVELTY


@pytest.mark.parametrize("profile_key", PROFILE_KEYS)
def test_golden_wildcard_slot_assignment(profile_key: str) -> None:
    """assign_wildcard_slots marks golden wildcard candidates across profiles."""
    golden = _load_golden(f"{profile_key}_wildcard_candidate")
    top = _load_golden(f"{profile_key}_strong_match")
    profile = _profile_from_golden(golden)
    wildcard_event = _event_from_golden(golden)
    top_event = _event_from_golden(top)

    wildcard_breakdown, _, _ = ranking.compute_score_breakdown(
        wildcard_event,
        profile,
        semantic_similarity=0.0,
        previously_recommended_ids=set(),
    )
    top_breakdown, _, _ = ranking.compute_score_breakdown(
        top_event,
        profile,
        semantic_similarity=0.0,
        previously_recommended_ids=set(),
    )

    ranked = [
        RankedEvent(
            event=top_event,
            score=ranking.composite_score(top_breakdown),
            score_breakdown=top_breakdown,
            explanation="Top pick.",
            run_id=TEST_RUN_ID,
        ),
        RankedEvent(
            event=wildcard_event,
            score=golden["expected_score"],
            score_breakdown=wildcard_breakdown,
            explanation="Wildcard candidate.",
            run_id=TEST_RUN_ID,
        ),
    ]

    updated = ranking.assign_wildcard_slots(ranked)
    flags = {item.event.id: item.wildcard_slot for item in updated}

    assert flags[wildcard_event.id] is True
    assert flags[top_event.id] is False


@pytest.mark.parametrize("fixture_name", GOLDEN_FIXTURE_NAMES)
@pytest.mark.asyncio
async def test_golden_fixture_explanation_fallback(fixture_name: str) -> None:
    """Regression: LLM validation failures fall back to deterministic explanations."""
    golden = _load_golden(fixture_name)
    profile = _profile_from_golden(golden)
    event = _event_from_golden(golden)

    with (
        patch("scene_scout.agents.ranking.similarity_score", return_value=0.0),
        patch(
            "scene_scout.agents.ranking.complete",
            AsyncMock(side_effect=LLMValidationError("invalid json")),
        ),
        patch("scene_scout.agents.ranking.get_liked_events_collection"),
    ):
        results = await ranking.run(
            [event],
            profile,
            TEST_RUN_ID,
            previously_recommended_ids=set(golden["previously_recommended_ids"]),
        )

    assert len(results) == 1
    assert results[0].explanation == ranking.fallback_explanation(event)
    _assert_score_approx(results[0].score, golden["expected_score"])
