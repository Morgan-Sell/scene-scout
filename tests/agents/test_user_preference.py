"""
Tests for the User Preference Agent.

Covers cold-start parsing, profile persistence, load failures, and LLM error
propagation.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import RateLimitError

from scene_scout.agents import user_preference
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.event import compute_normalized_event_id
from scene_scout.models.feedback import FeedbackEvent
from scene_scout.models.user import UserProfile, UserProfileParseLLMOutput
from scene_scout.services.llm import LLMInfrastructureError, LLMValidationError
from scene_scout.user_preference_config import (
    FEEDBACK_CLICK_CATEGORY_DELTA,
    FEEDBACK_DECAY_LAMBDA,
    FEEDBACK_NEGATIVE_CATEGORY_DELTA,
)
from tests.conftest import TEST_RUN_ID

REFERENCE_TIME = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
EVENT_ID = compute_normalized_event_id("Jazz Night", "Sat, Jul 19 2026", "Blue Note")


def _llm_profile_json() -> str:
    return json.dumps(
        {
            "stated_interests": ["jazz", "outdoor"],
            "stated_dislikes": ["EDM festivals"],
            "preferred_neighborhoods": ["Silver Lake"],
            "max_travel_minutes": 45,
            "budget_ceiling_cents": 5000,
            "excluded_categories": ["nightclub"],
            "category_weights": {"jazz": 0.9, "outdoor": 0.7},
            "vibe_preferences": ["intimate", "outdoor"],
        }
    )


def _parsed_llm_output() -> UserProfileParseLLMOutput:
    return UserProfileParseLLMOutput.model_validate(json.loads(_llm_profile_json()))


def _mock_litellm_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        ),
    )


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated vol-profiles directory for user preference tests."""
    profiles_path = tmp_path / "vol-profiles"
    profiles_path.mkdir()
    monkeypatch.setattr(
        "scene_scout.agents.user_preference.vol_profiles_dir",
        lambda: profiles_path,
    )
    return profiles_path


@pytest.mark.asyncio
async def test_parse_cold_start_writes_profile_json(profiles_dir: Path) -> None:
    with patch(
        "scene_scout.agents.user_preference.complete",
        AsyncMock(return_value=_parsed_llm_output()),
    ):
        profile = await user_preference.parse_cold_start(
            name="Morgan",
            email="Morgan@Example.com",
            prompt="I love jazz and outdoor concerts in Silver Lake.",
            run_id=TEST_RUN_ID,
            home_city="Los Angeles",
            horizon_days=14,
        )

    profile_path = profiles_dir / "profile.json"
    assert profile_path.is_file()

    loaded = UserProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
    assert loaded.name == "Morgan"
    assert loaded.email == "Morgan@Example.com"
    assert loaded.home_city == "Los Angeles"
    assert loaded.horizon_days == 14
    assert loaded.stated_interests == ["jazz", "outdoor"]
    assert loaded.vibe_preferences == ["intimate", "outdoor"]
    assert loaded.profile_version == 1
    assert loaded.user_id == profile.user_id
    assert profile == loaded


@pytest.mark.asyncio
async def test_parse_cold_start_calls_llm_complete(profiles_dir: Path) -> None:
    mock_complete = AsyncMock(return_value=_parsed_llm_output())

    with patch("scene_scout.agents.user_preference.complete", mock_complete):
        await user_preference.parse_cold_start(
            name="Morgan",
            email="morgan@example.com",
            prompt="Jazz and outdoor events only.",
            run_id=TEST_RUN_ID,
            home_city="Los Angeles",
            horizon_days=21,
        )

    mock_complete.assert_awaited_once()
    call_kwargs = mock_complete.await_args.kwargs
    assert call_kwargs["run_id"] == TEST_RUN_ID
    assert call_kwargs["agent_name"] == "user_preference"
    assert "Jazz and outdoor events only." in call_kwargs["prompt"]
    assert "Morgan" in call_kwargs["prompt"]


def test_load_profile_returns_persisted_profile(profiles_dir: Path) -> None:
    timestamp = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    profile = UserProfile(
        user_id="abc123",
        name="Morgan",
        email="morgan@example.com",
        stated_interests=["jazz"],
        created_at=timestamp,
        last_updated=timestamp,
    )
    user_preference.write_profile(profile)

    loaded = user_preference.load_profile()

    assert loaded == profile


def test_load_profile_raises_when_missing(profiles_dir: Path) -> None:
    with pytest.raises(
        user_preference.UserProfileNotFoundError,
        match="No user profile",
    ):
        user_preference.load_profile()


@pytest.mark.asyncio
async def test_parse_cold_start_propagates_infrastructure_error(
    profiles_dir: Path,
) -> None:
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        AsyncMock(
            side_effect=RateLimitError(
                message="rate limited",
                llm_provider="anthropic",
                model="claude-sonnet-4-6",
            )
        ),
    ):
        with pytest.raises(LLMInfrastructureError):
            await user_preference.parse_cold_start(
                name="Morgan",
                email="morgan@example.com",
                prompt="Jazz only.",
                run_id=TEST_RUN_ID,
                home_city="New York",
                horizon_days=14,
            )


@pytest.mark.asyncio
async def test_parse_cold_start_propagates_validation_error(
    profiles_dir: Path,
) -> None:
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        AsyncMock(return_value=_mock_litellm_response('{"unexpected": true}')),
    ):
        with pytest.raises(LLMValidationError):
            await user_preference.parse_cold_start(
                name="Morgan",
                email="morgan@example.com",
                prompt="Jazz only.",
                run_id=TEST_RUN_ID,
                home_city="New York",
                horizon_days=14,
            )


def _base_profile(**overrides: object) -> UserProfile:
    timestamp = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    payload = {
        "user_id": "abc123",
        "name": "Morgan",
        "email": "morgan@example.com",
        "home_city": "New York",
        "horizon_days": 14,
        "category_weights": {"jazz": 0.5, "comedy": 0.8},
        "vibe_preferences": ["intimate", "social"],
        "created_at": timestamp,
        "last_updated": timestamp,
    }
    payload.update(overrides)
    return UserProfile.model_validate(payload)


def _feedback_event(**overrides: object) -> FeedbackEvent:
    payload = {
        "token": str(uuid.uuid4()),
        "signal": "click",
        "run_id": TEST_RUN_ID,
        "event_id": EVENT_ID,
        "categories": ["Jazz"],
        "received_at": REFERENCE_TIME,
    }
    payload.update(overrides)
    return FeedbackEvent.model_validate(payload)


def _enriched_event(**overrides: object) -> EnrichedEvent:
    payload = {
        "id": EVENT_ID,
        "title": "Jazz Night",
        "start_datetime": REFERENCE_TIME,
        "venue": "Blue Note",
        "city": "New York",
        "url": "https://example.com/jazz-night",
        "is_free": False,
        "description": "An intimate jazz set.",
        "categories": ["Jazz"],
        "source_feeds": ["donyc"],
        "source_count": 1,
        "source_quality_score": 0.8,
        "best_source_feed": "donyc",
        "normalized_at": REFERENCE_TIME,
        "run_id": TEST_RUN_ID,
        "vibe_tags": ["intimate", "high-energy"],
    }
    payload.update(overrides)
    return EnrichedEvent.model_validate(payload)


def test_apply_feedback_signals_increases_category_weight_on_click(
    profiles_dir: Path,
) -> None:
    profile = _base_profile()
    user_preference.write_profile(profile)

    updated = user_preference.apply_feedback_signals(
        profile,
        [_feedback_event(signal="click", categories=["Jazz"])],
        reference_time=REFERENCE_TIME,
        run_id=TEST_RUN_ID,
    )

    assert updated.category_weights["jazz"] == pytest.approx(
        0.5 + FEEDBACK_CLICK_CATEGORY_DELTA,
    )
    assert updated.last_updated == REFERENCE_TIME

    loaded = user_preference.load_profile()
    assert loaded.category_weights["jazz"] == updated.category_weights["jazz"]


def test_apply_feedback_signals_decreases_category_weight_on_negative(
    profiles_dir: Path,
) -> None:
    profile = _base_profile()

    updated = user_preference.apply_feedback_signals(
        profile,
        [_feedback_event(signal="negative", categories=["comedy"])],
        reference_time=REFERENCE_TIME,
        persist=False,
    )

    assert updated.category_weights["comedy"] == pytest.approx(
        0.8 + FEEDBACK_NEGATIVE_CATEGORY_DELTA,
    )


def test_apply_feedback_signals_scales_deltas_by_decay(profiles_dir: Path) -> None:
    profile = _base_profile()
    age_days = 30.0
    expected_weight = math.exp(-FEEDBACK_DECAY_LAMBDA * age_days)
    old_signal = _feedback_event(
        signal="click",
        categories=["Jazz"],
        received_at=REFERENCE_TIME - timedelta(days=age_days),
    )

    updated = user_preference.apply_feedback_signals(
        profile,
        [old_signal],
        reference_time=REFERENCE_TIME,
        persist=False,
    )

    assert updated.category_weights["jazz"] == pytest.approx(
        0.5 + FEEDBACK_CLICK_CATEGORY_DELTA * expected_weight,
    )


def test_apply_feedback_signals_updates_vibe_preferences(profiles_dir: Path) -> None:
    profile = _base_profile()
    event = _enriched_event(vibe_tags=["high-energy", "late-night"])

    with patch(
        "scene_scout.agents.user_preference.chroma_service.add_liked_event",
    ):
        click_updated = user_preference.apply_feedback_signals(
            profile,
            [_feedback_event(signal="click")],
            events_by_id={EVENT_ID: event},
            reference_time=REFERENCE_TIME,
            persist=False,
        )
    assert "high-energy" in click_updated.vibe_preferences
    assert "late-night" in click_updated.vibe_preferences
    assert "intimate" in click_updated.vibe_preferences

    negative_updated = user_preference.apply_feedback_signals(
        click_updated,
        [_feedback_event(signal="negative")],
        events_by_id={EVENT_ID: event},
        reference_time=REFERENCE_TIME,
        persist=False,
    )
    assert "high-energy" not in negative_updated.vibe_preferences
    assert "late-night" not in negative_updated.vibe_preferences
    assert "intimate" in negative_updated.vibe_preferences


def test_apply_feedback_signals_calls_chroma_for_click_events(
    profiles_dir: Path,
) -> None:
    profile = _base_profile()
    event = _enriched_event()

    with patch(
        "scene_scout.agents.user_preference.chroma_service.add_liked_event",
    ) as add_liked_event:
        user_preference.apply_feedback_signals(
            profile,
            [_feedback_event(signal="click")],
            events_by_id={EVENT_ID: event},
            reference_time=REFERENCE_TIME,
            persist=False,
        )

    add_liked_event.assert_called_once_with(event, None)


def test_apply_feedback_signals_clamps_category_weights(profiles_dir: Path) -> None:
    profile = _base_profile(category_weights={"comedy": 0.02})

    updated = user_preference.apply_feedback_signals(
        profile,
        [_feedback_event(signal="negative", categories=["comedy"])],
        reference_time=REFERENCE_TIME,
        persist=False,
    )

    assert updated.category_weights["comedy"] == 0.0


def test_apply_feedback_signals_noop_for_empty_signals(profiles_dir: Path) -> None:
    profile = _base_profile()

    updated = user_preference.apply_feedback_signals(
        profile,
        [],
        reference_time=REFERENCE_TIME,
        persist=False,
    )

    assert updated == profile
