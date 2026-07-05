"""Orchestrator policy constants and UAT run options."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from scene_scout.models.feed import FeedConfig
from scene_scout.models.user import HORIZON_DAYS_MAX, HORIZON_DAYS_MIN

ENRICHMENT_BATCH_POLL_INTERVAL_SECONDS = 300

UatStopAfter = Literal["feeds", "extract", "normalize", "enrich", "email"]


@dataclass(frozen=True)
class UatRunOptions:
    """Optional limits for abbreviated UAT runs (Tier B)."""

    feed_ids: frozenset[str] | None = None
    max_extraction: int | None = None
    stop_after: UatStopAfter | None = None
    home_city: str | None = None
    horizon_days: int | None = None


def parse_feed_ids(raw: str | None) -> frozenset[str] | None:
    """Parse a comma-separated feed id list from the CLI."""
    if raw is None or not raw.strip():
        return None
    feed_ids = frozenset(part.strip() for part in raw.split(",") if part.strip())
    return feed_ids or None


def resolve_uat_home_city(cli_value: str | None) -> str | None:
    """Resolve UAT home city from ``--city`` or ``UAT_HOME_CITY``."""
    if cli_value is not None and cli_value.strip():
        return cli_value.strip()
    env_raw = os.getenv("UAT_HOME_CITY")
    if env_raw and env_raw.strip():
        return env_raw.strip()
    return None


def resolve_uat_horizon_days(cli_value: int | None) -> int | None:
    """Resolve UAT horizon from ``--horizon-days`` or ``UAT_HORIZON_DAYS``."""
    if cli_value is not None:
        if not HORIZON_DAYS_MIN <= cli_value <= HORIZON_DAYS_MAX:
            raise ValueError(
                f"--horizon-days must be between {HORIZON_DAYS_MIN} and "
                f"{HORIZON_DAYS_MAX}"
            )
        return cli_value
    env_raw = os.getenv("UAT_HORIZON_DAYS")
    if not env_raw:
        return None
    value = int(env_raw)
    if not HORIZON_DAYS_MIN <= value <= HORIZON_DAYS_MAX:
        raise ValueError(
            f"UAT_HORIZON_DAYS must be between {HORIZON_DAYS_MIN} and "
            f"{HORIZON_DAYS_MAX}"
        )
    return value


def resolve_uat_max_extraction(cli_value: int | None) -> int | None:
    """Resolve extraction cap from ``--max-extraction`` or ``UAT_MAX_EXTRACTION``."""
    if cli_value is not None:
        if cli_value < 1:
            raise ValueError("--max-extraction must be at least 1")
        return cli_value
    env_raw = os.getenv("UAT_MAX_EXTRACTION")
    if not env_raw:
        return None
    value = int(env_raw)
    if value < 1:
        raise ValueError("UAT_MAX_EXTRACTION must be at least 1")
    return value


def select_feed_configs(
    active_feeds: list[FeedConfig],
    feed_ids: frozenset[str] | None,
) -> list[FeedConfig]:
    """Return active feeds filtered to ``feed_ids`` when provided."""
    if feed_ids is None:
        return active_feeds
    by_id = {config.id: config for config in active_feeds}
    missing = feed_ids - by_id.keys()
    if missing:
        raise ValueError(
            "Unknown or inactive feed id(s): " + ", ".join(sorted(missing))
        )
    return [by_id[feed_id] for feed_id in sorted(feed_ids)]
