"""
Ranking domain models for SceneScout.

``RankedEvent`` is the Ranking Agent output consumed by Sell-Out Risk and the
Recommendation Curator.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scene_scout.models.enrichment import EnrichedEvent

SelloutRisk = Literal["low", "medium", "high"]

SCORE_COMPONENT_KEYS = (
    "category_fit",
    "vibe_fit",
    "semantic_similarity",
    "performer_affinity",
    "location",
    "novelty",
    "source_quality",
    "source_coverage",
    "description_quality",
)


class RankingExplanationLLMOutput(BaseModel):
    """LLM response schema for ranking explanations."""

    model_config = ConfigDict(extra="forbid")

    explanation: str = Field(min_length=1)


class RankedEvent(BaseModel):
    """Enriched event with deterministic score, breakdown, and explanation."""

    event: EnrichedEvent
    score: float
    score_breakdown: dict[str, float]
    explanation: str
    is_previously_recommended: bool = False
    novelty_penalty_applied: bool = False
    wildcard_slot: bool = False
    sellout_risk: SelloutRisk | None = None
    run_id: str

    @field_validator("sellout_risk")
    @classmethod
    def _validate_sellout_risk(cls, value: SelloutRisk | None) -> SelloutRisk | None:
        if value is not None and value not in {"low", "medium", "high"}:
            raise ValueError("sellout_risk must be low, medium, or high")
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
