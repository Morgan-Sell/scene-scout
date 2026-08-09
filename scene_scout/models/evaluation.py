"""
Evaluation domain models for SceneScout.

The Evaluation Agent validates curated recommendation quality via LLM review.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FlaggedRecommendation(BaseModel):
    """A single recommendation flagged during quality review."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    issue_type: str = Field(min_length=1)
    description: str = Field(min_length=1)

    @field_validator("rank")
    @classmethod
    def _validate_rank_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("rank must be at least 1")
        return value


class EvaluationLLMOutput(BaseModel):
    """LLM response schema for recommendation quality evaluation."""

    model_config = ConfigDict(extra="forbid")

    overall_quality: float
    flagged_recommendations: list[FlaggedRecommendation] = Field(default_factory=list)
    list_level_issues: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)

    @field_validator("overall_quality")
    @classmethod
    def _validate_overall_quality(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("overall_quality must be between 0.0 and 1.0")
        return value


class EvaluationReport(EvaluationLLMOutput):
    """Persisted evaluation report for a pipeline run."""

    run_id: str
    recommendation_count: int
    report_path: Path | None = None

    @field_validator("recommendation_count")
    @classmethod
    def _validate_recommendation_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("recommendation_count must be non-negative")
        return value
