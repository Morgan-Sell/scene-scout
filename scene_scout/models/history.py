"""
Recommendation history domain models for SceneScout.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RecommendationRecord(BaseModel):
    """Row payload written to ``recommendation_history``."""

    model_config = ConfigDict(extra="forbid")

    feedback_token: str
    event_id: str
    run_id: str
    rank: int
    score: float
    score_breakdown: dict[str, float]
    event_title: str
    categories: list[str] = Field(default_factory=list)
    explanation: str
    neighborhood_context: str | None = None
    sellout_risk: str | None = None
    sellout_urgency_note: str | None = None
    is_wildcard: bool = False
    recommended_at: datetime
    feedback_signal: str | None = None

    @field_validator("feedback_token")
    @classmethod
    def _validate_feedback_token_is_uuid(cls, value: str) -> str:
        uuid.UUID(value)
        return value

    @field_validator("rank")
    @classmethod
    def _validate_rank_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("rank must be at least 1")
        return value

    @field_validator("score")
    @classmethod
    def _validate_score(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("score must be between 0.0 and 1.0")
        return value


class RecommendationHistoryEntry(RecommendationRecord):
    """Recommendation history row returned from SQLite."""

    id: int
