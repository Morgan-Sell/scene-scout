"""
SceneScout pipeline orchestrator.

Generates ``run_id``, sequences agent calls, persists ``PipelineState`` at the
batch boundary, and returns per-stage counts in ``PipelineResult``.

Phase 2.4 skeleton: all agents are stubs returning empty lists. Real agent
implementations replace stubs in later phases without changing the orchestration
shape.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scene_scout.agents import event_extraction
from scene_scout.config import vol_pipeline_state_dir
from scene_scout.logging import get_logger

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
    extraction_candidates: int = 0
    normalized_events: int = 0
    deduplicated_events: int = 0
    after_description_quality: int = 0
    after_pre_enrichment_filter: int = 0
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


# ---------------------------------------------------------------------------
# Agent stubs — replaced by real agents in later phases
# ---------------------------------------------------------------------------


async def _stub_user_preference(prompt: str, run_id: str) -> dict[str, Any]:
    """Stub for User Preference Agent."""
    return {}


async def _stub_feed_scout(run_id: str) -> tuple[list[Any], list[Any]]:
    """Stub for Feed Scout Agent."""
    return [], []



async def _stub_event_normalization(candidates: list[Any], run_id: str) -> list[Any]:
    """Stub for Event Normalization Agent."""
    return []


async def _stub_deduplication(events: list[Any], run_id: str) -> list[Any]:
    """Stub for Deduplication Agent."""
    return []


async def _stub_description_quality(events: list[Any], run_id: str) -> list[Any]:
    """Stub for Description Quality Agent."""
    return []


def _apply_pre_enrichment_filter(events: list[Any]) -> list[Any]:
    """Stub pre-enrichment filter applied by the orchestrator."""
    return []


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

        entries_for_extraction = entries
        result.seen_entries_cache_hits = 0

        candidates = await event_extraction.run(entries_for_extraction, run_id)
        result.extraction_candidates = len(candidates)

        normalized = await _stub_event_normalization(candidates, run_id)
        result.normalized_events = len(normalized)

        deduplicated = await _stub_deduplication(normalized, run_id)
        result.deduplicated_events = len(deduplicated)

        quality_scored = await _stub_description_quality(deduplicated, run_id)
        result.after_description_quality = len(quality_scored)

        filtered = _apply_pre_enrichment_filter(quality_scored)
        result.after_pre_enrichment_filter = len(filtered)

        state.filtered_events = []
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

        logger.info(
            "Pipeline complete",
            data={
                "raw_entries": result.raw_entries,
                "curated_recommendations": result.curated_recommendations,
            },
        )

        return result
