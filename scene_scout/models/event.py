"""
Event domain models for SceneScout.

``EventCandidate`` is the extraction agent output (Phase 3). ``NormalizedEvent`` is
defined here for cache serialization (Phase 2.8) and normalization (Phase 4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EventCandidateLLMOutput(BaseModel):
    """Fields returned by the LLM during event extraction."""

    title: str
    date: str | None = None
    time: str | None = None
    venue: str | None = None
    neighborhood: str | None = None
    city: str
    url: str
    price: str | None = None
    description: str | None = None
    categories: list[str] = Field(default_factory=list)
    is_event: bool
    extraction_confidence: float

    @field_validator("extraction_confidence")
    @classmethod
    def _validate_extraction_confidence(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("extraction_confidence must be between 0.0 and 1.0")
        return value


class EventCandidate(EventCandidateLLMOutput):
    """Full extraction agent output — LLM fields plus pipeline metadata."""

    source_feed: str
    run_id: str
    extracted_at: datetime

    @classmethod
    def from_llm_output(
        cls,
        output: EventCandidateLLMOutput,
        *,
        source_feed: str,
        run_id: str,
        extracted_at: datetime,
    ) -> EventCandidate:
        """Build a full candidate by merging LLM output with agent metadata."""
        return cls(
            **output.model_dump(),
            source_feed=source_feed,
            run_id=run_id,
            extracted_at=extracted_at,
        )


class NormalizedEvent(BaseModel):
    """Structured event record after normalization."""

    id: str
    title: str
    start_datetime: datetime
    end_datetime: datetime | None = None
    venue: str
    neighborhood: str | None = None
    city: str
    url: str
    price_cents: int | None = None
    is_free: bool
    description: str
    categories: list[str] = Field(default_factory=list)
    source_feeds: list[str] = Field(default_factory=list)
    source_count: int = 1
    best_source_feed: str = ""
    source_quality_score: float = 0.0
    description_quality_score: float = 0.0
    low_information: bool = False
    run_id: str = ""
    normalized_at: datetime | None = None


class PerformerInfo(BaseModel):
    """Performer enrichment payload stored in ``performer_cache``."""

    name: str
    entity_type: str
    genre_tags: list[str] = Field(default_factory=list)
    one_line_summary: str | None = None
    confidence: float = 0.0
    affinity_score: float = 0.0


class VenueCacheEntry(BaseModel):
    """Partial venue cache read — fields may be ``None`` when their TTL has expired."""

    coordinates: tuple[float, float] | None = None
    poi_list: list[dict[str, Any]] | None = None
    neighborhood_context: str | None = None
    neighborhood_confidence: float | None = None
