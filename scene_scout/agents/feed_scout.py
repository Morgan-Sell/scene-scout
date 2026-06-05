"""
Feed Scout Agent

Responsibility: Fetch, parse, and validate configured RSS feeds concurrently.
Produce raw feed entries and a health report for each feed.

This agent is deliberately deterministic. No LLM is involved.
Its job is reliability: read what is there, report what failed,
pass everything downstream faithfully without interpretation.

All feeds are fetched concurrently via asyncio.gather(). One feed
failure never blocks or affects the others.

Inputs:  list[FeedConfig], run_id: str
Outputs: tuple[list[RawFeedEntry], list[FeedHealthReport]]
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx

from scene_scout.models.feed import (
    FeedConfig,
    FeedHealthReport,
    FeedStatus,
    RawFeedEntry,
)

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 10
_MIN_EXPECTED_ENTRIES = 1


async def run(
    feed_configs: list[FeedConfig],
    run_id: str,
) -> tuple[list[RawFeedEntry], list[FeedHealthReport]]:
    """
    Fetch all configured feeds concurrently. Return raw entries and health reports.

    Uses asyncio.gather() so all feeds are in-flight simultaneously. Total wall-clock
    time is bounded by the slowest feed, not the sum of all feeds.

    A failure on one feed does not affect others. All failures are logged and
    reported, not raised.

    Args:
        feed_configs: Active feed configurations to process.
        run_id: Pipeline run identifier, attached to every RawFeedEntry produced.

    Returns:
        A tuple of:
          - All raw entries successfully fetched across all feeds
          - A health report for every feed (success or failure)
    """
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "SceneScout/0.1 (event discovery agent)"},
    ) as client:
        tasks = [_fetch_feed(config, client, run_id) for config in feed_configs]
        results = await asyncio.gather(*tasks)

    all_entries: list[RawFeedEntry] = []
    health_reports: list[FeedHealthReport] = []

    for entries, report in results:
        all_entries.extend(entries)
        health_reports.append(report)

        if report.succeeded:
            logger.info(
                "[%s] Feed OK: %s — %d entries fetched",
                run_id,
                report.feed_name,
                report.entries_fetched,
            )
        else:
            logger.warning(
                "[%s] Feed failed: %s — status=%s error=%s",
                run_id,
                report.feed_name,
                report.status,
                report.error_message,
            )

    _log_summary(run_id, health_reports)
    return all_entries, health_reports


async def validate_feed(url: str) -> FeedHealthReport:
    """
    Lightweight feed validation for user-submitted URLs.

    Fetches, parses, and checks for at least one entry. Returns a health report
    that the Gradio UI can display before saving the feed to config.

    Args:
        url: The RSS URL to validate.

    Returns:
        A FeedHealthReport indicating whether the feed is usable.
    """
    config = FeedConfig(
        id="__validation__",
        name=url,
        url=url,
        city="unknown",
        source_quality_score=0.5,
        active=True,
    )
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "SceneScout/0.1 (event discovery agent)"},
    ) as client:
        _, report = await _fetch_feed(config, client, run_id="validation")
    return report


async def _fetch_feed(
    config: FeedConfig,
    client: httpx.AsyncClient,
    run_id: str,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """
    Fetch and parse a single RSS feed.

    Uses the shared async httpx client for the HTTP request, then passes
    raw content to feedparser for RSS/Atom parsing. feedparser is synchronous
    but fast — we run it directly since parsing is CPU-light and non-blocking
    in practice at this scale.

    Returns a tuple of (entries, health_report). Even on failure, a health
    report is always returned so the caller has full visibility.
    """
    fetched_at = datetime.now(timezone.utc)

    # Step 1: Fetch raw feed content.
    try:
        response = await client.get(config.url)
        response.raise_for_status()
        raw_content = response.text
        last_modified = response.headers.get("last-modified")

    except httpx.TimeoutException:
        return [], FeedHealthReport(
            feed_id=config.id,
            feed_name=config.name,
            feed_url=config.url,
            status=FeedStatus.UNREACHABLE,
            error_message="Request timed out",
            fetched_at=fetched_at,
        )
    except httpx.HTTPStatusError as e:
        return [], FeedHealthReport(
            feed_id=config.id,
            feed_name=config.name,
            feed_url=config.url,
            status=FeedStatus.UNREACHABLE,
            error_message=f"HTTP {e.response.status_code}",
            fetched_at=fetched_at,
        )
    except httpx.RequestError as e:
        return [], FeedHealthReport(
            feed_id=config.id,
            feed_name=config.name,
            feed_url=config.url,
            status=FeedStatus.UNREACHABLE,
            error_message=str(e),
            fetched_at=fetched_at,
        )

    # Step 2: Parse RSS/Atom content.
    parsed = feedparser.parse(raw_content)

    if parsed.bozo and not parsed.entries:
        bozo_reason = str(getattr(parsed, "bozo_exception", "unknown parse error"))
        return [], FeedHealthReport(
            feed_id=config.id,
            feed_name=config.name,
            feed_url=config.url,
            status=FeedStatus.MALFORMED,
            error_message=f"Feed parse error: {bozo_reason}",
            fetched_at=fetched_at,
        )

    if not parsed.entries:
        return [], FeedHealthReport(
            feed_id=config.id,
            feed_name=config.name,
            feed_url=config.url,
            status=FeedStatus.EMPTY,
            entries_fetched=0,
            error_message="Feed returned no entries",
            fetched_at=fetched_at,
        )

    # Step 3: Convert parsed entries to RawFeedEntry models.
    entries = [
        _parse_entry(entry, config, fetched_at, run_id)
        for entry in parsed.entries
    ]

    status = FeedStatus.OK if len(entries) >= _MIN_EXPECTED_ENTRIES else FeedStatus.STALE

    return entries, FeedHealthReport(
        feed_id=config.id,
        feed_name=config.name,
        feed_url=config.url,
        status=status,
        entries_fetched=len(entries),
        fetched_at=fetched_at,
        feed_last_modified=last_modified,
    )


def _parse_entry(
    entry: feedparser.FeedParserDict,
    config: FeedConfig,
    fetched_at: datetime,
    run_id: str,
) -> RawFeedEntry:
    """
    Convert a feedparser entry dict to a RawFeedEntry model.

    All fields default to None for missing data. We do not parse dates,
    validate URLs, or judge whether this is an event. That is downstream work.
    """
    categories = [
        tag.get("term", "") for tag in entry.get("tags", []) if tag.get("term")
    ]

    description = None
    if entry.get("content"):
        description = entry["content"][0].get("value")
    if not description:
        description = entry.get("summary")

    enclosure_url: Optional[str] = None
    enclosures = entry.get("enclosures", [])
    if enclosures:
        enclosure_url = enclosures[0].get("url")

    return RawFeedEntry(
        feed_id=config.id,
        feed_name=config.name,
        source_url=config.url,
        title=entry.get("title"),
        link=entry.get("link"),
        description=description,
        published_raw=entry.get("published"),
        author=entry.get("author"),
        categories=categories,
        enclosure_url=enclosure_url,
        fetched_at=fetched_at,
        run_id=run_id,
    )


def _log_summary(run_id: str, reports: list[FeedHealthReport]) -> None:
    total = len(reports)
    succeeded = sum(1 for r in reports if r.succeeded)
    failed = total - succeeded
    total_entries = sum(r.entries_fetched for r in reports)

    logger.info(
        "[%s] Feed Scout complete: %d/%d feeds OK, %d failed, %d total entries",
        run_id,
        succeeded,
        total,
        failed,
        total_entries,
    )

    if failed > 0:
        failed_names = [r.feed_name for r in reports if not r.succeeded]
        logger.warning("[%s] Failed feeds: %s", run_id, ", ".join(failed_names))
