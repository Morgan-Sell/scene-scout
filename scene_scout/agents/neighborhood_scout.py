"""
Neighborhood Scout Agent

Responsibility
--------------
Geocode venues, fetch nearby POIs, and narrate hyper-local context via batch LLM calls.

Design
------
Inputs  : list[EnrichedEvent], run_id, cache, batch_strategy
Outputs : list[EnrichedEvent] with neighborhood fields populated
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError, field_validator

from scene_scout.logging import get_logger
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.neighborhood_scout_config import (
    MODE_GEO_ASSISTED,
    MODE_LLM_FALLBACK,
    NEIGHBORHOOD_CONFIDENCE_THRESHOLD,
)
from scene_scout.services.batch import (
    BatchRequest,
    BatchResults,
    BatchStrategy,
    get_batch_strategy,
)
from scene_scout.services.cache import CacheService
from scene_scout.services.geocoding import (
    geocode_venue,
    get_nearby_pois,
    venue_cache_key,
)
from scene_scout.services.prompt_loader import render_prompt

_JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You are a local neighborhood guide for SceneScout. "
    "Narrate only the places you are given. "
    'Return only valid JSON with "neighborhood_context" and '
    '"neighborhood_confidence".'
)

NeighborhoodMode = Literal["geocoding-assisted", "llm-fallback"]


class NeighborhoodScoutLLMOutput(BaseModel):
    """LLM response schema for a single event."""

    neighborhood_context: str | None = None
    neighborhood_confidence: float

    @field_validator("neighborhood_confidence")
    @classmethod
    def _validate_confidence(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("neighborhood_confidence must be between 0.0 and 1.0")
        return value


@dataclass(frozen=True)
class NeighborhoodScoutJob:
    """Prepared batch work for one event."""

    event: EnrichedEvent
    venue_key: str
    mode: NeighborhoodMode
    poi_list: list[dict[str, Any]]
    coordinates: tuple[float, float] | None


def format_event_time(event: EnrichedEvent) -> str:
    """Format the event start time for prompt injection."""
    return event.start_datetime.isoformat()


def format_poi_list(pois: list[dict[str, Any]]) -> str:
    """Render POI dicts as structured prompt context."""
    if not pois:
        return "None"
    lines: list[str] = []
    for poi in pois:
        name = poi.get("name", "Unknown")
        poi_type = poi.get("type", "place")
        lines.append(f"- {name} ({poi_type})")
    return "\n".join(lines)


def apply_confidence_threshold(
    context: str | None,
    confidence: float,
) -> tuple[str | None, float]:
    """Return ``(context, confidence)`` with context cleared below threshold."""
    if confidence < NEIGHBORHOOD_CONFIDENCE_THRESHOLD:
        return None, confidence
    return context, confidence


def is_context_cache_hit(
    cached_context: str | None,
    cached_confidence: float | None,
) -> bool:
    """Return True when valid neighborhood context fields are present in cache."""
    return cached_context is not None or cached_confidence is not None


def resolve_neighborhood_from_cache(
    venue_key: str,
    cache: CacheService,
) -> tuple[str | None, float, tuple[float, float] | None] | None:
    """Return cached neighborhood fields when context TTL is valid."""
    cached = cache.get_venue(venue_key)
    if cached is None:
        return None
    if not is_context_cache_hit(
        cached.neighborhood_context,
        cached.neighborhood_confidence,
    ):
        return None
    confidence = cached.neighborhood_confidence or 0.0
    context, confidence = apply_confidence_threshold(
        cached.neighborhood_context,
        confidence,
    )
    return context, confidence, cached.coordinates


async def prepare_neighborhood_job(
    event: EnrichedEvent,
    *,
    cache: CacheService,
    run_id: str,
) -> NeighborhoodScoutJob:
    """Geocode a venue and load POIs for Mode A, or fall back to Mode B."""
    logger = get_logger("neighborhood_scout", run_id=run_id)
    venue_key = venue_cache_key(event.venue, event.city)
    cached = cache.get_venue(venue_key)

    coordinates = cached.coordinates if cached is not None else None
    poi_list = cached.poi_list if cached is not None else None
    mode: NeighborhoodMode = MODE_GEO_ASSISTED

    if coordinates is None:
        coordinates = await geocode_venue(
            event.venue,
            event.city,
            cache=cache,
            run_id=run_id,
        )

    if coordinates is None:
        logger.warning(
            "Geocoding failed; falling back to Mode B",
            data={
                "event_id": event.id,
                "venue": event.venue,
                "city": event.city,
            },
        )
        return NeighborhoodScoutJob(
            event=event,
            venue_key=venue_key,
            mode=MODE_LLM_FALLBACK,
            poi_list=[],
            coordinates=None,
        )

    if poi_list is None:
        poi_list = await get_nearby_pois(
            coordinates[0],
            coordinates[1],
            cache=cache,
            run_id=run_id,
            venue_key=venue_key,
        )

    return NeighborhoodScoutJob(
        event=event,
        venue_key=venue_key,
        mode=mode,
        poi_list=poi_list,
        coordinates=coordinates,
    )


def build_batch_request(job: NeighborhoodScoutJob) -> BatchRequest:
    """Build one batch request for a prepared neighborhood job."""
    event = job.event
    return BatchRequest(
        custom_id=event.id,
        prompt=render_prompt(
            "neighborhood_scout",
            venue=event.venue,
            city=event.city,
            neighborhood=event.neighborhood or "Unknown",
            event_time=format_event_time(event),
            mode=job.mode,
            poi_list=format_poi_list(job.poi_list),
        ),
        system=_SYSTEM_PROMPT,
        agent_name="neighborhood_scout",
    )


def _parse_batch_json(content: str) -> Any:
    text = content.strip()
    if not text:
        raise ValueError("LLM response was empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_FENCE_PATTERN.search(text)
        if match is None:
            raise
        return json.loads(match.group(1).strip())


def parse_batch_result(content: str) -> NeighborhoodScoutLLMOutput:
    """Parse and validate one batch LLM response."""
    payload = _parse_batch_json(content)
    return NeighborhoodScoutLLMOutput.model_validate(payload)


def apply_neighborhood_fields(
    event: EnrichedEvent,
    *,
    context: str | None,
    confidence: float,
    coordinates: tuple[float, float] | None,
) -> EnrichedEvent:
    """Return a copy of ``event`` with neighborhood fields populated."""
    return event.model_copy(
        update={
            "neighborhood_context": context,
            "neighborhood_confidence": confidence,
            "venue_coordinates": coordinates,
        }
    )


def _empty_neighborhood_event(
    event: EnrichedEvent,
    coordinates: tuple[float, float] | None = None,
) -> EnrichedEvent:
    return apply_neighborhood_fields(
        event,
        context=None,
        confidence=0.0,
        coordinates=coordinates,
    )


async def apply_batch_results(
    jobs: list[NeighborhoodScoutJob],
    batch_results: BatchResults,
    *,
    cache: CacheService,
    run_id: str,
) -> list[EnrichedEvent]:
    """Apply completed batch results to prepared neighborhood jobs."""
    logger = get_logger("neighborhood_scout", run_id=run_id)
    results_by_id = {item.custom_id: item for item in batch_results.results}
    enriched: list[EnrichedEvent] = []

    for job in jobs:
        result = results_by_id.get(job.event.id)
        if result is None or not result.success or not result.content:
            logger.warning(
                "Neighborhood Scout batch item failed for event",
                data={
                    "event_id": job.event.id,
                    "title": job.event.title,
                    "error": result.error if result else "missing batch result",
                },
            )
            enriched.append(_empty_neighborhood_event(job.event, job.coordinates))
            continue

        try:
            llm_output = parse_batch_result(result.content)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Neighborhood Scout validation error for event",
                data={
                    "event_id": job.event.id,
                    "title": job.event.title,
                    "error": str(exc),
                },
            )
            enriched.append(_empty_neighborhood_event(job.event, job.coordinates))
            continue

        context, confidence = apply_confidence_threshold(
            llm_output.neighborhood_context,
            llm_output.neighborhood_confidence,
        )
        cache.set_venue(
            job.venue_key,
            coordinates=job.coordinates,
            poi_list=job.poi_list if job.mode == MODE_GEO_ASSISTED else None,
            neighborhood_context=context,
            neighborhood_confidence=confidence,
        )
        enriched.append(
            apply_neighborhood_fields(
                job.event,
                context=context,
                confidence=confidence,
                coordinates=job.coordinates,
            )
        )

    return enriched


async def run(
    events: list[EnrichedEvent],
    run_id: str,
    *,
    cache: CacheService,
    batch_strategy: BatchStrategy | None = None,
    batch_results: BatchResults | None = None,
    prepared_jobs: list[NeighborhoodScoutJob] | None = None,
) -> list[EnrichedEvent]:
    """Enrich events with neighborhood context via cache, geocoding, and batch LLM.

    Parameters
    ----------
    events : list[EnrichedEvent]
        Events entering neighborhood enrichment.
    run_id : str
        Pipeline run identifier for logging.
    cache : CacheService
        SQLite cache including ``venue_cache``.
    batch_strategy : BatchStrategy | None
        Batch submit/poll implementation. Defaults to model-based routing.
    batch_results : BatchResults | None
        Pre-fetched batch results (used by orchestrator phase 2).
    prepared_jobs : list[NeighborhoodScoutJob] | None
        Pre-prepared jobs aligned with ``batch_results`` when orchestrator phase 1
        already geocoded venues.

    Returns
    -------
    list[EnrichedEvent]
        Events with neighborhood fields populated.
    """
    logger = get_logger("neighborhood_scout", run_id=run_id)
    strategy = batch_strategy or get_batch_strategy()
    enriched_by_id: dict[str, EnrichedEvent] = {}
    batch_jobs: list[NeighborhoodScoutJob] = []
    cache_hits = 0

    if prepared_jobs is not None:
        batch_jobs = prepared_jobs
        for event in events:
            if event.id not in {job.event.id for job in prepared_jobs}:
                enriched_by_id[event.id] = _empty_neighborhood_event(event)
    else:
        for event in events:
            venue_key = venue_cache_key(event.venue, event.city)
            cached_result = resolve_neighborhood_from_cache(venue_key, cache)
            if cached_result is not None:
                context, confidence, coordinates = cached_result
                cache_hits += 1
                enriched_by_id[event.id] = apply_neighborhood_fields(
                    event,
                    context=context,
                    confidence=confidence,
                    coordinates=coordinates,
                )
                continue

            job = await prepare_neighborhood_job(event, cache=cache, run_id=run_id)
            batch_jobs.append(job)

    if batch_jobs:
        if batch_results is None:
            requests = [build_batch_request(job) for job in batch_jobs]
            batch_id = await strategy.submit(requests, run_id=run_id)
            polled = await strategy.poll(batch_id)
            while polled.status == "processing":
                polled = await strategy.poll(batch_id)
            batch_results = polled

        batch_applied = await apply_batch_results(
            batch_jobs,
            batch_results,
            cache=cache,
            run_id=run_id,
        )
        for event in batch_applied:
            enriched_by_id[event.id] = event

    logger.info(
        "Neighborhood Scout complete",
        data={
            "events_processed": len(events),
            "venue_context_cache_hits": cache_hits,
            "batch_events": len(batch_jobs),
        },
    )
    return [enriched_by_id[event.id] for event in events]
