"""
Enrichment domain models for SceneScout.

``PerformerInfo`` is the performer cache payload and a field on ``EnrichedEvent``.
``EnrichedEvent`` extends ``NormalizedEvent`` with performer, vibe, and neighborhood
fields populated by Phase 5 enrichment agents.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from scene_scout.models.event import NormalizedEvent


class PerformerInfo(BaseModel):
    """Performer enrichment payload stored in ``performer_cache``."""

    name: str
    entity_type: str
    genre_tags: list[str] = Field(default_factory=list)
    one_line_summary: str | None = None
    confidence: float = 0.0
    affinity_score: float = 0.0

    @field_validator("confidence", "affinity_score")
    @classmethod
    def _validate_unit_score(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                "confidence and affinity_score must be between 0.0 and 1.0"
            )
        return value


class EnrichedEvent(NormalizedEvent):
    """Normalized event extended with enrichment agent outputs."""

    performers: list[PerformerInfo] = Field(default_factory=list)
    top_performer_affinity: float = 0.0
    vibe_tags: list[str] = Field(default_factory=list)
    neighborhood_context: str | None = None
    neighborhood_confidence: float = 0.0
    venue_coordinates: tuple[float, float] | None = None

    @field_validator("top_performer_affinity", "neighborhood_confidence")
    @classmethod
    def _validate_unit_score(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                "top_performer_affinity and neighborhood_confidence must be "
                "between 0.0 and 1.0"
            )
        return value

    @classmethod
    def from_normalized(cls, event: NormalizedEvent) -> EnrichedEvent:
        """Build an enriched event with empty enrichment fields."""
        return cls.model_validate(event.model_dump())
