"""
Talent Scout Agent

Responsibility
--------------
Identify named performers in event descriptions, enrich them with context, and populate
``EnrichedEvent.performers`` via the batch strategy.

Design
------
Inputs  : list[NormalizedEvent], stated_interests, run_id, cache, batch_strategy
Outputs : list[EnrichedEvent] with performers and top_performer_affinity
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from scene_scout.agents.description_quality import has_named_performer
from scene_scout.logging import get_logger
from scene_scout.models.enrichment import EnrichedEvent, PerformerInfo
from scene_scout.models.event import NormalizedEvent
from scene_scout.services.batch import (
    BatchRequest,
    BatchResults,
    BatchStrategy,
    get_batch_strategy,
)
from scene_scout.services.cache import CacheService
from scene_scout.services.prompt_loader import render_prompt
from scene_scout.talent_scout_config import (
    CONFIDENCE_SUMMARY_THRESHOLD,
    VALID_ENTITY_TYPES,
)

_JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You are a performer identification specialist for SceneScout. "
    'Return only valid JSON with a single key "performers".'
)

_TALENT_NAME_PATTERNS = (
    re.compile(
        r"\b(?:featuring|feat\.?|starring|presents|presented by)\s+"
        r"([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwith special guest\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)",
        re.IGNORECASE,
    ),
    re.compile(r"\bDJ\s+([A-Z][\w'-]+)", re.IGNORECASE),
)


class TalentScoutPerformerLLMOutput(BaseModel):
    """Single performer returned by the Talent Scout LLM."""

    name: str
    entity_type: str
    genre_tags: list[str] = Field(default_factory=list)
    one_line_summary: str | None = None
    confidence: float
    affinity_score: float

    @field_validator("confidence", "affinity_score")
    @classmethod
    def _validate_unit_score(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                "confidence and affinity_score must be between 0.0 and 1.0"
            )
        return value

    @field_validator("entity_type")
    @classmethod
    def _validate_entity_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_ENTITY_TYPES:
            raise ValueError(f"entity_type must be one of {sorted(VALID_ENTITY_TYPES)}")
        return normalized


class TalentScoutLLMOutput(BaseModel):
    """LLM response schema for a single event."""

    performers: list[TalentScoutPerformerLLMOutput] = Field(default_factory=list)


def normalize_performer_name(name: str) -> str:
    """Return the normalized performer cache key."""
    return " ".join(name.strip().lower().split())


def format_stated_interests(stated_interests: list[str] | None) -> str:
    """Format user interests for prompt injection."""
    if not stated_interests:
        return "None specified."
    return ", ".join(stated_interests)


def extract_candidate_performer_names(title: str, description: str) -> list[str]:
    """Extract specific performer names mentioned in title or description."""
    combined = f"{title}\n{description}"
    names: list[str] = []
    seen: set[str] = set()
    for pattern in _TALENT_NAME_PATTERNS:
        for match in pattern.finditer(combined):
            name = match.group(1).strip()
            if not name:
                continue
            key = normalize_performer_name(name)
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
    return names


def prepare_performer_for_cache(performer: PerformerInfo) -> PerformerInfo:
    """Strip summaries for low-confidence performers before cache storage."""
    if performer.confidence < CONFIDENCE_SUMMARY_THRESHOLD:
        return performer.model_copy(update={"one_line_summary": None})
    return performer


def compute_top_performer_affinity(performers: list[PerformerInfo]) -> float:
    """Return the highest performer affinity score for an event."""
    if not performers:
        return 0.0
    return max(performer.affinity_score for performer in performers)


def resolve_performers_from_cache(
    title: str,
    description: str,
    cache: CacheService,
) -> list[PerformerInfo] | None:
    """Return cached performers when every extracted name is in performer_cache."""
    candidates = extract_candidate_performer_names(title, description)
    if not candidates:
        return None

    performers: list[PerformerInfo] = []
    for name in candidates:
        cached = cache.get_performer(normalize_performer_name(name))
        if cached is None:
            return None
        performers.append(cached)
    return performers


def build_batch_request(
    event: NormalizedEvent,
    stated_interests: list[str] | None,
) -> BatchRequest:
    """Build one batch request for an uncached event."""
    return BatchRequest(
        custom_id=event.id,
        prompt=render_prompt(
            "talent_scout",
            title=event.title,
            description=event.description,
            categories=", ".join(event.categories) or "None",
            stated_interests=format_stated_interests(stated_interests),
        ),
        system=_SYSTEM_PROMPT,
        agent_name="talent_scout",
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


def parse_batch_result(content: str) -> TalentScoutLLMOutput:
    """Parse and validate one batch LLM response."""
    payload = _parse_batch_json(content)
    return TalentScoutLLMOutput.model_validate(payload)


def merge_llm_performers_with_cache(
    llm_performers: list[TalentScoutPerformerLLMOutput],
    cache: CacheService,
) -> list[PerformerInfo]:
    """Apply LLM performers, preferring ``performer_cache`` hits per name."""
    performers: list[PerformerInfo] = []
    for raw in llm_performers:
        cache_key = normalize_performer_name(raw.name)
        cached = cache.get_performer(cache_key)
        if cached is not None:
            performers.append(cached)
            continue

        performer = prepare_performer_for_cache(
            PerformerInfo.model_validate(raw.model_dump())
        )
        cache.set_performer(cache_key, performer)
        performers.append(performer)
    return performers


def apply_performers_to_event(
    event: NormalizedEvent,
    performers: list[PerformerInfo],
) -> EnrichedEvent:
    """Build an enriched event with performer fields populated."""
    enriched = EnrichedEvent.from_normalized(event)
    return enriched.model_copy(
        update={
            "performers": performers,
            "top_performer_affinity": compute_top_performer_affinity(performers),
        }
    )


def _empty_enriched_event(event: NormalizedEvent) -> EnrichedEvent:
    return apply_performers_to_event(event, [])


async def apply_batch_results(
    events: list[NormalizedEvent],
    batch_results: BatchResults,
    *,
    cache: CacheService,
    run_id: str,
) -> list[EnrichedEvent]:
    """Apply completed batch results to uncached events."""
    logger = get_logger("talent_scout", run_id=run_id)
    results_by_id = {item.custom_id: item for item in batch_results.results}
    enriched: list[EnrichedEvent] = []

    for event in events:
        result = results_by_id.get(event.id)
        if result is None or not result.success or not result.content:
            logger.warning(
                "Talent Scout batch item failed for event",
                data={
                    "event_id": event.id,
                    "title": event.title,
                    "error": result.error if result else "missing batch result",
                },
            )
            enriched.append(_empty_enriched_event(event))
            continue

        try:
            llm_output = parse_batch_result(result.content)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Talent Scout validation error for event",
                data={
                    "event_id": event.id,
                    "title": event.title,
                    "error": str(exc),
                },
            )
            enriched.append(_empty_enriched_event(event))
            continue

        performers = merge_llm_performers_with_cache(llm_output.performers, cache)
        enriched.append(apply_performers_to_event(event, performers))

    return enriched


async def run(
    events: list[NormalizedEvent],
    stated_interests: list[str] | None,
    run_id: str,
    *,
    cache: CacheService,
    batch_strategy: BatchStrategy | None = None,
    batch_results: BatchResults | None = None,
) -> list[EnrichedEvent]:
    """Identify performers for normalized events via cache and batch LLM calls.

    Parameters
    ----------
    events : list[NormalizedEvent]
        Filtered events entering enrichment.
    stated_interests : list[str] | None
        User interest strings for affinity scoring in prompts.
    run_id : str
        Pipeline run identifier for logging.
    cache : CacheService
        SQLite cache including ``performer_cache``.
    batch_strategy : BatchStrategy | None
        Batch submit/poll implementation. Defaults to model-based routing.
    batch_results : BatchResults | None
        Pre-fetched batch results (used by orchestrator phase 2). When set,
        ``batch_strategy`` is not called.

    Returns
    -------
    list[EnrichedEvent]
        Events with ``performers`` and ``top_performer_affinity`` populated.
    """
    logger = get_logger("talent_scout", run_id=run_id)
    strategy = batch_strategy or get_batch_strategy()
    enriched_by_id: dict[str, EnrichedEvent] = {}
    batch_pending: list[NormalizedEvent] = []
    cache_hits = 0

    for event in events:
        if not has_named_performer(event.title, event.description):
            enriched_by_id[event.id] = _empty_enriched_event(event)
            continue

        cached_performers = resolve_performers_from_cache(
            event.title,
            event.description,
            cache,
        )
        if cached_performers is not None:
            cache_hits += 1
            enriched_by_id[event.id] = apply_performers_to_event(
                event,
                cached_performers,
            )
            continue

        batch_pending.append(event)

    batch_applied: list[EnrichedEvent] = []
    if batch_pending:
        if batch_results is None:
            requests = [
                build_batch_request(event, stated_interests) for event in batch_pending
            ]
            batch_id = await strategy.submit(requests, run_id=run_id)
            polled = await strategy.poll(batch_id)
            while polled.status == "processing":
                polled = await strategy.poll(batch_id)
            batch_results = polled

        batch_applied = await apply_batch_results(
            batch_pending,
            batch_results,
            cache=cache,
            run_id=run_id,
        )
        for event in batch_applied:
            enriched_by_id[event.id] = event

    logger.info(
        "Talent Scout complete",
        data={
            "events_processed": len(events),
            "performer_cache_hits": cache_hits,
            "batch_events": len(batch_pending),
        },
    )
    return [enriched_by_id[event.id] for event in events]
