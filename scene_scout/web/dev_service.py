"""Backend helpers for the web Dev Section."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from scene_scout.agents.user_preference import UserProfileNotFoundError, load_profile
from scene_scout.cache_config import (
    CACHE_TYPES,
    PERFORMER_TTL_DAYS,
    SEEN_ENTRIES_TTL_DAYS,
    VENUE_CONTEXT_TTL_DAYS,
    VENUE_GEO_TTL_DAYS,
    VIBE_TTL_DAYS,
)
from scene_scout.cli import run_uat, uat_output_dir
from scene_scout.config import PROJECT_ROOT, load_feed_configs
from scene_scout.logging import list_run_logs, read_run_log_entries
from scene_scout.models.history import RecommendationHistoryEntry
from scene_scout.orchestrator import PipelineRunError
from scene_scout.services import history as history_service
from scene_scout.services.cache import _cache_db_path, _format_dt
from scene_scout.uat_artifacts import build_summary_payload, write_summary_json

DryRunStatus = Literal["idle", "running", "completed", "failed"]
_OUTPUT_DIR = PROJECT_ROOT / "output"
_DEFAULT_DEV_PROMPT = (
    "Find me intimate live music, art openings, and neighborhood events "
    "that match my taste."
)


@dataclass
class DryRunJob:
    """In-memory dry-run job tracked by the web Dev Section."""

    status: DryRunStatus = "idle"
    run_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    email_preview_path: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


_dry_run_job = DryRunJob()
_dry_run_lock = asyncio.Lock()


def list_recent_runs(limit: int = 5) -> list[dict[str, Any]]:
    """Return metadata for the last ``limit`` pipeline runs."""
    return list_run_logs(limit=limit)


def get_run_logs(
    *,
    run_id: str | None = None,
    agent: str | None = None,
    level: str | None = None,
    run_limit: int = 5,
) -> dict[str, Any]:
    """Return filtered JSONL entries for one or more recent runs."""
    runs = list_run_logs(limit=run_limit)
    if not runs:
        return {"runs": [], "entries": []}

    selected_run_id = run_id or runs[0]["run_id"]
    entries = read_run_log_entries(selected_run_id, agent=agent, level=level)
    return {
        "runs": runs,
        "selected_run_id": selected_run_id,
        "entries": entries,
    }


def _latest_summary_path() -> Path | None:
    summaries = sorted(
        _OUTPUT_DIR.glob("uat_*/summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return summaries[0] if summaries else None


def _load_latest_summary() -> dict[str, Any] | None:
    path = _latest_summary_path()
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _post_date_filter_yield_pct(summary: dict[str, Any] | None) -> float | None:
    if summary is None:
        return None
    before = summary.get("after_description_quality", 0)
    after = summary.get("after_pre_enrichment_filter", 0)
    if not before:
        return None
    return round(after / before * 100.0, 1)


def _feed_etag_last_fetch() -> dict[str, str]:
    db_path = _cache_db_path()
    if not db_path.is_file():
        return {}

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT feed_id, stored_at FROM feed_etags ORDER BY stored_at DESC"
        ).fetchall()
    return {feed_id: stored_at for feed_id, stored_at in rows}


def _feed_scout_details_from_logs(run_id: str) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for entry in read_run_log_entries(run_id, agent="feed_scout"):
        data = entry.get("data") or {}
        feed_id = data.get("feed_id")
        if not feed_id:
            continue
        if entry["message"] == "Feed OK":
            details[feed_id] = {
                "etag_supported": data.get("etag_supported"),
                "entries_fetched": data.get("entries_fetched"),
                "last_fetch_at": entry.get("timestamp"),
                "status": "ok",
            }
        elif entry["message"] == "Feed unchanged":
            details[feed_id] = {
                "etag_supported": details.get(feed_id, {}).get("etag_supported"),
                "entries_fetched": 0,
                "last_fetch_at": entry.get("timestamp"),
                "status": "unchanged",
            }
        elif entry["message"] == "Feed failed":
            details[feed_id] = {
                "etag_supported": details.get(feed_id, {}).get("etag_supported"),
                "entries_fetched": 0,
                "last_fetch_at": entry.get("timestamp"),
                "status": "failed",
                "error_message": data.get("error_message"),
            }
    return details


def build_feed_health_dashboard() -> dict[str, Any]:
    """Assemble feed health metrics for the Dev Section dashboard."""
    summary = _load_latest_summary()
    etag_last_fetch = _feed_etag_last_fetch()
    log_details: dict[str, dict[str, Any]] = {}
    if summary is not None:
        log_details = _feed_scout_details_from_logs(summary["run_id"])

    feeds: list[dict[str, Any]] = []
    for config in load_feed_configs():
        summary_row = next(
            (
                row
                for row in (summary or {}).get("feed_health", [])
                if row.get("feed_id") == config.id
            ),
            None,
        )
        log_row = log_details.get(config.id, {})
        last_fetch = log_row.get("last_fetch_at") or etag_last_fetch.get(config.id)
        feeds.append(
            {
                "feed_id": config.id,
                "feed_name": config.name,
                "feed_url": config.url,
                "active": config.active,
                "status": log_row.get("status")
                or (summary_row or {}).get("status"),
                "entries_fetched": log_row.get("entries_fetched")
                if log_row
                else (summary_row or {}).get("entries_fetched"),
                "etag_supported": log_row.get("etag_supported"),
                "last_fetch_at": last_fetch,
                "error_message": log_row.get("error_message")
                or (summary_row or {}).get("error_message"),
            }
        )

    return {
        "latest_run_id": (summary or {}).get("run_id"),
        "seen_entries_hit_rate_pct": (summary or {}).get("seen_entries_hit_rate_pct"),
        "post_date_filter_yield_pct": _post_date_filter_yield_pct(summary),
        "feeds": feeds,
    }


def _table_stats(
    conn: sqlite3.Connection,
    table: str,
    *,
    expiry_column: str | None = None,
) -> dict[str, Any]:
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    expired = 0
    if expiry_column is not None:
        now = _format_dt(datetime.now(timezone.utc))
        expired = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {expiry_column} < ?",
            (now,),
        ).fetchone()[0]
    active = total - expired
    return {"rows": total, "expired": expired, "active": active}


def build_cache_inspection() -> dict[str, Any]:
    """Return cache table counts, TTL policy, and latest run hit rates."""
    summary = _load_latest_summary()
    ttl_days = {
        "feed_etags": None,
        "seen_entries": SEEN_ENTRIES_TTL_DAYS,
        "performer_cache": PERFORMER_TTL_DAYS,
        "venue_cache_geo": VENUE_GEO_TTL_DAYS,
        "venue_cache_context": VENUE_CONTEXT_TTL_DAYS,
        "vibe_cache": VIBE_TTL_DAYS,
    }

    tables: list[dict[str, Any]] = []
    db_path = _cache_db_path()
    if db_path.is_file():
        with sqlite3.connect(db_path) as conn:
            tables.append(
                {
                    "cache_type": "feed_etags",
                    "ttl_days": ttl_days["feed_etags"],
                    **_table_stats(conn, "feed_etags"),
                }
            )
            tables.append(
                {
                    "cache_type": "seen_entries",
                    "ttl_days": ttl_days["seen_entries"],
                    **_table_stats(conn, "seen_entries", expiry_column="expires_at"),
                }
            )
            tables.append(
                {
                    "cache_type": "performer_cache",
                    "ttl_days": ttl_days["performer_cache"],
                    **_table_stats(
                        conn, "performer_cache", expiry_column="expires_at"
                    ),
                }
            )
            venue_total = conn.execute("SELECT COUNT(*) FROM venue_cache").fetchone()[0]
            geo_expired = conn.execute(
                "SELECT COUNT(*) FROM venue_cache WHERE geo_expires_at < ?",
                (_format_dt(datetime.now(timezone.utc)),),
            ).fetchone()[0]
            context_expired = conn.execute(
                "SELECT COUNT(*) FROM venue_cache WHERE context_expires_at < ?",
                (_format_dt(datetime.now(timezone.utc)),),
            ).fetchone()[0]
            tables.append(
                {
                    "cache_type": "venue_cache",
                    "ttl_days_geo": ttl_days["venue_cache_geo"],
                    "ttl_days_context": ttl_days["venue_cache_context"],
                    "rows": venue_total,
                    "geo_expired": geo_expired,
                    "context_expired": context_expired,
                    "geo_active": venue_total - geo_expired,
                    "context_active": venue_total - context_expired,
                }
            )
            tables.append(
                {
                    "cache_type": "vibe_cache",
                    "ttl_days": ttl_days["vibe_cache"],
                    **_table_stats(conn, "vibe_cache", expiry_column="expires_at"),
                }
            )
    else:
        for cache_type in CACHE_TYPES:
            tables.append(
                {
                    "cache_type": cache_type,
                    "rows": 0,
                    "expired": 0,
                    "active": 0,
                    "ttl_days": ttl_days.get(cache_type),
                }
            )

    return {
        "latest_run_id": (summary or {}).get("run_id"),
        "seen_entries_hit_rate_pct": (summary or {}).get("seen_entries_hit_rate_pct"),
        "enrichment_cache_hit_rates_pct": (summary or {}).get(
            "enrichment_cache_hit_rates_pct", {}
        ),
        "tables": tables,
    }


def serialize_history_entry(entry: RecommendationHistoryEntry) -> dict[str, Any]:
    """Convert a history row to JSON-serializable data."""
    return {
        "id": entry.id,
        "run_id": entry.run_id,
        "rank": entry.rank,
        "score": entry.score,
        "event_id": entry.event_id,
        "event_title": entry.event_title,
        "categories": entry.categories,
        "explanation": entry.explanation,
        "recommended_at": entry.recommended_at.isoformat(),
        "feedback_signal": entry.feedback_signal,
        "is_wildcard": entry.is_wildcard,
    }


def get_recommendation_history(days: int = 30) -> dict[str, Any]:
    """Return recent recommendation history for the Dev Section browser."""
    entries = history_service.get_recent(days)
    return {
        "days": days,
        "count": len(entries),
        "entries": [serialize_history_entry(entry) for entry in entries],
    }


def get_dry_run_status() -> dict[str, Any]:
    """Return the current dry-run job state."""
    return {
        "status": _dry_run_job.status,
        "run_id": _dry_run_job.run_id,
        "started_at": _dry_run_job.started_at.isoformat()
        if _dry_run_job.started_at
        else None,
        "completed_at": _dry_run_job.completed_at.isoformat()
        if _dry_run_job.completed_at
        else None,
        "error": _dry_run_job.error,
        "email_preview_path": _dry_run_job.email_preview_path,
        "summary": _dry_run_job.summary,
    }


def resolve_dry_run_prompt(prompt: str | None) -> str:
    """Resolve the prompt used for a Dev Section dry-run."""
    if prompt and prompt.strip():
        return prompt.strip()
    try:
        profile = load_profile()
    except UserProfileNotFoundError:
        return _DEFAULT_DEV_PROMPT
    interests = ", ".join(profile.stated_interests) or "events I would enjoy"
    return (
        f"Find me {interests} in {profile.home_city} "
        f"within the next {profile.horizon_days} days."
    )


def email_preview_path_for_run(run_id: str) -> Path | None:
    """Return the email preview HTML path for a UAT run, if it exists."""
    preview = uat_output_dir(run_id) / "email_preview.html"
    return preview if preview.is_file() else None


async def start_dry_run(prompt: str | None = None) -> dict[str, Any]:
    """Launch a background dry-run pipeline job."""
    global _dry_run_job

    async with _dry_run_lock:
        if _dry_run_job.status == "running":
            return get_dry_run_status()

        resolved_prompt = resolve_dry_run_prompt(prompt)
        _dry_run_job = DryRunJob(
            status="running",
            started_at=datetime.now(timezone.utc),
        )

    async def _execute() -> None:
        global _dry_run_job
        previous_dry_run = os.environ.get("DRY_RUN")
        os.environ["DRY_RUN"] = "true"
        try:
            result = await run_uat(resolved_prompt, dry_run=True)
            _dry_run_job.run_id = result.run_id
            _dry_run_job.email_preview_path = result.email_preview_path
            _dry_run_job.summary = build_summary_payload(result)
            _dry_run_job.status = "completed"
        except PipelineRunError as exc:
            result = exc.result
            write_summary_json(
                uat_output_dir(result.run_id),
                result,
                status="failed",
            )
            _dry_run_job.run_id = result.run_id
            _dry_run_job.error = str(exc.cause)
            _dry_run_job.summary = build_summary_payload(result, status="failed")
            _dry_run_job.status = "failed"
        except Exception as exc:
            _dry_run_job.error = str(exc)
            _dry_run_job.status = "failed"
        finally:
            if previous_dry_run is None:
                os.environ.pop("DRY_RUN", None)
            else:
                os.environ["DRY_RUN"] = previous_dry_run
            _dry_run_job.completed_at = datetime.now(timezone.utc)

    asyncio.create_task(_execute())
    return get_dry_run_status()


def reset_dry_run_job() -> None:
    """Reset dry-run job state (for tests)."""
    global _dry_run_job
    _dry_run_job = DryRunJob()
