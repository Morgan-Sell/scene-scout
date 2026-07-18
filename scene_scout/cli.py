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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from scene_scout.agents import feed_scout
from scene_scout.config import PROJECT_ROOT, is_dry_run, load_feed_configs
from scene_scout.email_composer_config import EMAIL_PREVIEW_FILENAME
from scene_scout.logging import configure_log_level, get_logger
from scene_scout.models.feed import FeedHealthReport, FeedStatus
from scene_scout.orchestrator import Orchestrator, PipelineResult, PipelineRunError
from scene_scout.orchestrator_config import (
    UatRunOptions,
    parse_feed_ids,
    resolve_uat_home_city,
    resolve_uat_horizon_days,
    resolve_uat_max_extraction,
)
from scene_scout.services.cache import CacheService
from scene_scout.uat_artifacts import UATStatus, write_error_json, write_summary_json

_OUTPUT_DIR = PROJECT_ROOT / "output"
_CONSOLE = Console()
_FEED_PROBE_HEALTHY_STATUSES = frozenset({FeedStatus.OK, FeedStatus.UNCHANGED})


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


def feed_probe_run_id(now: datetime | None = None) -> str:
    """Return a UTC timestamp id for a feed-probe run."""
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return reference.strftime("%Y%m%d-%H%M%S")


def feed_probe_output_path(run_id: str) -> Path:
    """Return ``output/feed_probe_{run_id}.json`` under the project root."""
    return _OUTPUT_DIR / f"feed_probe_{run_id}.json"


def feed_probe_is_healthy(report: FeedHealthReport) -> bool:
    """Return True when ingest succeeded or the feed was unchanged (304)."""
    return report.status in _FEED_PROBE_HEALTHY_STATUSES


def serialize_feed_health_report(report: FeedHealthReport) -> dict[str, Any]:
    """Serialize a feed health report for JSON output."""
    return {
        "feed_id": report.feed_id,
        "feed_name": report.feed_name,
        "feed_url": report.feed_url,
        "status": report.status.value,
        "entries_fetched": report.entries_fetched,
        "error_message": report.error_message,
    }


def build_feed_probe_payload(
    run_id: str,
    entries: list[Any],
    reports: list[FeedHealthReport],
) -> dict[str, Any]:
    """Build the feed-probe JSON payload."""
    feeds_unchanged = sum(
        1 for report in reports if report.status == FeedStatus.UNCHANGED
    )
    all_ok = bool(reports) and all(feed_probe_is_healthy(report) for report in reports)
    return {
        "run_id": run_id,
        "feeds_fetched": len(reports),
        "feeds_unchanged": feeds_unchanged,
        "raw_entries": len(entries),
        "all_ok": all_ok,
        "feed_health": [serialize_feed_health_report(report) for report in reports],
    }


def print_feed_probe_summary(
    reports: list[FeedHealthReport],
    *,
    raw_entries: int,
    output_path: Path,
) -> None:
    """Print the feed-probe table to the terminal."""
    feed_table = Table(
        title="Feed probe",
        show_header=True,
        header_style="bold",
    )
    feed_table.add_column("Feed", style="cyan")
    feed_table.add_column("Status")
    feed_table.add_column("Entries", justify="right")
    feed_table.add_column("Error")

    for report in reports:
        feed_table.add_row(
            report.feed_name,
            report.status.value,
            str(report.entries_fetched),
            report.error_message or "—",
        )

    unchanged_count = sum(
        1 for report in reports if report.status == FeedStatus.UNCHANGED
    )
    _CONSOLE.print(feed_table)
    _CONSOLE.print(
        f"\nFeeds: {len(reports)}  "
        f"Raw entries: {raw_entries}  "
        f"UNCHANGED (304): {unchanged_count}"
    )
    _CONSOLE.print(f"Report: [green]{output_path}[/green]")


def write_feed_probe_json(output_path: Path, payload: dict[str, Any]) -> Path:
    """Write feed-probe results to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output_path


@dataclass(frozen=True)
class FeedProbeResult:
    """Outcome of a feed-probe run."""

    run_id: str
    output_path: Path
    payload: dict[str, Any]
    exit_code: int


async def run_feed_probe(
    *,
    allow_failures: bool = False,
    verbose: bool = False,
    now: datetime | None = None,
    home_city: str | None = None,
) -> FeedProbeResult:
    """Fetch active feeds and report ingest health without LLM calls."""
    if verbose:
        configure_log_level(logging.DEBUG)

    run_id = feed_probe_run_id(now)
    logger = get_logger("feed_scout", run_id=run_id)
    logger.info(
        "Feed probe starting",
        data={"home_city": home_city, "active_feeds": "pending"},
    )

    feed_configs = load_feed_configs(home_city=home_city)
    cache = CacheService(run_id=run_id)
    entries, reports = await feed_scout.run(
        feed_configs,
        run_id,
        get_feed_etag=cache.get_feed_etag,
        store_feed_etag=cache.set_feed_etag,
        home_city=home_city,
    )

    payload = build_feed_probe_payload(run_id, entries, reports)
    output_path = write_feed_probe_json(feed_probe_output_path(run_id), payload)
    print_feed_probe_summary(reports, raw_entries=len(entries), output_path=output_path)

    all_ok = payload["all_ok"]
    exit_code = 0 if all_ok or allow_failures else 1
    logger.info(
        "Feed probe complete",
        data={
            "output_path": str(output_path),
            "feeds_fetched": payload["feeds_fetched"],
            "raw_entries": payload["raw_entries"],
            "all_ok": all_ok,
            "exit_code": exit_code,
        },
    )
    return FeedProbeResult(
        run_id=run_id,
        output_path=output_path,
        payload=payload,
        exit_code=exit_code,
    )


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
    pipeline_table.add_row(
        "Structured ingest bypass",
        str(result.structured_ingest_bypass_count),
    )
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


def build_uat_run_options(
    *,
    max_extraction: int | None = None,
    feeds: str | None = None,
    stop_after: str | None = None,
    home_city: str | None = None,
    horizon_days: int | None = None,
) -> UatRunOptions:
    """Build orchestrator UAT limits from CLI flags and env."""
    return UatRunOptions(
        feed_ids=parse_feed_ids(feeds),
        max_extraction=resolve_uat_max_extraction(max_extraction),
        stop_after=stop_after,  # type: ignore[arg-type]
        home_city=resolve_uat_home_city(home_city),
        horizon_days=resolve_uat_horizon_days(horizon_days),
    )


def uat_summary_status(result: PipelineResult) -> UATStatus:
    """Map pipeline outcome to summary JSON status."""
    if result.last_completed_stage == "complete":
        return "completed"
    return "partial"


async def run_uat(
    prompt: str,
    *,
    dry_run: bool = False,
    verbose: bool = False,
    max_extraction: int | None = None,
    feeds: str | None = None,
    stop_after: str | None = None,
    home_city: str | None = None,
    horizon_days: int | None = None,
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
    max_extraction : int | None
        Cap cache-miss entries sent to extraction (``UAT_MAX_EXTRACTION`` env fallback).
    feeds : str | None
        Comma-separated active feed ids to include.
    stop_after : str | None
        Stop after ``feeds``, ``extract``, ``normalize``, ``enrich``, or ``email``.

    Returns
    -------
    PipelineResult
        Per-stage counts from the orchestrator.
    """
    if dry_run:
        os.environ["DRY_RUN"] = "true"

    if verbose:
        configure_log_level(logging.DEBUG)

    uat_options = build_uat_run_options(
        max_extraction=max_extraction,
        feeds=feeds,
        stop_after=stop_after,
        home_city=home_city,
        horizon_days=horizon_days,
    )

    logger = get_logger("orchestrator")
    logger.info(
        "UAT run starting",
        data={
            "dry_run": is_dry_run(),
            "verbose": verbose,
            "feeds": sorted(uat_options.feed_ids) if uat_options.feed_ids else None,
            "max_extraction": uat_options.max_extraction,
            "stop_after": uat_options.stop_after,
            "home_city": uat_options.home_city,
            "horizon_days": uat_options.horizon_days,
        },
    )

    try:
        result = await Orchestrator().run(
            prompt,
            uat_output_base=_OUTPUT_DIR,
            uat_options=uat_options,
        )
    except PipelineRunError as exc:
        result = exc.result
        output_dir = uat_output_dir(result.run_id)
        write_error_json(output_dir, result, exc.cause)
        write_summary_json(output_dir, result, status="failed")
        logger = get_logger("orchestrator", run_id=result.run_id)
        logger.error(
            "UAT run failed",
            data={
                "dry_run": is_dry_run(),
                "output_dir": str(output_dir),
                "last_completed_stage": result.last_completed_stage,
                "exception_type": type(exc.cause).__name__,
                "message": str(exc.cause),
            },
        )
        raise

    output_dir = uat_output_dir(result.run_id)
    write_summary_json(output_dir, result, status=uat_summary_status(result))
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
    uat_parser.add_argument(
        "--max-extraction",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Cap cache-miss entries sent to extraction "
            "(UAT_MAX_EXTRACTION env fallback)"
        ),
    )
    uat_parser.add_argument(
        "--feeds",
        default=None,
        help="Comma-separated active feed ids (default: all active feeds)",
    )
    uat_parser.add_argument(
        "--stop-after",
        choices=["feeds", "extract", "normalize", "enrich", "email"],
        default=None,
        help="Stop after the given pipeline stage and write a partial summary",
    )
    uat_parser.add_argument(
        "--city",
        default=None,
        help="Home city when no persisted profile exists (UAT_HOME_CITY env fallback)",
    )
    uat_parser.add_argument(
        "--horizon-days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Days ahead to search when no persisted profile exists "
            "(1–60; UAT_HORIZON_DAYS env fallback)"
        ),
    )

    feed_probe_parser = subparsers.add_parser(
        "feed-probe",
        help="Ingest-only feed health check (no LLM)",
    )
    feed_probe_parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Exit 0 even when one or more active feeds are not ok/unchanged",
    )
    feed_probe_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level agent logs",
    )
    feed_probe_parser.add_argument(
        "--city",
        default=None,
        help=(
            "Home city for metro feed filter (includes is_national feeds); "
            "omit to probe all active feeds"
        ),
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
        try:
            asyncio.run(
                run_uat(
                    args.prompt,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                    max_extraction=args.max_extraction,
                    feeds=args.feeds,
                    stop_after=args.stop_after,
                    home_city=args.city,
                    horizon_days=args.horizon_days,
                )
            )
        except PipelineRunError:
            return 1
        except ValueError as exc:
            _CONSOLE.print(f"[red]UAT configuration error:[/red] {exc}")
            return 1
        return 0

    if args.command == "feed-probe":
        result = asyncio.run(
            run_feed_probe(
                allow_failures=args.allow_failures,
                verbose=args.verbose,
                home_city=args.city,
            )
        )
        return result.exit_code

    parser.error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
