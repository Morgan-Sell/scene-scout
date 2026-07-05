"""
SceneScout pipeline orchestrator.

Generates ``run_id``, sequences agent calls, persists ``PipelineState`` at the
batch boundary, and returns per-stage counts in ``PipelineResult``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scene_scout.agents import (
    deduplication,
    description_quality,
    email_composer,
    event_extraction,
    event_normalization,
    feed_scout,
    neighborhood_scout,
    ranking,
    recommendation_curator,
    sellout_risk,
    talent_scout,
    user_preference,
    vibe_classifier,
)
from scene_scout.agents.description_quality import has_named_performer
from scene_scout.agents.event_normalization import is_within_normalization_window
from scene_scout.agents.user_preference import UserProfileNotFoundError
from scene_scout.config import (
    get_user_email,
    get_user_name,
    load_feed_configs,
    vol_pipeline_state_dir,
)
from scene_scout.db import run_migrations
from scene_scout.logging import get_logger
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.event import NormalizedEvent
from scene_scout.models.feed import FeedStatus, RawFeedEntry
from scene_scout.models.ranking import RankedEvent
from scene_scout.models.user import UserProfile
from scene_scout.orchestrator_config import (
    ENRICHMENT_BATCH_POLL_INTERVAL_SECONDS,
    UatRunOptions,
    resolve_uat_home_city,
    resolve_uat_horizon_days,
    select_feed_configs,
)
from scene_scout.pre_enrichment_filter_config import (
    PRE_ENRICHMENT_COMING_WEEK_DAYS,
    PRE_ENRICHMENT_HARD_EXCLUDE_DAYS,
)
from scene_scout.services import history as history_service
from scene_scout.services.batch import BatchRequest, BatchResults, get_batch_strategy
from scene_scout.services.cache import CacheService
from scene_scout.services.geocoding import venue_cache_key

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
    neighborhood_jobs : list[dict[str, Any]]
        Serialized neighborhood scout jobs prepared during Phase 1 geocoding.
    stated_interests : list[str]
        User interest strings forwarded to the Talent Scout batch prompts.
    phase : str
        Current pipeline phase.
    """

    run_id: str
    filtered_events: list[dict[str, Any]] = field(default_factory=list)
    batch_id: str | None = None
    neighborhood_jobs: list[dict[str, Any]] = field(default_factory=list)
    stated_interests: list[str] = field(default_factory=list)
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
            neighborhood_jobs=payload.get("neighborhood_jobs", []),
            stated_interests=payload.get("stated_interests", []),
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
        Issues flagged by the Evaluation Agent (Phase 9 — not yet wired).
    feeds_fetched : int
        Number of active feeds processed by Feed Scout.
    feed_health : list[dict[str, Any]]
        Per-feed status from Feed Scout (status, entries, error).
    enrichment_cache_hit_rates_pct : dict[str, float]
        Hit-rate percentages for performer, venue, and vibe caches.
    top_recommendations : list[dict[str, Any]]
        Top ranked events with title, score, source_count, and source_coverage.
    email_preview_path : str | None
        Path to ``email_preview.html`` when Email Composer ran.
    email_sent : bool
        Whether Resend delivery succeeded (``False`` during dry-run).
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
    feeds_fetched: int = 0
    feed_health: list[dict[str, Any]] = field(default_factory=list)
    enrichment_cache_hit_rates_pct: dict[str, float] = field(default_factory=dict)
    top_recommendations: list[dict[str, Any]] = field(default_factory=list)
    email_preview_path: str | None = None
    email_sent: bool = False
    last_completed_stage: str = ""


class PipelineRunError(Exception):
    """Raised when the pipeline fails after partial progress."""

    def __init__(self, result: PipelineResult, cause: BaseException) -> None:
        self.result = result
        self.cause = cause
        super().__init__(str(cause))


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


def _find_candidate_for_normalized_event(
    normalized_event: NormalizedEvent,
    candidates: list[Any],
) -> Any | None:
    """Match a normalized event back to its extraction candidate."""
    for candidate in candidates:
        if (
            candidate.source_feed == normalized_event.best_source_feed
            and candidate.url.strip() == normalized_event.url
            and candidate.title.strip() == normalized_event.title
        ):
            return candidate
    return None


def _store_seen_entries_after_normalization(
    cache: CacheService,
    candidates: list[Any],
    normalized_events: list[NormalizedEvent],
    source_entries: list[RawFeedEntry],
) -> None:
    """Persist newly normalized events to ``seen_entries`` cache.

    Only successfully normalized events are cached. ``candidates`` may be longer
    than ``normalized_events`` when normalization discards rows.
    """
    for normalized_event in normalized_events:
        candidate = _find_candidate_for_normalized_event(normalized_event, candidates)
        if candidate is None:
            continue
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
    """Return event IDs recommended within the hard-exclude window."""
    if exclude_days != PRE_ENRICHMENT_HARD_EXCLUDE_DAYS:
        return history_service.get_recommended_event_ids(
            exclude_days,
            now=now,
        )
    return history_service.get_hard_exclude_event_ids(now=now)


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


def _batch_custom_id(agent_name: str, event_id: str) -> str:
    """Return a unique batch ``custom_id`` for one agent and event."""
    return f"{agent_name}:{event_id}"


def _batch_results_for_agent(
    batch_results: BatchResults,
    agent_name: str,
) -> BatchResults:
    """Extract one agent's batch items and restore bare event IDs."""
    prefix = f"{agent_name}:"
    items = [
        item.model_copy(update={"custom_id": item.custom_id.removeprefix(prefix)})
        for item in batch_results.results
        if item.custom_id.startswith(prefix)
    ]
    return BatchResults(
        batch_id=batch_results.batch_id,
        status=batch_results.status,
        results=items,
    )


def _serialize_neighborhood_job(
    job: neighborhood_scout.NeighborhoodScoutJob,
) -> dict[str, Any]:
    coordinates = job.coordinates
    return {
        "event": job.event.model_dump(mode="json"),
        "venue_key": job.venue_key,
        "mode": job.mode,
        "poi_list": job.poi_list,
        "coordinates": list(coordinates) if coordinates is not None else None,
    }


def _deserialize_neighborhood_job(
    payload: dict[str, Any],
) -> neighborhood_scout.NeighborhoodScoutJob:
    raw_coordinates = payload.get("coordinates")
    return neighborhood_scout.NeighborhoodScoutJob(
        event=EnrichedEvent.model_validate(payload["event"]),
        venue_key=str(payload["venue_key"]),
        mode=payload["mode"],
        poi_list=payload.get("poi_list", []),
        coordinates=tuple(raw_coordinates) if raw_coordinates else None,
    )


def _stated_interests_from_prompt(prompt: str) -> list[str]:
    """Extract stated interests for Talent Scout prompts."""
    stripped = prompt.strip()
    if not stripped:
        return []
    return [stripped]


async def _collect_enrichment_batch_requests(
    filtered: list[NormalizedEvent],
    *,
    cache: CacheService,
    stated_interests: list[str] | None,
    run_id: str,
) -> tuple[list[BatchRequest], list[neighborhood_scout.NeighborhoodScoutJob]]:
    """Build one combined batch request list for all enrichment agents."""
    requests: list[BatchRequest] = []
    neighborhood_jobs: list[neighborhood_scout.NeighborhoodScoutJob] = []

    for event in filtered:
        if has_named_performer(event.title, event.description):
            if (
                talent_scout.resolve_performers_from_cache(
                    event.title,
                    event.description,
                    cache,
                )
                is None
            ):
                talent_request = talent_scout.build_batch_request(
                    event,
                    stated_interests,
                )
                requests.append(
                    talent_request.model_copy(
                        update={
                            "custom_id": _batch_custom_id(
                                "talent_scout",
                                event.id,
                            )
                        }
                    )
                )

        vibe_key = vibe_classifier.compute_vibe_content_hash(
            event.description,
            event.categories,
        )
        if cache.get_vibe(vibe_key) is None:
            vibe_request = vibe_classifier.build_batch_request(
                EnrichedEvent.from_normalized(event)
            )
            requests.append(
                vibe_request.model_copy(
                    update={"custom_id": _batch_custom_id("vibe_classifier", event.id)}
                )
            )

        enriched = EnrichedEvent.from_normalized(event)
        venue_key = venue_cache_key(event.venue, event.city)
        if neighborhood_scout.resolve_neighborhood_from_cache(venue_key, cache) is None:
            job = await neighborhood_scout.prepare_neighborhood_job(
                enriched,
                cache=cache,
                run_id=run_id,
            )
            neighborhood_jobs.append(job)
            neighborhood_request = neighborhood_scout.build_batch_request(job)
            requests.append(
                neighborhood_request.model_copy(
                    update={
                        "custom_id": _batch_custom_id(
                            "neighborhood_scout",
                            event.id,
                        )
                    }
                )
            )

    return requests, neighborhood_jobs


async def _poll_enrichment_batch(
    batch_id: str,
    *,
    run_id: str,
) -> BatchResults:
    """Poll provider batch status every five minutes until completion."""
    logger = get_logger("orchestrator", run_id=run_id)
    strategy = get_batch_strategy()

    while True:
        results = await strategy.poll(batch_id)
        if results.status != "processing":
            logger.info(
                "Enrichment batch polling complete",
                data={"batch_id": batch_id, "status": results.status},
            )
            return results

        logger.info(
            "Enrichment batch still processing; sleeping",
            data={
                "batch_id": batch_id,
                "poll_interval_seconds": ENRICHMENT_BATCH_POLL_INTERVAL_SECONDS,
            },
        )
        await asyncio.sleep(ENRICHMENT_BATCH_POLL_INTERVAL_SECONDS)


async def _apply_enrichment_batch(
    filtered: list[NormalizedEvent],
    batch_results: BatchResults,
    neighborhood_jobs: list[neighborhood_scout.NeighborhoodScoutJob],
    stated_interests: list[str] | None,
    *,
    cache: CacheService,
    run_id: str,
) -> list[EnrichedEvent]:
    """Apply a completed enrichment batch through all three agents."""
    logger = get_logger("orchestrator", run_id=run_id)

    if batch_results.status == "failed":
        logger.warning(
            "Enrichment batch failed; applying empty agent fallbacks",
            data={"batch_id": batch_results.batch_id},
        )

    talent_results = _batch_results_for_agent(batch_results, "talent_scout")
    vibe_results = _batch_results_for_agent(batch_results, "vibe_classifier")
    neighborhood_results = _batch_results_for_agent(
        batch_results,
        "neighborhood_scout",
    )

    after_talent = await talent_scout.run(
        filtered,
        stated_interests,
        run_id,
        cache=cache,
        batch_results=talent_results,
    )
    after_vibe = await vibe_classifier.run(
        after_talent,
        run_id,
        cache=cache,
        batch_results=vibe_results,
    )
    enriched = await neighborhood_scout.run(
        after_vibe,
        run_id,
        cache=cache,
        batch_results=neighborhood_results,
        prepared_jobs=neighborhood_jobs if neighborhood_jobs else None,
    )

    logger.info(
        "Enrichment batch applied",
        data={"enriched_events": len(enriched)},
    )
    return enriched


def _top_recommendation_rows(
    ranked: list[RankedEvent],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Serialize top ranked events for UAT summary output."""
    rows: list[dict[str, Any]] = []
    for ranked_event in ranked[:limit]:
        breakdown = ranked_event.score_breakdown
        rows.append(
            {
                "title": ranked_event.event.title,
                "score": round(ranked_event.score, 4),
                "source_count": ranked_event.event.source_count,
                "source_coverage": round(breakdown.get("source_coverage", 0.0), 4),
                "wildcard_slot": ranked_event.wildcard_slot,
            }
        )
    return rows


async def _resolve_user_profile(
    prompt: str,
    run_id: str,
    *,
    home_city: str | None = None,
    horizon_days: int | None = None,
) -> UserProfile:
    """Load a persisted profile or parse one from the UAT prompt."""
    logger = get_logger("orchestrator", run_id=run_id)
    try:
        profile = user_preference.load_profile()
        logger.info(
            "Loaded persisted user profile",
            data={
                "user_id": profile.user_id,
                "name": profile.name,
                "home_city": profile.home_city,
                "horizon_days": profile.horizon_days,
            },
        )
        return profile
    except UserProfileNotFoundError:
        user_email = get_user_email()
        user_name = get_user_name()
        if not user_email:
            raise RuntimeError(
                "No user profile found and USER_EMAIL is not configured. "
                "Complete web onboarding or set USER_EMAIL in .env before UAT."
            ) from None

        resolved_city = home_city or resolve_uat_home_city(None)
        resolved_horizon = (
            horizon_days
            if horizon_days is not None
            else (resolve_uat_horizon_days(None))
        )
        if not resolved_city:
            raise RuntimeError(
                "No user profile found and home city is not configured. "
                "Complete web onboarding or pass --city / set UAT_HOME_CITY before UAT."
            ) from None
        if resolved_horizon is None:
            raise RuntimeError(
                "No user profile found and horizon is not configured. "
                "Complete web onboarding or pass --horizon-days / set "
                "UAT_HORIZON_DAYS before UAT."
            ) from None

        logger.info(
            "No persisted profile — parsing cold-start from UAT prompt",
            data={
                "email": user_email,
                "name": user_name,
                "home_city": resolved_city,
                "horizon_days": resolved_horizon,
            },
        )
        return await user_preference.parse_cold_start(
            name=user_name,
            email=user_email,
            prompt=prompt,
            run_id=run_id,
            home_city=resolved_city,
            horizon_days=resolved_horizon,
        )


def _uat_run_dir(uat_output_base: Path | None, run_id: str) -> Path | None:
    if uat_output_base is None:
        return None
    run_dir = uat_output_base / f"uat_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_uat_stage_checkpoint(
    uat_dir: Path | None,
    result: PipelineResult,
    stage: str,
) -> None:
    if uat_dir is None:
        return
    from scene_scout.uat_artifacts import write_uat_checkpoint

    result.last_completed_stage = stage
    write_uat_checkpoint(uat_dir, result, stage)


def _cap_extraction_entries(
    entries: list[RawFeedEntry],
    max_extraction: int | None,
    logger: Any,
) -> list[RawFeedEntry]:
    """Limit cache-miss entries sent to the Extraction Agent."""
    if max_extraction is None or len(entries) <= max_extraction:
        return entries
    logger.info(
        "Capping extraction entries",
        data={"before": len(entries), "cap": max_extraction},
    )
    return entries[:max_extraction]


def _should_stop_after(
    stop_after: str | None,
    stage: str,
) -> bool:
    return stop_after == stage


class Orchestrator:
    """Sequences the SceneScout pipeline from feed fetch through email send."""

    async def run(
        self,
        prompt: str,
        *,
        uat_output_base: Path | None = None,
        uat_options: UatRunOptions | None = None,
    ) -> PipelineResult:
        """Execute the full pipeline skeleton for a user prompt.

        Generates ``run_id``, calls each agent stub in sequence, persists
        ``PipelineState`` at the batch boundary, and clears state on success.

        Parameters
        ----------
        prompt : str
            User cold-start or UAT prompt.
        uat_output_base : Path | None, optional
            When set (UAT CLI), writes ``checkpoint.json`` under
            ``{uat_output_base}/uat_{run_id}/`` after major early stages.
        uat_options : UatRunOptions | None, optional
            Abbreviated UAT limits (feed subset, extraction cap, early stop).

        Returns
        -------
        PipelineResult
            Per-stage entry counts for this run.

        Raises
        ------
        PipelineRunError
            When a stage fails after partial progress; carries the partial
            ``PipelineResult`` for failure artifact writes.
        """
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        logger = get_logger("orchestrator", run_id=run_id)
        result = PipelineResult(run_id=run_id, user_prompt=prompt)
        state = PipelineState(run_id=run_id)
        uat_dir = _uat_run_dir(uat_output_base, run_id)
        options = uat_options or UatRunOptions()

        logger.info("Pipeline started", data={"user_prompt_length": len(prompt)})

        try:
            return await self._run_pipeline(
                prompt,
                run_id=run_id,
                logger=logger,
                result=result,
                state=state,
                uat_dir=uat_dir,
                uat_options=options,
            )
        except Exception as exc:
            raise PipelineRunError(result, exc) from exc

    async def _run_pipeline(
        self,
        prompt: str,
        *,
        run_id: str,
        logger: Any,
        result: PipelineResult,
        state: PipelineState,
        uat_dir: Path | None,
        uat_options: UatRunOptions,
    ) -> PipelineResult:
        run_migrations()

        profile = await _resolve_user_profile(
            prompt,
            run_id,
            home_city=uat_options.home_city,
            horizon_days=uat_options.horizon_days,
        )

        cache = CacheService(run_id=run_id)
        feed_configs = select_feed_configs(
            load_feed_configs(home_city=profile.home_city),
            uat_options.feed_ids,
        )
        entries, feed_reports = await feed_scout.run(
            feed_configs,
            run_id,
            get_feed_etag=cache.get_feed_etag,
            store_feed_etag=cache.set_feed_etag,
            home_city=profile.home_city,
        )
        result.raw_entries = len(entries)
        result.feeds_fetched = len(feed_reports)
        result.feed_health = [
            {
                "feed_id": report.feed_id,
                "feed_name": report.feed_name,
                "status": report.status.value,
                "entries_fetched": report.entries_fetched,
                "error_message": report.error_message,
            }
            for report in feed_reports
        ]
        result.feeds_unchanged = sum(
            1 for report in feed_reports if report.status == FeedStatus.UNCHANGED
        )
        _write_uat_stage_checkpoint(uat_dir, result, "feed_scout")

        if _should_stop_after(uat_options.stop_after, "feeds"):
            result.last_completed_stage = "feeds"
            return result

        (
            cached_events,
            entries_for_extraction,
            cache_hits,
            cache_misses,
        ) = _partition_entries_by_seen_cache(entries, cache, logger)
        entries_for_extraction = _cap_extraction_entries(
            entries_for_extraction,
            uat_options.max_extraction,
            logger,
        )
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
        _write_uat_stage_checkpoint(uat_dir, result, "extraction")

        if _should_stop_after(uat_options.stop_after, "extract"):
            result.last_completed_stage = "extract"
            return result

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
        _write_uat_stage_checkpoint(uat_dir, result, "normalization")

        if _should_stop_after(uat_options.stop_after, "normalize"):
            result.last_completed_stage = "normalize"
            return result

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

        stated_interests = _stated_interests_from_prompt(prompt)
        batch_requests, neighborhood_jobs = await _collect_enrichment_batch_requests(
            filtered,
            cache=cache,
            stated_interests=stated_interests,
            run_id=run_id,
        )

        if batch_requests:
            strategy = get_batch_strategy()
            batch_id = await strategy.submit(batch_requests, run_id=run_id)
            batch_results = await _poll_enrichment_batch(batch_id, run_id=run_id)
        else:
            batch_id = None
            batch_results = BatchResults(
                batch_id="",
                status="completed",
                results=[],
            )
            logger.info(
                "No enrichment batch requests required; skipping provider submit",
                data={"filtered_events": len(filtered)},
            )

        state.filtered_events = [event.model_dump(mode="json") for event in filtered]
        state.batch_id = batch_id
        state.neighborhood_jobs = [
            _serialize_neighborhood_job(job) for job in neighborhood_jobs
        ]
        state.stated_interests = stated_interests
        state.phase = "batch_submitted"
        write_pipeline_state(state)
        logger.info(
            "Phase 1 complete; pipeline state persisted",
            data={
                "filtered_events": result.after_pre_enrichment_filter,
                "batch_id": batch_id,
                "batch_requests": len(batch_requests),
            },
        )

        resumed = read_pipeline_state()
        if resumed is None:
            raise RuntimeError("Pipeline state missing at enrichment phase boundary")
        state = resumed
        state.phase = "phase_2"
        write_pipeline_state(state)

        filtered_events = [
            NormalizedEvent.model_validate(payload) for payload in state.filtered_events
        ]
        restored_jobs = [
            _deserialize_neighborhood_job(payload)
            for payload in state.neighborhood_jobs
        ]
        enriched = await _apply_enrichment_batch(
            filtered_events,
            batch_results,
            restored_jobs,
            state.stated_interests,
            cache=cache,
            run_id=run_id,
        )
        result.enriched_events = len(enriched)

        if _should_stop_after(uat_options.stop_after, "enrich"):
            result.last_completed_stage = "enrich"
            return result

        ranked = await ranking.run(enriched, profile, run_id)
        result.ranked_events = len(ranked)
        result.top_recommendations = _top_recommendation_rows(ranked)

        risk_scored = await sellout_risk.run(ranked, run_id)
        result.after_sellout_risk = len(risk_scored)

        curator_result = await recommendation_curator.run(
            risk_scored,
            profile,
            run_id,
        )
        curated = curator_result.recommendations
        result.curated_recommendations = len(curated)

        email_result = await email_composer.run(
            curated,
            profile,
            run_id,
            below_minimum=curator_result.below_minimum,
            curator_config=curator_result.curator_config,
        )
        if email_result.preview_path is not None:
            result.email_preview_path = str(email_result.preview_path)
        result.email_sent = email_result.sent

        result.enrichment_cache_hit_rates_pct = cache.enrichment_cache_hit_rates()
        result.evaluation_flags = 0
        cache.log_run_stats()

        if _should_stop_after(uat_options.stop_after, "email"):
            result.last_completed_stage = "email"
            return result

        state.phase = "complete"
        clear_pipeline_state()
        result.last_completed_stage = "complete"

        logger.info(
            "Pipeline complete",
            data={
                "raw_entries": result.raw_entries,
                "feeds_fetched": result.feeds_fetched,
                "feeds_unchanged": result.feeds_unchanged,
                "seen_entries_cache_hits": result.seen_entries_cache_hits,
                "seen_entries_cache_misses": result.seen_entries_cache_misses,
                "seen_entries_hit_rate_pct": result.seen_entries_hit_rate_pct,
                "after_pre_enrichment_filter": result.after_pre_enrichment_filter,
                "enrichment_cache_hit_rates_pct": result.enrichment_cache_hit_rates_pct,
                "pre_enrichment_discards": {
                    "low_information": result.pre_enrichment_discard_low_information,
                    "outside_coming_week": result.pre_enrichment_discard_outside_week,
                    "in_exclude_window": result.pre_enrichment_discard_exclude_window,
                },
                "curated_recommendations": result.curated_recommendations,
                "email_sent": result.email_sent,
                "email_preview_path": result.email_preview_path,
            },
        )

        return result
