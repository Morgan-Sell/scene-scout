"""
Feed Scout Agent

Responsibility
--------------
Fetch, parse, validate, and monitor configured event sources concurrently.
Produce raw feed entries and a health report for each source.

Dispatches to a pluggable adapter per ``FeedConfig.source_type`` (RSS, iCal,
API, scrape). All adapters normalize to ``RawFeedEntry``.

This agent is deliberately deterministic. No LLM is involved.
Its job is reliability: read what is there, report what failed,
pass everything downstream faithfully without interpretation.

Two efficiency mechanisms are applied at the RSS adapter layer:

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
from urllib.parse import urlparse

import httpx

from scene_scout.agents.sources import CacheHooks, get_adapter
from scene_scout.models.feed import (
    FeedConfig,
    FeedHealthReport,
    RawFeedEntry,
    SourceType,
)

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 10
_USER_AGENT = "SceneScout/0.1 (event discovery agent)"


async def run(
    feed_configs: list[FeedConfig],
    run_id: str,
    get_feed_etag=None,
    store_feed_etag=None,
) -> tuple[list[RawFeedEntry], list[FeedHealthReport]]:
    """Fetch all configured sources concurrently.

    Uses asyncio.gather() so all sources are in-flight simultaneously. Total
    wall-clock time is bounded by the slowest source, not the sum of all sources.

    A failure on one source does not affect others. All failures are logged
    and reported, not raised. UNCHANGED (304) RSS feeds are logged and skipped.

    Parameters
    ----------
    feed_configs : list[FeedConfig]
        Active source configurations to process.
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
        All raw entries fetched across all sources, and a health report for
        every source regardless of success or failure status.
    """
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        cache_hooks = CacheHooks(
            client=client,
            get_feed_etag=get_feed_etag,
            store_feed_etag=store_feed_etag,
        )
        tasks = [_fetch_source(config, run_id, cache_hooks) for config in feed_configs]
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


async def validate_feed(
    url: str,
    source_type: SourceType | None = None,
) -> FeedHealthReport:
    """Validate a user-submitted source URL before saving it to config.

    Fetches via the adapter for ``source_type`` and checks for at least one
    entry where the adapter is implemented. Returns a health report that the
    web UI displays before persisting the source.

    When ``source_type`` is omitted, infers ``"ical"`` for ``.ics`` URLs and
    defaults to ``"rss"`` otherwise.

    Parameters
    ----------
    url : str
        The source URL to validate.
    source_type : SourceType, optional
        Explicit adapter to use. Inferred from the URL when omitted.

    Returns
    -------
    FeedHealthReport
        Health report indicating whether the source is usable. The caller
        should check ``report.succeeded`` before saving the source.
    """
    resolved_type = source_type or infer_source_type(url)
    config = FeedConfig(
        id="__validation__",
        name=url,
        url=url,
        city="unknown",
        source_quality_score=0.5,
        active=True,
        source_type=resolved_type,
    )

    if resolved_type != "rss":
        adapter = get_adapter(resolved_type)
        _, report = await adapter.fetch(
            config,
            run_id="validation",
            cache_hooks=CacheHooks(),
        )
        return report

    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        _, report = await get_adapter("rss").fetch(
            config,
            run_id="validation",
            cache_hooks=CacheHooks(client=client),
        )
    return report


def infer_source_type(url: str) -> SourceType:
    """Infer a source type from a URL when not explicitly configured.

    Parameters
    ----------
    url : str
        Source URL to inspect.

    Returns
    -------
    SourceType
        ``"ical"`` for ``.ics`` paths; ``"rss"`` otherwise.
    """
    path = urlparse(url).path.lower()
    if path.endswith(".ics"):
        return "ical"
    return "rss"


async def _fetch_source(
    config: FeedConfig,
    run_id: str,
    cache_hooks: CacheHooks,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Dispatch a single source fetch to the adapter for its ``source_type``."""
    adapter = get_adapter(config.source_type)
    return await adapter.fetch(config, run_id, cache_hooks)


def _log_summary(run_id: str, reports: list[FeedHealthReport]) -> None:
    """Log a summary of feed health across the full run."""
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
