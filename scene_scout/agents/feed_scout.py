"""
Feed Scout Agent

Responsibility
--------------
Fetch, parse, validate, and monitor configured RSS feeds concurrently.
Produce raw feed entries and a health report for each feed.

This agent is deliberately deterministic. No LLM is involved.
Its job is reliability: read what is there, report what failed,
pass everything downstream faithfully without interpretation.

Two efficiency mechanisms are applied at this layer:

1. HTTP change detection (ETag / Last-Modified):
   Before parsing, sends conditional request headers. On 304 Not Modified,
   the feed is skipped entirely with status UNCHANGED. No parsing, no entries.

2. seen_entries cache integration:
   Applied by the orchestrator between Feed Scout and Extraction. Feed Scout
   itself only fetches and parses -- it does not check the seen_entries cache.

Design
------
Inputs  : list[FeedConfig], run_id: str
Outputs : tuple[list[RawFeedEntry], list[FeedHealthReport]]
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
    get_feed_etag=None,
    store_feed_etag=None,
) -> tuple[list[RawFeedEntry], list[FeedHealthReport]]:
    """Fetch all configured feeds concurrently.

    Uses asyncio.gather() so all feeds are in-flight simultaneously. Total
    wall-clock time is bounded by the slowest feed, not the sum of all feeds.

    A failure on one feed does not affect others. All failures are logged
    and reported, not raised. UNCHANGED (304) feeds are logged and skipped.

    Parameters
    ----------
    feed_configs : list[FeedConfig]
        Active feed configurations to process.
    run_id : str
        Pipeline run identifier, attached to every RawFeedEntry produced.
    get_feed_etag : callable, optional
        Cache lookup function: (feed_id) -> (etag, last_modified) | None.
        If None, conditional request headers are not sent.
    store_feed_etag : callable, optional
        Cache write function: (feed_id, etag, last_modified) -> None.
        If None, ETag values are not persisted.

    Returns
    -------
    tuple[list[RawFeedEntry], list[FeedHealthReport]]
        All raw entries fetched across all feeds, and a health report for
        every feed regardless of success or failure status.
    """
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "SceneScout/0.1 (event discovery agent)"},
    ) as client:
        tasks = [
            _fetch_feed(config, client, run_id, get_feed_etag, store_feed_etag)
            for config in feed_configs
        ]
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
        elif report.skipped:
            logger.info(
                "[%s] Feed UNCHANGED (304): %s — skipped",
                run_id,
                report.feed_name,
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
    """Validate a user-submitted RSS URL before saving it to config.

    Fetches, parses, and checks for at least one entry. Returns a health
    report that the web UI displays before persisting the feed.

    Parameters
    ----------
    url : str
        The RSS URL to validate.

    Returns
    -------
    FeedHealthReport
        Health report indicating whether the feed is usable. The caller
        should check `report.succeeded` before saving the feed.
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
    get_feed_etag=None,
    store_feed_etag=None,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Fetch and parse a single RSS feed.

    Sends conditional request headers (ETag / Last-Modified) when available.
    On 304 Not Modified, returns UNCHANGED status with no entries. On success,
    stores the new ETag and Last-Modified values for the next request.

    Parameters
    ----------
    config : FeedConfig
        Feed configuration for this fetch.
    client : httpx.AsyncClient
        Shared async HTTP client.
    run_id : str
        Pipeline run identifier for log correlation.
    get_feed_etag : callable, optional
        Cache lookup: (feed_id) -> (etag, last_modified) | None.
    store_feed_etag : callable, optional
        Cache write: (feed_id, etag, last_modified) -> None.

    Returns
    -------
    tuple[list[RawFeedEntry], FeedHealthReport]
        Parsed entries and a health report. On any failure, entries list
        is empty and the health report describes the failure.
    """
    fetched_at = datetime.now(timezone.utc)

    # Build conditional request headers from cached ETag / Last-Modified
    conditional_headers: dict[str, str] = {}
    if get_feed_etag is not None:
        cached = get_feed_etag(config.id)
        if cached:
            etag, last_modified = cached
            if etag:
                conditional_headers["If-None-Match"] = etag
            if last_modified:
                conditional_headers["If-Modified-Since"] = last_modified

    # Step 1: Fetch raw feed content
    try:
        response = await client.get(config.url, headers=conditional_headers)

        # 304 Not Modified — feed unchanged, skip processing
        if response.status_code == 304:
            return [], FeedHealthReport(
                feed_id=config.id,
                feed_name=config.name,
                feed_url=config.url,
                status=FeedStatus.UNCHANGED,
                entries_fetched=0,
                fetched_at=fetched_at,
                etag_supported=True,
            )

        response.raise_for_status()
        raw_content = response.text
        response_etag = response.headers.get("etag")
        response_last_modified = response.headers.get("last-modified")
        etag_supported = bool(response_etag or response_last_modified)

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

    # Step 2: Persist new ETag / Last-Modified for next request
    if store_feed_etag is not None and (response_etag or response_last_modified):
        store_feed_etag(config.id, response_etag, response_last_modified)

    # Step 3: Parse RSS/Atom content
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
            etag_supported=etag_supported,
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
            etag_supported=etag_supported,
        )

    # Step 4: Convert parsed entries to RawFeedEntry models
    entries = [
        _parse_entry(entry, config, fetched_at, run_id) for entry in parsed.entries
    ]

    status = (
        FeedStatus.OK if len(entries) >= _MIN_EXPECTED_ENTRIES else FeedStatus.STALE
    )

    return entries, FeedHealthReport(
        feed_id=config.id,
        feed_name=config.name,
        feed_url=config.url,
        status=status,
        entries_fetched=len(entries),
        fetched_at=fetched_at,
        feed_last_modified=response_last_modified,
        etag=response_etag,
        etag_supported=etag_supported,
    )


def _parse_entry(
    entry: feedparser.FeedParserDict,
    config: FeedConfig,
    fetched_at: datetime,
    run_id: str,
) -> RawFeedEntry:
    """Convert a feedparser entry dict to a RawFeedEntry model.

    All fields default to None for missing data. Dates are preserved as raw
    strings. URLs are not validated. Whether this is an event is not judged.
    That is downstream work.

    Parameters
    ----------
    entry : feedparser.FeedParserDict
        Parsed entry dict from feedparser.
    config : FeedConfig
        Source feed configuration.
    fetched_at : datetime
        UTC timestamp of the fetch.
    run_id : str
        Pipeline run identifier.

    Returns
    -------
    RawFeedEntry
        Faithful representation of the feed entry.
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
        run_id=run_id,
        title=entry.get("title"),
        link=entry.get("link"),
        description=description,
        published_raw=entry.get("published"),
        author=entry.get("author"),
        categories=categories,
        enclosure_url=enclosure_url,
        fetched_at=fetched_at,
    )


def _log_summary(run_id: str, reports: list[FeedHealthReport]) -> None:
    """Log a summary of feed health across the full run.

    Parameters
    ----------
    run_id : str
        Pipeline run identifier for log correlation.
    reports : list[FeedHealthReport]
        Health reports for all feeds processed in this run.
    """
    total = len(reports)
    succeeded = sum(1 for r in reports if r.succeeded)
    unchanged = sum(1 for r in reports if r.skipped)
    failed = total - succeeded - unchanged
    total_entries = sum(r.entries_fetched for r in reports)
    etag_supported = sum(1 for r in reports if r.etag_supported)

    logger.info(
        "[%s] Feed Scout complete: %d/%d feeds OK, %d unchanged (304), "
        "%d failed, %d total entries, %d/%d feeds support ETag",
        run_id,
        succeeded,
        total,
        unchanged,
        failed,
        total_entries,
        etag_supported,
        total,
    )

    if failed > 0:
        failed_names = [
            r.feed_name for r in reports if not r.succeeded and not r.skipped
        ]
        logger.warning("[%s] Failed feeds: %s", run_id, ", ".join(failed_names))
