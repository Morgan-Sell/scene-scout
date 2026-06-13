"""
Vibe Classifier Agent

Responsibility
--------------
Assign 2–5 controlled vocabulary vibe tags to each enriched event via batch LLM calls.

Design
------
Inputs  : list[EnrichedEvent], run_id, cache, batch_strategy
Outputs : list[EnrichedEvent] with vibe_tags populated
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from scene_scout.logging import get_logger
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.services.batch import (
    BatchRequest,
    BatchResults,
    BatchStrategy,
    get_batch_strategy,
)
from scene_scout.services.cache import CacheService
from scene_scout.services.prompt_loader import render_prompt
from scene_scout.vibe_classifier_config import (
    MAX_VIBE_TAGS,
    MIN_VIBE_TAGS,
    VIBE_VOCABULARY,
)

_JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You are an event atmosphere classifier for SceneScout. "
    'Return only valid JSON with a single key "vibe_tags".'
)


class VibeClassifierLLMOutput(BaseModel):
    """LLM response schema for a single event."""

    vibe_tags: list[str] = Field(default_factory=list)


def compute_vibe_content_hash(description: str, categories: list[str]) -> str:
    """Return the ``vibe_cache`` key for an event's description and categories."""
    payload = description + json.dumps(sorted(categories), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def format_performers(event: EnrichedEvent) -> str:
    """Format performer names for prompt injection."""
    if not event.performers:
        return "None listed."
    return ", ".join(performer.name for performer in event.performers)


def validate_vibe_tags(raw_tags: list[str]) -> list[str] | None:
    """Return normalized tags when valid, otherwise ``None``.

    Rejects tags outside the controlled vocabulary, wrong counts, or duplicates
    that reduce the count below ``MIN_VIBE_TAGS``.
    """
    if len(raw_tags) < MIN_VIBE_TAGS or len(raw_tags) > MAX_VIBE_TAGS:
        return None

    normalized: list[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        candidate = tag.strip().lower()
        if candidate not in VIBE_VOCABULARY:
            return None
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)

    if len(normalized) < MIN_VIBE_TAGS:
        return None
    return normalized


def compute_tag_distribution(events: list[EnrichedEvent]) -> dict[str, int]:
    """Count how often each vibe tag appears across enriched events."""
    counts: Counter[str] = Counter()
    for event in events:
        counts.update(event.vibe_tags)
    return dict(sorted(counts.items()))


def build_batch_request(event: EnrichedEvent) -> BatchRequest:
    """Build one batch request for an uncached event."""
    return BatchRequest(
        custom_id=event.id,
        prompt=render_prompt(
            "vibe_classifier",
            title=event.title,
            description=event.description,
            categories=", ".join(event.categories) or "None",
            performers=format_performers(event),
        ),
        system=_SYSTEM_PROMPT,
        agent_name="vibe_classifier",
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


def parse_batch_result(content: str) -> VibeClassifierLLMOutput:
    """Parse and validate one batch LLM response."""
    payload = _parse_batch_json(content)
    return VibeClassifierLLMOutput.model_validate(payload)


def apply_vibe_tags(event: EnrichedEvent, tags: list[str]) -> EnrichedEvent:
    """Return a copy of ``event`` with ``vibe_tags`` set."""
    return event.model_copy(update={"vibe_tags": tags})


async def apply_batch_results(
    events: list[EnrichedEvent],
    batch_results: BatchResults,
    *,
    cache: CacheService,
    run_id: str,
) -> list[EnrichedEvent]:
    """Apply completed batch results to uncached events."""
    logger = get_logger("vibe_classifier", run_id=run_id)
    results_by_id = {item.custom_id: item for item in batch_results.results}
    enriched: list[EnrichedEvent] = []

    for event in events:
        result = results_by_id.get(event.id)
        if result is None or not result.success or not result.content:
            logger.warning(
                "Vibe Classifier batch item failed for event",
                data={
                    "event_id": event.id,
                    "title": event.title,
                    "error": result.error if result else "missing batch result",
                },
            )
            enriched.append(apply_vibe_tags(event, []))
            continue

        try:
            llm_output = parse_batch_result(result.content)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Vibe Classifier validation error for event",
                data={
                    "event_id": event.id,
                    "title": event.title,
                    "error": str(exc),
                },
            )
            enriched.append(apply_vibe_tags(event, []))
            continue

        validated = validate_vibe_tags(llm_output.vibe_tags)
        if validated is None:
            logger.warning(
                "Vibe Classifier rejected tags outside vocabulary or count",
                data={
                    "event_id": event.id,
                    "title": event.title,
                    "raw_tags": llm_output.vibe_tags,
                },
            )
            enriched.append(apply_vibe_tags(event, []))
            continue

        cache_key = compute_vibe_content_hash(event.description, event.categories)
        cache.set_vibe(cache_key, validated)
        enriched.append(apply_vibe_tags(event, validated))

    return enriched


async def run(
    events: list[EnrichedEvent],
    run_id: str,
    *,
    cache: CacheService,
    batch_strategy: BatchStrategy | None = None,
    batch_results: BatchResults | None = None,
) -> list[EnrichedEvent]:
    """Assign vibe tags to enriched events via cache and batch LLM calls.

    Parameters
    ----------
    events : list[EnrichedEvent]
        Events entering vibe classification (may already have performer fields).
    run_id : str
        Pipeline run identifier for logging.
    cache : CacheService
        SQLite cache including ``vibe_cache``.
    batch_strategy : BatchStrategy | None
        Batch submit/poll implementation. Defaults to model-based routing.
    batch_results : BatchResults | None
        Pre-fetched batch results (used by orchestrator phase 2). When set,
        ``batch_strategy`` is not called.

    Returns
    -------
    list[EnrichedEvent]
        Events with ``vibe_tags`` populated.
    """
    logger = get_logger("vibe_classifier", run_id=run_id)
    strategy = batch_strategy or get_batch_strategy()
    enriched_by_id: dict[str, EnrichedEvent] = {}
    batch_pending: list[EnrichedEvent] = []
    cache_hits = 0

    for event in events:
        cache_key = compute_vibe_content_hash(event.description, event.categories)
        cached_tags = cache.get_vibe(cache_key)
        if cached_tags is not None:
            cache_hits += 1
            enriched_by_id[event.id] = apply_vibe_tags(event, cached_tags)
            continue
        batch_pending.append(event)

    if batch_pending:
        if batch_results is None:
            requests = [build_batch_request(event) for event in batch_pending]
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

    results = [enriched_by_id[event.id] for event in events]
    logger.info(
        "Vibe Classifier complete",
        data={
            "events_processed": len(events),
            "vibe_cache_hits": cache_hits,
            "batch_events": len(batch_pending),
            "tag_distribution": compute_tag_distribution(results),
        },
    )
    return results
