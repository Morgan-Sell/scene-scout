"""
SceneScout pipeline orchestrator.

Generates ``run_id``, sequences agent calls, persists ``PipelineState`` at the
batch boundary, and returns per-stage counts in ``PipelineResult``.

Phase 2.4 skeleton: all agents are stubs returning empty lists. Real agent
implementations replace stubs in later phases without changing the orchestration
shape.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scene_scout.agents import (
    deduplication,
    description_quality,
    event_extraction,
    event_normalization,
)
from scene_scout.agents.event_normalization import is_within_normalization_window
from scene_scout.config import vol_history_dir, vol_pipeline_state_dir
from scene_scout.logging import get_logger
from scene_scout.models.event import NormalizedEvent
from scene_scout.models.feed import RawFeedEntry
from scene_scout.pre_enrichment_filter_config import (
    PRE_ENRICHMENT_COMING_WEEK_DAYS,
    PRE_ENRICHMENT_HARD_EXCLUDE_DAYS,
)
from scene_scout.services.cache import CacheService

_PIPELINE_STATE_FILENAME = "pipeline_state.json"

PipelinePhase = str  # "phase_1" | "batch_submitted" | "phase_2" | "complete"


@dataclass
class PipelineState:
    """Persisted state written to ``vol-pipeline-state`` at the batch boundary.

    Used when Phase 1 completes and enrichment batch polling begins. Phase 2
    reads this state back before applying batch results.

    Parameters
    ----------
    run_id : str
        Pipeline run identifier.
    filtered_events : list[dict[str, Any]]
        Events that passed the pre-enrichment filter. Stored as JSON-serializable
        dicts until ``NormalizedEvent`` is wired in Phase 4.
    batch_id : str, optional
        Provider batch job identifier once enrichment batch is submitted.
    phase : str
        Current pipeline phase.
    """

    run_id: str
    filtered_events: list[dict[str, Any]] = field(default_factory=list)
    batch_id: str | None = None
    phase: PipelinePhase = "phase_1"

    def to_json(self) -> str:
        """Serialize this state to a JSON string.

        Returns
        -------
        str
            JSON representation suitable for ``vol-pipeline-state`` storage.
        """
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str) -> PipelineState:
        """Deserialize a ``PipelineState`` from JSON.

        Parameters
        ----------
        data : str
            JSON string previously produced by :meth:`to_json`.

        Returns
        -------
        PipelineState
            Reconstructed pipeline state.
        """
        payload = json.loads(data)
        return cls(
            run_id=payload["run_id"],
            filtered_events=payload.get("filtered_events", []),
            batch_id=payload.get("batch_id"),
            phase=payload.get("phase", "phase_1"),
        )


@dataclass
class PipelineResult:
    """Per-stage entry counts returned after a full pipeline run.

    Parameters
    ----------
    run_id : str
        Pipeline run identifier.
    user_prompt : str
        Cold-start or UAT prompt supplied to the orchestrator.
    raw_entries : int
        Raw feed entries fetched by Feed Scout.
    feeds_unchanged : int
        Feeds skipped via HTTP 304 Not Modified.
    seen_entries_cache_hits : int
        Entries retrieved from ``seen_entries`` cache, bypassing extraction.
    seen_entries_cache_misses : int
        Entries sent to the Extraction Agent after a cache miss.
    seen_entries_hit_rate_pct : float
        Percentage of raw entries served from ``seen_entries`` cache (0–100).
    extraction_candidates : int
        Event candidates returned by the Extraction Agent.
    normalized_events : int
        Events returned by the Normalization Agent.
    deduplicated_events : int
        Events returned by the Deduplication Agent.
    after_description_quality : int
        Events returned by the Description Quality Agent.
    after_pre_enrichment_filter : int
        Events that passed the pre-enrichment filter.
    pre_enrichment_discard_low_information : int
        Events discarded for ``low_information=True``.
    pre_enrichment_discard_outside_week : int
        Events discarded for falling outside the coming-week window.
    pre_enrichment_discard_exclude_window : int
        Events discarded for appearing in the 2-week recommendation exclude window.
    enriched_events : int
        Events returned after enrichment batch application.
    ranked_events : int
        Events returned by the Ranking Agent.
    after_sellout_risk : int
        Events returned by the Sell-Out Risk Agent.
    curated_recommendations : int
        Final recommendations from the Curator Agent.
    evaluation_flags : int
        Issues flagged by the Evaluation Agent.
    """

    run_id: str
    user_prompt: str
    raw_entries: int = 0
    feeds_unchanged: int = 0
    seen_entries_cache_hits: int = 0
    seen_entries_cache_misses: int = 0
    seen_entries_hit_rate_pct: float = 0.0
    extraction_candidates: int = 0
    normalized_events: int = 0
    deduplicated_events: int = 0
    after_description_quality: int = 0
    after_pre_enrichment_filter: int = 0
    pre_enrichment_discard_low_information: int = 0
    pre_enrichment_discard_outside_week: int = 0
    pre_enrichment_discard_exclude_window: int = 0
    enriched_events: int = 0
    ranked_events: int = 0
    after_sellout_risk: int = 0
    curated_recommendations: int = 0
    evaluation_flags: int = 0


def _pipeline_state_dir() -> Path:
    """Return the directory for persisted pipeline state.

    Returns
    -------
    Path
        Resolved pipeline state directory, created if it does not exist.
    """
    return vol_pipeline_state_dir()


def _pipeline_state_path() -> Path:
    """Return the canonical pipeline state file path."""
    return _pipeline_state_dir() / _PIPELINE_STATE_FILENAME


def write_pipeline_state(state: PipelineState) -> None:
    """Write ``PipelineState`` to ``vol-pipeline-state``.

    Parameters
    ----------
    state : PipelineState
        State to persist at the batch boundary or on failure.
    """
    _pipeline_state_path().write_text(state.to_json(), encoding="utf-8")


def read_pipeline_state() -> PipelineState | None:
    """Read ``PipelineState`` from ``vol-pipeline-state``.

    Returns
    -------
    PipelineState or None
        Persisted state if present, else ``None``.
    """
    path = _pipeline_state_path()
    if not path.is_file():
        return None
    return PipelineState.from_json(path.read_text(encoding="utf-8"))


def clear_pipeline_state() -> None:
    """Remove persisted pipeline state after a successful run."""
    path = _pipeline_state_path()
    if path.is_file():
        path.unlink()


def compute_entry_hash(entry: RawFeedEntry) -> str:
    """Return a stable cache key hash for a raw feed entry.

    Parameters
    ----------
    entry : RawFeedEntry
        Feed entry whose ``link`` and ``published_raw`` identify it within a feed.

    Returns
    -------
    str
        SHA-256 hex digest of ``link + published_raw`` (empty string for null fields).
    """
    link = entry.link or ""
    published_raw = entry.published_raw or ""
    payload = f"{link}{published_raw}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _seen_entries_hit_rate_pct(hits: int, misses: int) -> float:
    """Compute seen_entries cache hit rate as a percentage."""
    total = hits + misses
    if total == 0:
        return 0.0
    return round(100.0 * hits / total, 2)


def _partition_entries_by_seen_cache(
    entries: list[RawFeedEntry],
    cache: CacheService,
    logger: Any,
) -> tuple[list[NormalizedEvent], list[RawFeedEntry], int, int]:
    """Split feed entries into cache hits and extraction candidates.

    Parameters
    ----------
    entries : list[RawFeedEntry]
        Raw entries from Feed Scout.
    cache : CacheService
        Cache service for ``seen_entries`` lookups.
    logger
        Orchestrator logger for cache hit messages.

    Returns
    -------
    tuple[list[NormalizedEvent], list[RawFeedEntry], int, int]
        Cached normalized events, entries for extraction, hit count, miss count.
    """
    cached_events: list[NormalizedEvent] = []
    entries_for_extraction: list[RawFeedEntry] = []
    hits = 0
    misses = 0

    for entry in entries:
        entry_hash = compute_entry_hash(entry)
        cached = cache.get_seen_entry(entry.feed_id, entry_hash)
        if cached is not None:
            hits += 1
            cached_events.append(cached)
            logger.info(
                "seen_entries cache hit",
                data={
                    "feed_id": entry.feed_id,
                    "entry_hash": entry_hash,
                    "entry_title": entry.title,
                    "normalized_event_id": cached.id,
                },
            )
            continue

        misses += 1
        entries_for_extraction.append(entry)

    return cached_events, entries_for_extraction, hits, misses


def _find_source_entry(
    candidate: Any,
    entries: list[RawFeedEntry],
) -> RawFeedEntry | None:
    """Match an extraction candidate back to its source feed entry."""
    for entry in entries:
        if entry.feed_id == candidate.source_feed and entry.link == candidate.url:
            return entry
    return None


def _store_seen_entries_after_normalization(
    cache: CacheService,
    candidates: list[Any],
    normalized_events: list[NormalizedEvent],
    source_entries: list[RawFeedEntry],
) -> None:
    """Persist newly normalized events to ``seen_entries`` cache."""
    for candidate, normalized_event in zip(candidates, normalized_events, strict=True):
        source = _find_source_entry(candidate, source_entries)
        if source is None:
            continue
        cache.set_seen_entry(
            source.feed_id,
            compute_entry_hash(source),
            normalized_event,
        )


DISCARD_LOW_INFORMATION = "low_information"
DISCARD_OUTSIDE_WEEK = "outside_coming_week"
DISCARD_EXCLUDE_WINDOW = "in_exclude_window"


@dataclass
class PreEnrichmentFilterResult:
    """Outcome of the pre-enrichment filter."""

    events: list[NormalizedEvent]
    discards: dict[str, int]

    @property
    def total_discarded(self) -> int:
        return sum(self.discards.values())


def _load_hard_exclude_event_ids(
    *,
    now: datetime,
    exclude_days: int = PRE_ENRICHMENT_HARD_EXCLUDE_DAYS,
) -> set[str]:
    """Return event IDs recommended within the hard-exclude window.

    Reads ``vol-history/hard_exclude_index.json`` when present. Phase 7.2 replaces
    this lightweight index with the full recommendation history service.
    """
    index_path = vol_history_dir() / "hard_exclude_index.json"
    if not index_path.is_file():
        return set()

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    cutoff = now - timedelta(days=exclude_days)
    excluded: set[str] = set()

    for entry in entries:
        event_id = entry.get("event_id")
        recommended_at_raw = entry.get("recommended_at")
        if not event_id or not recommended_at_raw:
            continue
        recommended_at = datetime.fromisoformat(
            str(recommended_at_raw).replace("Z", "+00:00")
        )
        if recommended_at.astimezone(timezone.utc) >= cutoff.astimezone(timezone.utc):
            excluded.add(str(event_id))

    return excluded


def _pre_enrichment_discard_reason(
    event: NormalizedEvent,
    *,
    now: datetime,
    exclude_event_ids: set[str],
) -> str | None:
    """Return a discard reason or ``None`` when the event should proceed."""
    if event.low_information:
        return DISCARD_LOW_INFORMATION
    if event.id in exclude_event_ids:
        return DISCARD_EXCLUDE_WINDOW
    if not is_within_normalization_window(
        event.start_datetime,
        now=now,
        window_days=PRE_ENRICHMENT_COMING_WEEK_DAYS,
    ):
        return DISCARD_OUTSIDE_WEEK
    return None


def apply_pre_enrichment_filter(
    events: list[NormalizedEvent],
    run_id: str,
    *,
    now: datetime | None = None,
    exclude_event_ids: set[str] | None = None,
) -> PreEnrichmentFilterResult:
    """Filter events before enrichment.

    Discards records that are low-information, outside the coming week, or within
    the hard recommendation exclude window.

    Parameters
    ----------
    events : list[NormalizedEvent]
        Description-quality-scored events.
    run_id : str
        Pipeline run identifier for logging.
    now : datetime, optional
        Reference time for date-window checks.
    exclude_event_ids : set[str], optional
        Event IDs to hard-exclude; loaded from history when omitted.

    Returns
    -------
    PreEnrichmentFilterResult
        Passing events and per-reason discard counts.
    """
    logger = get_logger("orchestrator", run_id=run_id)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    excluded_ids = (
        exclude_event_ids
        if exclude_event_ids is not None
        else _load_hard_exclude_event_ids(now=reference)
    )

    discards = {
        DISCARD_LOW_INFORMATION: 0,
        DISCARD_OUTSIDE_WEEK: 0,
        DISCARD_EXCLUDE_WINDOW: 0,
    }
    passing: list[NormalizedEvent] = []

    for event in events:
        reason = _pre_enrichment_discard_reason(
            event,
            now=reference,
            exclude_event_ids=excluded_ids,
        )
        if reason is None:
            passing.append(event)
            continue

        discards[reason] += 1
        logger.info(
            "Pre-enrichment filter discard: %s",
            event.title,
            data={
                "event_id": event.id,
                "reason": reason,
                "start_datetime": event.start_datetime.isoformat(),
                "low_information": event.low_information,
            },
        )

    logger.info(
        "Pre-enrichment filter complete",
        data={
            "input_count": len(events),
            "passed_count": len(passing),
            "discards": discards,
        },
    )
    return PreEnrichmentFilterResult(events=passing, discards=discards)


# ---------------------------------------------------------------------------
# Agent stubs — replaced by real agents in later phases
# ---------------------------------------------------------------------------


async def _stub_user_preference(prompt: str, run_id: str) -> dict[str, Any]:
    """Stub for User Preference Agent."""
    return {}


async def _stub_feed_scout(run_id: str) -> tuple[list[Any], list[Any]]:
    """Stub for Feed Scout Agent."""
    return [], []


async def _stub_enrichment(events: list[Any], run_id: str) -> list[Any]:
    """Stub for enrichment batch application (Talent, Vibe, Neighborhood)."""
    return []


async def _stub_ranking(events: list[Any], run_id: str) -> list[Any]:
    """Stub for Ranking Agent."""
    return []


async def _stub_sellout_risk(events: list[Any], run_id: str) -> list[Any]:
    """Stub for Sell-Out Risk Agent."""
    return []


async def _stub_recommendation_curator(events: list[Any], run_id: str) -> list[Any]:
    """Stub for Recommendation Curator Agent."""
    return []


async def _stub_email_composer(recommendations: list[Any], run_id: str) -> None:
    """Stub for Email Composer Agent."""


async def _stub_evaluation(recommendations: list[Any], run_id: str) -> list[Any]:
    """Stub for Evaluation Agent."""
    return []


class Orchestrator:
    """Sequences the SceneScout pipeline from feed fetch through email send."""

    async def run(self, prompt: str) -> PipelineResult:
        """Execute the full pipeline skeleton for a user prompt.

        Generates ``run_id``, calls each agent stub in sequence, persists
        ``PipelineState`` at the batch boundary, and clears state on success.

        Parameters
        ----------
        prompt : str
            User cold-start or UAT prompt.

        Returns
        -------
        PipelineResult
            Per-stage entry counts for this run.
        """
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        logger = get_logger("orchestrator", run_id=run_id)
        result = PipelineResult(run_id=run_id, user_prompt=prompt)
        state = PipelineState(run_id=run_id)

        logger.info("Pipeline started", data={"user_prompt_length": len(prompt)})

        await _stub_user_preference(prompt, run_id)

        entries, _reports = await _stub_feed_scout(run_id)
        result.raw_entries = len(entries)
        result.feeds_unchanged = 0

        cache = CacheService(run_id=run_id)
        (
            cached_events,
            entries_for_extraction,
            cache_hits,
            cache_misses,
        ) = _partition_entries_by_seen_cache(entries, cache, logger)
        result.seen_entries_cache_hits = cache_hits
        result.seen_entries_cache_misses = cache_misses
        result.seen_entries_hit_rate_pct = _seen_entries_hit_rate_pct(
            cache_hits,
            cache_misses,
        )

        if entries_for_extraction:
            candidates = await event_extraction.run(entries_for_extraction, run_id)
        else:
            candidates = []
        result.extraction_candidates = len(candidates)

        newly_normalized = await event_normalization.run(candidates, run_id)
        if newly_normalized:
            _store_seen_entries_after_normalization(
                cache,
                candidates,
                newly_normalized,
                entries_for_extraction,
            )

        normalized = cached_events + newly_normalized
        result.normalized_events = len(normalized)

        deduplicated = await deduplication.run(normalized, run_id)
        result.deduplicated_events = len(deduplicated)

        quality_scored = await description_quality.run(deduplicated, run_id)
        result.after_description_quality = len(quality_scored)

        filter_result = apply_pre_enrichment_filter(quality_scored, run_id)
        filtered = filter_result.events
        result.after_pre_enrichment_filter = len(filtered)
        result.pre_enrichment_discard_low_information = filter_result.discards[
            DISCARD_LOW_INFORMATION
        ]
        result.pre_enrichment_discard_outside_week = filter_result.discards[
            DISCARD_OUTSIDE_WEEK
        ]
        result.pre_enrichment_discard_exclude_window = filter_result.discards[
            DISCARD_EXCLUDE_WINDOW
        ]

        state.filtered_events = [event.model_dump(mode="json") for event in filtered]
        state.phase = "batch_submitted"
        write_pipeline_state(state)
        logger.info(
            "Phase 1 complete; pipeline state persisted",
            data={"filtered_events": result.after_pre_enrichment_filter},
        )

        resumed = read_pipeline_state()
        if resumed is not None:
            state = resumed
        state.phase = "phase_2"
        write_pipeline_state(state)

        enriched = await _stub_enrichment(filtered, run_id)
        result.enriched_events = len(enriched)

        ranked = await _stub_ranking(enriched, run_id)
        result.ranked_events = len(ranked)

        risk_scored = await _stub_sellout_risk(ranked, run_id)
        result.after_sellout_risk = len(risk_scored)

        curated = await _stub_recommendation_curator(risk_scored, run_id)
        result.curated_recommendations = len(curated)

        await _stub_email_composer(curated, run_id)

        evaluation = await _stub_evaluation(curated, run_id)
        result.evaluation_flags = len(evaluation)

        state.phase = "complete"
        clear_pipeline_state()

        cache.log_run_stats()

        logger.info(
            "Pipeline complete",
            data={
                "raw_entries": result.raw_entries,
                "seen_entries_cache_hits": result.seen_entries_cache_hits,
                "seen_entries_cache_misses": result.seen_entries_cache_misses,
                "seen_entries_hit_rate_pct": result.seen_entries_hit_rate_pct,
                "after_pre_enrichment_filter": result.after_pre_enrichment_filter,
                "pre_enrichment_discards": {
                    "low_information": result.pre_enrichment_discard_low_information,
                    "outside_coming_week": result.pre_enrichment_discard_outside_week,
                    "in_exclude_window": result.pre_enrichment_discard_exclude_window,
                },
                "curated_recommendations": result.curated_recommendations,
            },
        )

        return result
