"""
Event domain models for SceneScout.

``EventCandidateLLMOutput`` is the schema validated against LLM JSON.
``EventCandidate`` adds agent-owned metadata via :meth:`EventCandidate.from_llm_output`.
``NormalizedEvent`` is defined for cache serialization (Phase 2.8) and normalization
(Phase 4).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from scene_scout.models.feed import RawFeedEntry, SourceType
from scene_scout.structured_categories import infer_categories_from_text

STRUCTURED_INGEST_SOURCE_TYPES: frozenset[SourceType] = frozenset({"api", "ical"})
STRUCTURED_INGEST_CONFIDENCE = 1.0
_TRAILING_VENUE_PUNCTUATION = re.compile(r"[.,;:!?]+$")
_WHITESPACE = re.compile(r"\s+")


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


def structured_ingest_applies(
    entry: RawFeedEntry,
    *,
    scrape_structured_ingest: bool = False,
) -> bool:
    """Return True when this entry's source type may skip the extraction LLM."""
    if entry.source_type == "scrape":
        return scrape_structured_ingest
    return entry.source_type in STRUCTURED_INGEST_SOURCE_TYPES


def _is_http_url(url: str | None) -> bool:
    if not url or not url.strip():
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_structured_venue(venue: str | None) -> str | None:
    if venue is None:
        return None
    cleaned = _TRAILING_VENUE_PUNCTUATION.sub("", venue.strip())
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or None


def has_structured_ingest_fields(entry: RawFeedEntry, *, feed_city: str) -> bool:
    """Return True when adapter output has fields required to skip extraction."""
    if not entry.title or not entry.title.strip():
        return False
    if not _is_http_url(entry.link):
        return False
    if _normalize_structured_venue(entry.event_venue) is None:
        return False
    city = (entry.event_city or feed_city or "").strip()
    if not city:
        return False
    if not (entry.published_raw or "").strip():
        return False
    return True


def candidate_from_structured_entry(
    entry: RawFeedEntry,
    *,
    feed_city: str,
    run_id: str,
    extracted_at: datetime | None = None,
) -> EventCandidate | None:
    """Map structured adapter output to an ``EventCandidate`` without an LLM call."""
    if not has_structured_ingest_fields(entry, feed_city=feed_city):
        return None

    venue = _normalize_structured_venue(entry.event_venue)
    if venue is None:
        return None

    city = (entry.event_city or feed_city).strip()
    date_value = entry.published_raw.strip() if entry.published_raw else None
    time_value = entry.event_time.strip() if entry.event_time else None
    categories = list(entry.categories)
    if not categories:
        categories = infer_categories_from_text(
            title=entry.title,
            description=entry.description,
        )

    llm_output = EventCandidateLLMOutput(
        title=entry.title.strip(),
        date=date_value,
        time=time_value,
        venue=venue,
        city=city,
        url=entry.link.strip(),
        price=None,
        description=entry.description,
        categories=categories,
        is_event=True,
        extraction_confidence=STRUCTURED_INGEST_CONFIDENCE,
    )
    return EventCandidate.from_llm_output(
        llm_output,
        source_feed=entry.feed_id,
        run_id=run_id,
        extracted_at=extracted_at or datetime.now(timezone.utc),
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
