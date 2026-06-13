"""
Tests for user domain models.

Covers UserProfile validation, defaults, and field constraints.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from scene_scout.models.user import UserProfile

PROFILE_TIMESTAMP = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


def _valid_profile(**overrides: object) -> UserProfile:
    payload = {
        "user_id": "user-123",
        "name": "Morgan",
        "email": "morgan@example.com",
        "stated_interests": ["jazz", "outdoor"],
        "stated_dislikes": ["EDM festivals"],
        "preferred_neighborhoods": ["Silver Lake"],
        "max_travel_minutes": 45,
        "budget_ceiling_cents": 5000,
        "excluded_categories": ["nightclub"],
        "category_weights": {"jazz": 0.9, "outdoor": 0.7},
        "vibe_preferences": ["intimate", "outdoor"],
        "created_at": PROFILE_TIMESTAMP,
        "last_updated": PROFILE_TIMESTAMP,
        "profile_version": 1,
    }
    payload.update(overrides)
    return UserProfile.model_validate(payload)


def test_user_profile_validates_full_payload() -> None:
    profile = _valid_profile()

    assert profile.user_id == "user-123"
    assert profile.name == "Morgan"
    assert profile.email == "morgan@example.com"
    assert profile.stated_interests == ["jazz", "outdoor"]
    assert profile.stated_dislikes == ["EDM festivals"]
    assert profile.preferred_neighborhoods == ["Silver Lake"]
    assert profile.max_travel_minutes == 45
    assert profile.budget_ceiling_cents == 5000
    assert profile.excluded_categories == ["nightclub"]
    assert profile.category_weights == {"jazz": 0.9, "outdoor": 0.7}
    assert profile.vibe_preferences == ["intimate", "outdoor"]
    assert profile.created_at == PROFILE_TIMESTAMP
    assert profile.last_updated == PROFILE_TIMESTAMP
    assert profile.profile_version == 1


def test_user_profile_defaults_list_and_dict_fields() -> None:
    profile = UserProfile.model_validate(
        {
            "user_id": "user-456",
            "name": "Alex",
            "email": "alex@example.com",
            "created_at": PROFILE_TIMESTAMP,
            "last_updated": PROFILE_TIMESTAMP,
        }
    )

    assert profile.stated_interests == []
    assert profile.stated_dislikes == []
    assert profile.preferred_neighborhoods == []
    assert profile.excluded_categories == []
    assert profile.category_weights == {}
    assert profile.vibe_preferences == []
    assert profile.max_travel_minutes is None
    assert profile.budget_ceiling_cents is None
    assert profile.profile_version == 1


def test_user_profile_rejects_category_weight_outside_unit_interval() -> None:
    with pytest.raises(ValidationError, match="category_weights values"):
        _valid_profile(category_weights={"jazz": 1.5})


def test_user_profile_rejects_invalid_vibe_preference() -> None:
    with pytest.raises(ValidationError, match="vibe_preferences must use controlled"):
        _valid_profile(vibe_preferences=["not-a-real-vibe"])


def test_user_profile_rejects_negative_optional_constraints() -> None:
    with pytest.raises(ValidationError, match="must be non-negative"):
        _valid_profile(max_travel_minutes=-10)

    with pytest.raises(ValidationError, match="must be non-negative"):
        _valid_profile(budget_ceiling_cents=-100)


def test_user_profile_rejects_profile_version_below_one() -> None:
    with pytest.raises(ValidationError, match="profile_version must be at least 1"):
        _valid_profile(profile_version=0)
