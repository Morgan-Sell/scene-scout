"""
RSS/Atom source adapter.

Fetches and parses RSS feeds with ETag / Last-Modified conditional requests.
"""

from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx

from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.models.feed import (
    FeedConfig,
    FeedHealthReport,
    FeedStatus,
    RawFeedEntry,
)

_FETCH_TIMEOUT_SECONDS = 10
_MIN_EXPECTED_ENTRIES = 1


class RssSourceAdapter:
    """Adapter for RSS and Atom feed URLs."""

    async def fetch(
        self,
        config: FeedConfig,
        run_id: str,
        cache_hooks: CacheHooks,
    ) -> tuple[list[RawFeedEntry], FeedHealthReport]:
        """Fetch and parse a single RSS feed."""
        if cache_hooks.client is None:
            raise ValueError("RSS adapter requires an httpx.AsyncClient in cache_hooks")

        return await _fetch_feed(
            config,
            cache_hooks.client,
            run_id,
            cache_hooks.get_feed_etag,
            cache_hooks.store_feed_etag,
        )


async def _fetch_feed(
    config: FeedConfig,
    client: httpx.AsyncClient,
    run_id: str,
    get_feed_etag=None,
    store_feed_etag=None,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Fetch and parse a single RSS feed."""
    fetched_at = datetime.now(timezone.utc)

    conditional_headers: dict[str, str] = {}
    if get_feed_etag is not None:
        cached = get_feed_etag(config.id)
        if cached:
            etag, last_modified = cached
            if etag:
                conditional_headers["If-None-Match"] = etag
            if last_modified:
                conditional_headers["If-Modified-Since"] = last_modified

    try:
        response = await client.get(config.url, headers=conditional_headers)

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

    if store_feed_etag is not None and (response_etag or response_last_modified):
        store_feed_etag(config.id, response_etag, response_last_modified)

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
    """Convert a feedparser entry dict to a RawFeedEntry model."""
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
