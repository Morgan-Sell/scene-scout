"""UAT run output artifacts (summary, error, checkpoint JSON)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from scene_scout.config import is_dry_run
from scene_scout.orchestrator import PipelineResult

UATStatus = Literal["completed", "failed"]


def build_summary_payload(
    result: PipelineResult,
    *,
    status: UATStatus = "completed",
) -> dict[str, Any]:
    """Build the summary JSON payload for a UAT run."""
    return {
        "run_id": result.run_id,
        "status": status,
        "last_completed_stage": result.last_completed_stage,
        "dry_run": is_dry_run(),
        "raw_entries": result.raw_entries,
        "feeds_fetched": result.feeds_fetched,
        "feed_health": result.feed_health,
        "feeds_unchanged": result.feeds_unchanged,
        "seen_entries_cache_hits": result.seen_entries_cache_hits,
        "seen_entries_cache_misses": result.seen_entries_cache_misses,
        "seen_entries_hit_rate_pct": result.seen_entries_hit_rate_pct,
        "extraction_candidates": result.extraction_candidates,
        "normalized_events": result.normalized_events,
        "deduplicated_events": result.deduplicated_events,
        "after_description_quality": result.after_description_quality,
        "after_pre_enrichment_filter": result.after_pre_enrichment_filter,
        "pre_enrichment_discards": {
            "low_information": result.pre_enrichment_discard_low_information,
            "outside_coming_week": result.pre_enrichment_discard_outside_week,
            "in_exclude_window": result.pre_enrichment_discard_exclude_window,
        },
        "enriched_events": result.enriched_events,
        "enrichment_cache_hit_rates_pct": result.enrichment_cache_hit_rates_pct,
        "ranked_events": result.ranked_events,
        "after_sellout_risk": result.after_sellout_risk,
        "curated_recommendations": result.curated_recommendations,
        "top_recommendations": result.top_recommendations,
        "email_preview_path": result.email_preview_path,
        "email_sent": result.email_sent,
        "evaluation_flags": result.evaluation_flags,
    }


def checkpoint_counts(result: PipelineResult) -> dict[str, Any]:
    """Return stage funnel counts for ``checkpoint.json``."""
    return {
        "raw_entries": result.raw_entries,
        "feeds_fetched": result.feeds_fetched,
        "feeds_unchanged": result.feeds_unchanged,
        "seen_entries_cache_hits": result.seen_entries_cache_hits,
        "seen_entries_cache_misses": result.seen_entries_cache_misses,
        "seen_entries_hit_rate_pct": result.seen_entries_hit_rate_pct,
        "extraction_candidates": result.extraction_candidates,
        "normalized_events": result.normalized_events,
        "deduplicated_events": result.deduplicated_events,
        "after_description_quality": result.after_description_quality,
        "after_pre_enrichment_filter": result.after_pre_enrichment_filter,
    }


def write_summary_json(
    output_dir: Path,
    result: PipelineResult,
    *,
    status: UATStatus = "completed",
) -> Path:
    """Write UAT summary statistics for a pipeline run."""
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(build_summary_payload(result, status=status), indent=2) + "\n",
        encoding="utf-8",
    )
    return summary_path


def write_error_json(
    output_dir: Path,
    result: PipelineResult,
    exc: BaseException,
) -> Path:
    """Write failure details for a partial UAT run."""
    output_dir.mkdir(parents=True, exist_ok=True)
    error_path = output_dir / "error.json"
    payload = {
        "run_id": result.run_id,
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "last_completed_stage": result.last_completed_stage,
    }
    error_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return error_path


def write_uat_checkpoint(
    output_dir: Path,
    result: PipelineResult,
    stage: str,
) -> Path:
    """Write or overwrite ``checkpoint.json`` after a pipeline stage completes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    payload = {
        "run_id": result.run_id,
        "last_completed_stage": stage,
        "counts": checkpoint_counts(result),
    }
    checkpoint_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return checkpoint_path
