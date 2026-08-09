"""
Tests for the Recommendation Curator Agent.

Covers diversity rules, history hard-exclude safety, wildcard assignment,
CuratedRecommendation construction, and Allegra voice brief loading.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scene_scout.agents import recommendation_curator, sellout_risk
from scene_scout.curator_config import (
    CURATOR_MAX_PER_CATEGORY,
    CURATOR_MAX_PER_VENUE,
    CURATOR_MAX_RECOMMENDATIONS,
    load_curator_config,
)
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.history import RecommendationRecord
from scene_scout.models.ranking import RankedEvent
from scene_scout.models.user import UserProfile
from scene_scout.services.feedback import generate_feedback_token
from scene_scout.services.history import write_recommendations
from tests.conftest import TEST_RUN_ID

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
PROFILE_TIMESTAMP = NOW


def _profile(**overrides: object) -> UserProfile:
    payload = {
        "user_id": "user-123",
        "name": "Morgan",
        "email": "morgan@example.com",
        "home_city": "Los Angeles",
        "horizon_days": 14,
        "stated_interests": ["jazz"],
        "preferred_neighborhoods": ["Silver Lake"],
        "category_weights": {"jazz": 0.9},
        "vibe_preferences": ["intimate"],
        "excluded_categories": [],
        "created_at": PROFILE_TIMESTAMP,
        "last_updated": PROFILE_TIMESTAMP,
    }
    payload.update(overrides)
    return UserProfile.model_validate(payload)


def _event(
    *,
    event_id: str,
    title: str,
    venue: str = "The Sandlot",
    categories: list[str] | None = None,
    day_offset: int = 1,
    score_seed: float = 0.5,
) -> EnrichedEvent:
    start = NOW + timedelta(days=day_offset, hours=18)
    return EnrichedEvent.model_validate(
        {
            "id": event_id,
            "title": title,
            "start_datetime": start,
            "venue": venue,
            "city": "Los Angeles",
            "url": f"https://example.com/{event_id}",
            "is_free": False,
            "description": f"Description for {title}.",
            "categories": categories or ["Music"],
            "source_feeds": ["sandlot-pickup-league"],
            "source_count": 1,
            "best_source_feed": "sandlot-pickup-league",
            "source_quality_score": 0.8,
            "description_quality_score": 0.8,
            "low_information": False,
            "run_id": TEST_RUN_ID,
            "normalized_at": NOW,
            "top_performer_affinity": score_seed,
            "vibe_tags": ["intimate"],
            "neighborhood_context": "Walkable from Echo Park.",
        }
    )


def _breakdown(**overrides: float) -> dict[str, float]:
    payload = {
        "category_fit": 0.8,
        "vibe_fit": 0.7,
        "semantic_similarity": 0.0,
        "performer_affinity": 0.5,
        "location": 1.0,
        "novelty": 1.0,
        "source_quality": 0.8,
        "source_coverage": 0.33,
        "description_quality": 0.8,
    }
    payload.update(overrides)
    return payload


def _ranked(
    event: EnrichedEvent,
    *,
    score: float,
    wildcard_slot: bool = False,
    sellout_risk: str = "low",
) -> RankedEvent:
    return RankedEvent(
        event=event,
        score=score,
        score_breakdown=_breakdown(novelty=0.8 if wildcard_slot else 1.0),
        explanation=f"Why {event.title} fits this week.",
        wildcard_slot=wildcard_slot,
        sellout_risk=sellout_risk,
        run_id=TEST_RUN_ID,
    )


def test_load_curator_config_reads_allegra_voice_brief() -> None:
    config = load_curator_config()

    assert config.name == "Allegra"
    assert 'Never use the word "curated"' in config.voice_brief


def test_select_recommendations_respects_category_cap() -> None:
    events = [
        _ranked(
            _event(
                event_id=f"jazz-{index}",
                title=f"Jazz Night {index}",
                categories=["Jazz"],
                day_offset=index,
            ),
            score=0.9 - index * 0.01,
        )
        for index in range(1, 6)
    ]

    selected = recommendation_curator.select_recommendations(events)

    jazz_count = sum(
        1
        for item in selected
        if "jazz" in [category.lower() for category in item.event.categories]
    )
    assert jazz_count <= CURATOR_MAX_PER_CATEGORY


def test_select_recommendations_respects_venue_cap() -> None:
    events = [
        _ranked(
            _event(
                event_id=f"venue-{index}",
                title=f"Same Venue Show {index}",
                venue="The Sandlot",
                categories=[f"Genre-{index}"],
                day_offset=index,
            ),
            score=0.9 - index * 0.01,
        )
        for index in range(1, 5)
    ]

    selected = recommendation_curator.select_recommendations(events)

    sandlot_count = sum(
        1 for item in selected if item.event.venue.lower() == "the sandlot"
    )
    assert sandlot_count <= CURATOR_MAX_PER_VENUE


def test_select_recommendations_prefers_two_distinct_dates() -> None:
    same_day_events = [
        _ranked(
            _event(
                event_id=f"day1-{index}",
                title=f"Day One Show {index}",
                categories=[f"Cat-{index}"],
                day_offset=1,
            ),
            score=0.95 - index * 0.01,
        )
        for index in range(1, 5)
    ]
    second_day = _ranked(
        _event(
            event_id="day2-show",
            title="Day Two Show",
            categories=["Experimental"],
            day_offset=3,
        ),
        score=0.70,
    )

    selected = recommendation_curator.select_recommendations(
        same_day_events + [second_day]
    )

    dates = {recommendation_curator._event_date(item.event) for item in selected}
    assert len(dates) >= 2


def test_select_recommendations_includes_wildcard_slot() -> None:
    top_pick = _ranked(
        _event(event_id="top", title="Top Pick", day_offset=1),
        score=0.95,
    )
    wildcard = _ranked(
        _event(
            event_id="wildcard",
            title="Wildcard Pick",
            categories=["Experimental"],
            day_offset=2,
        ),
        score=0.55,
        wildcard_slot=True,
    )

    selected = recommendation_curator.select_recommendations([top_pick, wildcard])

    assert any(item.event.id == "wildcard" for item in selected)
    assert any(item.wildcard_slot for item in selected)


def test_build_curated_recommendations_marks_wildcards_and_tokens() -> None:
    wildcard = _ranked(
        _event(event_id="wildcard", title="Wildcard Pick", day_offset=2),
        score=0.55,
        wildcard_slot=True,
    )
    top_pick = _ranked(
        _event(event_id="top", title="Top Pick", day_offset=1),
        score=0.95,
    )

    curated = recommendation_curator.build_curated_recommendations(
        [top_pick, wildcard],
        run_id=TEST_RUN_ID,
        now=NOW,
    )

    assert curated[0].rank == 1
    assert curated[0].feedback_token
    assert curated[0].neighborhood_context == "Walkable from Echo Park."
    assert curated[1].is_wildcard is True
    assert curated[0].is_wildcard is False


def test_build_curated_recommendations_passes_through_high_risk_urgency_note() -> None:
    event = _event(
        event_id="risky",
        title="Risky Show",
        day_offset=1,
        venue="Basement Club",
        score_seed=0.95,
    ).model_copy(
        update={
            "is_free": True,
            "description": "Final release — selling fast before doors open.",
            "start_datetime": NOW + timedelta(days=1, hours=18),
        }
    )
    risky = sellout_risk.annotate_event_risk(_ranked(event, score=0.9), now=NOW)
    assert risky.sellout_risk == "high"

    curated = recommendation_curator.build_curated_recommendations(
        [risky],
        run_id=TEST_RUN_ID,
        now=NOW,
    )

    assert curated[0].sellout_risk == "high"
    assert curated[0].sellout_urgency_note == sellout_risk.HIGH_RISK_URGENCY_NOTE


@pytest.mark.asyncio
async def test_run_flags_below_minimum_when_fewer_than_ten(
    migrated_databases: tuple,
) -> None:
    events = [
        _ranked(
            _event(
                event_id=f"event-{index}",
                title=f"Show {index}",
                categories=[f"Cat-{index}"],
                venue=f"Venue {index}",
                day_offset=index,
            ),
            score=0.8 - index * 0.01,
        )
        for index in range(1, 4)
    ]

    result = await recommendation_curator.run(events, _profile(), TEST_RUN_ID, now=NOW)

    assert len(result.recommendations) == 3
    assert result.below_minimum is True
    assert result.curator_config.name == "Allegra"
    assert "Allegra" in result.curator_config.voice_brief


@pytest.mark.asyncio
async def test_run_excludes_recent_history_events(
    migrated_databases: tuple,
) -> None:
    recent_event = _event(event_id="recent-repeat", title="Recent Repeat", day_offset=1)
    fresh_event = _event(event_id="fresh-show", title="Fresh Show", day_offset=2)
    ranked = [
        _ranked(recent_event, score=0.95),
        _ranked(fresh_event, score=0.85),
    ]
    write_recommendations(
        [
            RecommendationRecord(
                feedback_token=generate_feedback_token(),
                event_id="recent-repeat",
                run_id=TEST_RUN_ID,
                rank=1,
                score=0.9,
                score_breakdown=_breakdown(),
                event_title="Recent Repeat",
                explanation="Sent last week.",
                recommended_at=NOW - timedelta(days=3),
            )
        ]
    )

    result = await recommendation_curator.run(ranked, _profile(), TEST_RUN_ID, now=NOW)

    assert [item.event.id for item in result.recommendations] == ["fresh-show"]


@pytest.mark.asyncio
async def test_run_selects_up_to_ten_with_diversity(
    migrated_databases: tuple,
) -> None:
    events = [
        _ranked(
            _event(
                event_id=f"event-{index}",
                title=f"Show {index}",
                categories=[f"Cat-{index % 6}"],
                venue=f"Venue {index}",
                day_offset=(index % 4) + 1,
            ),
            score=0.95 - index * 0.01,
            wildcard_slot=index == 8,
        )
        for index in range(15)
    ]

    result = await recommendation_curator.run(events, _profile(), TEST_RUN_ID, now=NOW)

    assert len(result.recommendations) == CURATOR_MAX_RECOMMENDATIONS
    assert result.below_minimum is False
    assert sum(1 for item in result.recommendations if item.is_wildcard) >= 1

    category_counts: dict[str, int] = {}
    venue_counts: dict[str, int] = {}
    for item in result.recommendations:
        for category in item.event.categories:
            key = category.lower()
            category_counts[key] = category_counts.get(key, 0) + 1
        venue = item.event.venue.lower()
        venue_counts[venue] = venue_counts.get(venue, 0) + 1

    assert all(count <= CURATOR_MAX_PER_CATEGORY for count in category_counts.values())
    assert all(count <= CURATOR_MAX_PER_VENUE for count in venue_counts.values())
