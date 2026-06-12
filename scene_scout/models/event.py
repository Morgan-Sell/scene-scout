"""
Event domain models for SceneScout.

``EventCandidateLLMOutput`` is the schema validated against LLM JSON.
``EventCandidate`` adds agent-owned metadata via :meth:`EventCandidate.from_llm_output`.
``NormalizedEvent`` is defined for cache serialization (Phase 2.8) and normalization
(Phase 4).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


def compute_normalized_event_id(title: str, date: str, venue: str) -> str:
    """Return a stable deduplication ID for a normalized event.

    Parameters
    ----------
    title : str
        Event title.
    date : str
        Event date as written or normalized (used verbatim in the hash input).
    venue : str
        Venue name.

    Returns
    -------
    str
        SHA-256 hex digest of ``title + date + venue``.
    """
    payload = f"{title}{date}{venue}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    """Structured event record after normalization.

    ``id`` is a SHA-256 hash of ``title + date + venue`` (see
    :func:`compute_normalized_event_id`). Source provenance fields are populated at
    normalization time with a single feed and updated by deduplication when records
    from multiple feeds are merged.
    """

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

    @field_validator("source_quality_score", "description_quality_score")
    @classmethod
    def _validate_quality_score(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("quality scores must be between 0.0 and 1.0")
        return value

    @field_validator("source_count")
    @classmethod
    def _validate_source_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("source_count must be at least 1")
        return value


class VenueCacheEntry(BaseModel):
    """Partial venue cache read — fields may be ``None`` when their TTL has expired."""

    coordinates: tuple[float, float] | None = None
    poi_list: list[dict[str, Any]] | None = None
    neighborhood_context: str | None = None
    neighborhood_confidence: float | None = None
