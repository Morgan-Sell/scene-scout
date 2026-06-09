"""
Command-line interface for SceneScout.

Provides UAT mode for running the full pipeline skeleton locally with
color-coded agent logs and per-run output directories.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from scene_scout.config import PROJECT_ROOT, is_dry_run
from scene_scout.logging import configure_log_level, get_logger
from scene_scout.orchestrator import Orchestrator, PipelineResult

_OUTPUT_DIR = PROJECT_ROOT / "output"


def uat_output_dir(run_id: str) -> Path:
    """Return the UAT output directory for a pipeline run.

    Parameters
    ----------
    run_id : str
        Pipeline run identifier.

    Returns
    -------
    Path
        ``output/uat_{run_id}/`` under the project root.
    """
    return _OUTPUT_DIR / f"uat_{run_id}"


def write_summary_json(output_dir: Path, result: PipelineResult) -> Path:
    """Write UAT summary statistics for a pipeline run.

    Parameters
    ----------
    output_dir : Path
        UAT run output directory.
    result : PipelineResult
        Per-stage counts from the orchestrator.

    Returns
    -------
    Path
        Path to the written ``summary.json`` file.
    """
    summary = {
        "run_id": result.run_id,
        "raw_entries": result.raw_entries,
        "feeds_unchanged": result.feeds_unchanged,
        "seen_entries_cache_hits": result.seen_entries_cache_hits,
        "seen_entries_cache_misses": result.seen_entries_cache_misses,
        "seen_entries_hit_rate_pct": result.seen_entries_hit_rate_pct,
        "extraction_candidates": result.extraction_candidates,
        "normalized_events": result.normalized_events,
        "deduplicated_events": result.deduplicated_events,
        "after_description_quality": result.after_description_quality,
        "after_pre_enrichment_filter": result.after_pre_enrichment_filter,
        "enriched_events": result.enriched_events,
        "ranked_events": result.ranked_events,
        "after_sellout_risk": result.after_sellout_risk,
        "curated_recommendations": result.curated_recommendations,
        "evaluation_flags": result.evaluation_flags,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary_path


async def run_uat(
    prompt: str, *, dry_run: bool = False, verbose: bool = False
) -> PipelineResult:
    """Execute a UAT pipeline run.

    Parameters
    ----------
    prompt : str
        User cold-start prompt for the pipeline.
    dry_run : bool
        When ``True``, sets ``DRY_RUN=true`` so email is not sent.
    verbose : bool
        When ``True``, enables DEBUG-level agent logs.

    Returns
    -------
    PipelineResult
        Per-stage counts from the orchestrator.
    """
    if dry_run:
        os.environ["DRY_RUN"] = "true"

    if verbose:
        configure_log_level(logging.DEBUG)

    logger = get_logger("orchestrator")
    logger.info(
        "UAT run starting",
        data={"dry_run": is_dry_run(), "verbose": verbose},
    )

    result = await Orchestrator().run(prompt)

    output_dir = uat_output_dir(result.run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_summary_json(output_dir, result)

    logger = get_logger("orchestrator", run_id=result.run_id)
    logger.info(
        "UAT run complete",
        data={
            "dry_run": is_dry_run(),
            "output_dir": str(output_dir),
            "curated_recommendations": result.curated_recommendations,
        },
    )

    return result


def build_parser() -> argparse.ArgumentParser:
    """Build the SceneScout CLI argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser with subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="scene_scout.cli",
        description="SceneScout — personalized event discovery",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    uat_parser = subparsers.add_parser(
        "uat",
        help="Run the pipeline end-to-end in UAT mode",
    )
    uat_parser.add_argument(
        "--prompt",
        required=True,
        help="User cold-start prompt describing event preferences",
    )
    uat_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the pipeline without sending email",
    )
    uat_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level agent logs",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m scene_scout.cli``.

    Parameters
    ----------
    argv : list[str], optional
        Command-line arguments. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "uat":
        asyncio.run(
            run_uat(
                args.prompt,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
        )
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
