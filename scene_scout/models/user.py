"""
User domain models for SceneScout.

``UserProfile`` is the persisted taste profile written by the User Preference Agent
and consumed by Ranking, the Recommendation Curator, and Email Composer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scene_scout.vibe_classifier_config import VIBE_VOCABULARY

HORIZON_DAYS_MIN = 1
HORIZON_DAYS_MAX = 60
DEFAULT_HORIZON_DAYS = 14
LEGACY_DEFAULT_HOME_CITY = "New York"
LEGACY_DEFAULT_HORIZON_DAYS = 7


def _validate_category_weights_dict(value: dict[str, float]) -> dict[str, float]:
    for weight in value.values():
        if not 0.0 <= weight <= 1.0:
            raise ValueError("category_weights values must be between 0.0 and 1.0")
    return value


def _validate_vibe_preference_list(value: list[str]) -> list[str]:
    invalid = sorted(set(value) - VIBE_VOCABULARY)
    if invalid:
        raise ValueError(
            f"vibe_preferences must use controlled vocabulary; invalid: {invalid}"
        )
    return value


class UserProfileParseLLMOutput(BaseModel):
    """Fields returned by the LLM during cold-start profile parsing."""

    model_config = ConfigDict(extra="forbid")

    stated_interests: list[str] = Field(default_factory=list)
    stated_dislikes: list[str] = Field(default_factory=list)
    preferred_neighborhoods: list[str] = Field(default_factory=list)
    max_travel_minutes: int | None = None
    budget_ceiling_cents: int | None = None
    excluded_categories: list[str] = Field(default_factory=list)
    category_weights: dict[str, float] = Field(default_factory=dict)
    vibe_preferences: list[str] = Field(default_factory=list)

    @field_validator("category_weights")
    @classmethod
    def _validate_category_weights(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_category_weights_dict(value)

    @field_validator("vibe_preferences")
    @classmethod
    def _validate_vibe_preferences(cls, value: list[str]) -> list[str]:
        return _validate_vibe_preference_list(value)

    @field_validator("max_travel_minutes", "budget_ceiling_cents")
    @classmethod
    def _validate_non_negative_optional(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("must be non-negative when set")
        return value


class UserProfile(BaseModel):
    """Structured user taste profile for ranking and curation."""

    user_id: str
    name: str
    email: str
    home_city: str
    horizon_days: int = Field(ge=HORIZON_DAYS_MIN, le=HORIZON_DAYS_MAX)
    stated_interests: list[str] = Field(default_factory=list)
    stated_dislikes: list[str] = Field(default_factory=list)
    preferred_neighborhoods: list[str] = Field(default_factory=list)
    max_travel_minutes: int | None = None
    budget_ceiling_cents: int | None = None
    excluded_categories: list[str] = Field(default_factory=list)
    category_weights: dict[str, float] = Field(default_factory=dict)
    vibe_preferences: list[str] = Field(default_factory=list)
    created_at: datetime
    last_updated: datetime
    profile_version: int = 1

    @model_validator(mode="before")
    @classmethod
    def _apply_legacy_defaults(cls, data: Any) -> Any:
        """Backfill city/horizon for profiles saved before Phase 1C.1."""
        if isinstance(data, dict):
            payload = dict(data)
            payload.setdefault("home_city", LEGACY_DEFAULT_HOME_CITY)
            payload.setdefault("horizon_days", LEGACY_DEFAULT_HORIZON_DAYS)
            return payload
        return data

    @field_validator("home_city")
    @classmethod
    def _validate_home_city(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("home_city must be non-empty")
        return cleaned

    @field_validator("category_weights")
    @classmethod
    def _validate_category_weights(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_category_weights_dict(value)

    @field_validator("vibe_preferences")
    @classmethod
    def _validate_vibe_preferences(cls, value: list[str]) -> list[str]:
        return _validate_vibe_preference_list(value)

    @field_validator("max_travel_minutes", "budget_ceiling_cents")
    @classmethod
    def _validate_non_negative_optional(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("must be non-negative when set")
        return value

    @field_validator("profile_version")
    @classmethod
    def _validate_profile_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("profile_version must be at least 1")
        return value
