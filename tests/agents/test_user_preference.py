"""
Tests for the User Preference Agent.

Covers cold-start parsing, profile persistence, load failures, and LLM error
propagation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import RateLimitError

from scene_scout.agents import user_preference
from scene_scout.models.user import UserProfile, UserProfileParseLLMOutput
from scene_scout.services.llm import LLMInfrastructureError, LLMValidationError
from tests.conftest import TEST_RUN_ID


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
        )

    profile_path = profiles_dir / "profile.json"
    assert profile_path.is_file()

    loaded = UserProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
    assert loaded.name == "Morgan"
    assert loaded.email == "Morgan@Example.com"
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
            )
