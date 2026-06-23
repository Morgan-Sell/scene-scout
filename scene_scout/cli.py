"""
Command-line interface for SceneScout.

Provides UAT mode for running the full pipeline locally with
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

from rich.console import Console
from rich.table import Table

from scene_scout.config import PROJECT_ROOT, is_dry_run
from scene_scout.email_composer_config import EMAIL_PREVIEW_FILENAME
from scene_scout.logging import configure_log_level, get_logger
from scene_scout.orchestrator import Orchestrator, PipelineResult

_OUTPUT_DIR = PROJECT_ROOT / "output"
_CONSOLE = Console()


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
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary_path


def print_uat_summary(result: PipelineResult) -> None:
    """Print the UAT pipeline summary table to the terminal."""
    pipeline_table = Table(
        title=f"SceneScout UAT — {result.run_id}",
        show_header=True,
        header_style="bold",
    )
    pipeline_table.add_column("Stage", style="cyan")
    pipeline_table.add_column("Count", justify="right")

    pipeline_table.add_row("Feeds fetched", str(result.feeds_fetched))
    pipeline_table.add_row("Feeds UNCHANGED (304)", str(result.feeds_unchanged))
    pipeline_table.add_row("Raw entries", str(result.raw_entries))

    if result.feed_health:
        feed_table = Table(
            title="Feed health",
            show_header=True,
            header_style="bold",
        )
        feed_table.add_column("Feed", style="cyan")
        feed_table.add_column("Status")
        feed_table.add_column("Entries", justify="right")
        feed_table.add_column("Error")
        for report in result.feed_health:
            feed_table.add_row(
                report["feed_name"],
                report["status"],
                str(report["entries_fetched"]),
                report.get("error_message") or "—",
            )
        _CONSOLE.print(feed_table)
    pipeline_table.add_row(
        "seen_entries cache hits",
        str(result.seen_entries_cache_hits),
    )
    pipeline_table.add_row(
        "seen_entries cache misses",
        str(result.seen_entries_cache_misses),
    )
    pipeline_table.add_row(
        "seen_entries hit rate",
        f"{result.seen_entries_hit_rate_pct:.1f}%",
    )
    pipeline_table.add_row("Extraction candidates", str(result.extraction_candidates))
    pipeline_table.add_row("Normalized events", str(result.normalized_events))
    pipeline_table.add_row("After deduplication", str(result.deduplicated_events))
    pipeline_table.add_row(
        "After description quality",
        str(result.after_description_quality),
    )
    pipeline_table.add_row(
        "After pre-enrichment filter",
        str(result.after_pre_enrichment_filter),
    )
    pipeline_table.add_row(
        "Discarded — low information",
        str(result.pre_enrichment_discard_low_information),
    )
    pipeline_table.add_row(
        "Discarded — outside coming week",
        str(result.pre_enrichment_discard_outside_week),
    )
    pipeline_table.add_row(
        "Discarded — exclude window",
        str(result.pre_enrichment_discard_exclude_window),
    )
    pipeline_table.add_row("Enriched events", str(result.enriched_events))
    pipeline_table.add_row("Ranked events", str(result.ranked_events))
    pipeline_table.add_row(
        "Curated recommendations",
        str(result.curated_recommendations),
    )

    for cache_name, hit_rate in result.enrichment_cache_hit_rates_pct.items():
        pipeline_table.add_row(
            f"Enrichment cache hit rate — {cache_name}",
            f"{hit_rate:.1f}%",
        )

    pipeline_table.add_row(
        "Email sent",
        "yes" if result.email_sent else ("no (dry-run)" if is_dry_run() else "no"),
    )

    _CONSOLE.print(pipeline_table)

    if result.top_recommendations:
        top_table = Table(
            title="Top recommendations",
            show_header=True,
            header_style="bold",
        )
        top_table.add_column("#", justify="right")
        top_table.add_column("Title")
        top_table.add_column("Score", justify="right")
        top_table.add_column("Sources", justify="right")
        top_table.add_column("Coverage", justify="right")

        for index, row in enumerate(result.top_recommendations, start=1):
            top_table.add_row(
                str(index),
                row["title"],
                f"{row['score']:.3f}",
                str(row["source_count"]),
                f"{row['source_coverage']:.3f}",
            )

        _CONSOLE.print(top_table)

    if result.email_preview_path:
        _CONSOLE.print(f"\nEmail preview: [green]{result.email_preview_path}[/green]")
    else:
        preview_path = uat_output_dir(result.run_id) / EMAIL_PREVIEW_FILENAME
        if preview_path.is_file():
            _CONSOLE.print(f"\nEmail preview: [green]{preview_path}[/green]")


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
    print_uat_summary(result)

    logger = get_logger("orchestrator", run_id=result.run_id)
    logger.info(
        "UAT run complete",
        data={
            "dry_run": is_dry_run(),
            "output_dir": str(output_dir),
            "curated_recommendations": result.curated_recommendations,
            "email_preview_path": result.email_preview_path,
            "email_sent": result.email_sent,
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
