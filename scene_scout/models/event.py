"""
Event domain models for SceneScout.

``NormalizedEvent`` is defined here for cache serialization (Phase 2.8) and will be
extended by normalization agents in Phase 4.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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
