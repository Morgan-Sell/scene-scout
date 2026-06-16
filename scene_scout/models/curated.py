"""
Curated recommendation domain models for SceneScout.

``CuratedRecommendation`` is the Recommendation Curator output consumed by Email
Composer and persisted to recommendation history.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scene_scout.curator_config import CuratorConfig
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.ranking import SCORE_COMPONENT_KEYS, SelloutRisk


class CuratedRecommendation(BaseModel):
    """Final ranked recommendation selected by Allegra for the weekly email."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    event: EnrichedEvent
    score: float
    score_breakdown: dict[str, float]
    explanation: str
    neighborhood_context: str | None = None
    sellout_risk: SelloutRisk
    sellout_urgency_note: str | None = None
    feedback_token: str
    is_wildcard: bool = False
    run_id: str
    recommended_at: datetime

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

    @field_validator("score_breakdown")
    @classmethod
    def _validate_score_breakdown(cls, value: dict[str, float]) -> dict[str, float]:
        missing = [key for key in SCORE_COMPONENT_KEYS if key not in value]
        if missing:
            raise ValueError(f"score_breakdown missing components: {missing}")
        for component, score in value.items():
            if not 0.0 <= score <= 1.0:
                raise ValueError(
                    f"score_breakdown[{component!r}] must be between 0.0 and 1.0"
                )
        return value


class CuratorResult(BaseModel):
    """Recommendation Curator output for downstream email composition."""

    model_config = ConfigDict(extra="forbid")

    recommendations: list[CuratedRecommendation] = Field(default_factory=list)
    below_minimum: bool = False
    curator_config: CuratorConfig
