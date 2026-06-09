"""
Event Extraction Agent

Responsibility
--------------
Convert raw RSS feed entries into structured ``EventCandidate`` records via LLM
extraction. Discards non-events and invalid extractions at the record level.

Design
------
Inputs  : list[RawFeedEntry], run_id: str
Outputs : list[EventCandidate]
"""

from __future__ import annotations

from datetime import datetime, timezone

from scene_scout.logging import get_logger
from scene_scout.models.event import EventCandidate, EventCandidateLLMOutput
from scene_scout.models.feed import RawFeedEntry
from scene_scout.services.llm import (
    LLMInfrastructureError,
    LLMValidationError,
    complete,
)
from scene_scout.services.prompt_loader import render_prompt

_SYSTEM_PROMPT = (
    "You are an event extraction specialist for SceneScout. "
    "Return only valid JSON matching the requested schema."
)


async def run(entries: list[RawFeedEntry], run_id: str) -> list[EventCandidate]:
    """Extract event candidates from raw feed entries.

    Calls ``llm.complete()`` once per entry, validates LLM JSON against
    ``EventCandidateLLMOutput``, merges agent metadata into ``EventCandidate``,
    and discards entries where ``is_event`` is false.

    Parameters
    ----------
    entries : list[RawFeedEntry]
        Cache-miss feed entries to extract.
    run_id : str
        Pipeline run identifier for logging and provenance.

    Returns
    -------
    list[EventCandidate]
        Valid event candidates only.

    Raises
    ------
    LLMInfrastructureError
        On API outage or unrecoverable provider error (fail-fast).
    """
    logger = get_logger("event_extraction", run_id=run_id)
    candidates: list[EventCandidate] = []

    for entry in entries:
        entry_label = entry.title or entry.link or entry.feed_id
        try:
            llm_output = await complete(
                prompt=render_prompt("event_extraction", entry=entry),
                system=_SYSTEM_PROMPT,
                response_model=EventCandidateLLMOutput,
                run_id=run_id,
                agent_name="event_extraction",
            )
        except LLMInfrastructureError:
            raise
        except LLMValidationError as exc:
            logger.warning(
                "Skipping entry due to LLM validation error: %s",
                entry_label,
                data={
                    "feed_id": entry.feed_id,
                    "entry_link": entry.link,
                    "error": str(exc),
                },
            )
            continue

        if not llm_output.is_event:
            logger.info(
                "Discarding non-event entry: %s",
                entry_label,
                data={
                    "feed_id": entry.feed_id,
                    "entry_link": entry.link,
                    "reason": "is_event=False",
                    "extraction_confidence": llm_output.extraction_confidence,
                },
            )
            continue

        candidate = EventCandidate.from_llm_output(
            llm_output,
            source_feed=entry.feed_id,
            run_id=run_id,
            extracted_at=datetime.now(timezone.utc),
        )
        candidates.append(candidate)
        logger.debug(
            "Extracted event candidate: %s",
            candidate.title,
            data={
                "feed_id": entry.feed_id,
                "extraction_confidence": candidate.extraction_confidence,
            },
        )

    logger.info(
        "Event extraction complete",
        data={
            "entries_processed": len(entries),
            "candidates_returned": len(candidates),
        },
    )
    return candidates
