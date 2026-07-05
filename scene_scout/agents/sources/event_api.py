"""
Event platform API source adapter.

Currently supports Eventbrite event search with geo filtering and pagination
via ``FeedConfig.cursor`` (1-based page number).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.config import get_eventbrite_api_token
from scene_scout.models.feed import (
    FeedConfig,
    FeedHealthReport,
    FeedStatus,
    RawFeedEntry,
)

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 15
_MIN_EXPECTED_ENTRIES = 1
_MAX_PAGES_PER_RUN = 3
_EVENTBRITE_SEARCH_URL = "https://www.eventbriteapi.com/v3/events/search/"
_USER_AGENT = "SceneScout/0.1 (event discovery agent)"

# Geo search parameters keyed by feed ``city`` (extend as new cities are added).
_CITY_SEARCH_PARAMS: dict[str, dict[str, str]] = {
    "New York": {
        "location.address": "New York, NY",
        "location.within": "50km",
    },
    "Los Angeles": {
        "location.address": "Los Angeles, CA",
        "location.within": "50km",
    },
}


class EventApiSourceAdapter:
    """Adapter for third-party event platform APIs."""

    async def fetch(
        self,
        config: FeedConfig,
        run_id: str,
        cache_hooks: CacheHooks,
    ) -> tuple[list[RawFeedEntry], FeedHealthReport]:
        """Fetch events from the platform identified by ``config.url``."""
        platform = _detect_platform(config.url)
        if platform == "eventbrite":
            if cache_hooks.client is not None:
                return await _fetch_eventbrite(config, cache_hooks.client, run_id)
            async with httpx.AsyncClient(
                timeout=_FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                return await _fetch_eventbrite(config, client, run_id)

        return _unsupported_platform(config, platform)


async def _fetch_eventbrite(
    config: FeedConfig,
    client: httpx.AsyncClient,
    run_id: str,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Fetch Eventbrite search results for the feed's city."""
    fetched_at = datetime.now(timezone.utc)
    token = get_eventbrite_api_token()
    if not token:
        return [], _health_report(
            config,
            FeedStatus.UNREACHABLE,
            fetched_at,
            error_message="EVENTBRITE_API_TOKEN is not configured",
        )

    start_page = _parse_cursor(config.cursor)
    params = _eventbrite_search_params(config)
    headers = {"Authorization": f"Bearer {token}"}

    all_events: list[dict[str, Any]] = []
    page = start_page
    pages_fetched = 0

    try:
        while pages_fetched < _MAX_PAGES_PER_RUN:
            page_params = {**params, "page": str(page)}
            response = await client.get(
                _EVENTBRITE_SEARCH_URL,
                params=page_params,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("events") or []
            if not isinstance(batch, list):
                return [], _health_report(
                    config,
                    FeedStatus.MALFORMED,
                    fetched_at,
                    error_message="Eventbrite response events field is not a list",
                )

            all_events.extend(batch)
            pages_fetched += 1

            pagination = payload.get("pagination") or {}
            if not pagination.get("has_more_items"):
                break
            page += 1

    except httpx.TimeoutException:
        return [], _health_report(
            config,
            FeedStatus.UNREACHABLE,
            fetched_at,
            error_message="Request timed out",
        )
    except httpx.HTTPStatusError as exc:
        return [], _health_report(
            config,
            FeedStatus.UNREACHABLE,
            fetched_at,
            error_message=f"HTTP {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        return [], _health_report(
            config,
            FeedStatus.UNREACHABLE,
            fetched_at,
            error_message=str(exc),
        )
    except json.JSONDecodeError as exc:
        return [], _health_report(
            config,
            FeedStatus.MALFORMED,
            fetched_at,
            error_message=f"Invalid JSON response: {exc}",
        )

    if not all_events:
        return [], _health_report(
            config,
            FeedStatus.EMPTY,
            fetched_at,
            entries_fetched=0,
            error_message="Eventbrite returned no events for this location",
        )

    entries = [
        _map_eventbrite_event(event, config, fetched_at, run_id) for event in all_events
    ]
    status = (
        FeedStatus.OK if len(entries) >= _MIN_EXPECTED_ENTRIES else FeedStatus.STALE
    )
    return entries, _health_report(
        config,
        status,
        fetched_at,
        entries_fetched=len(entries),
    )


def _map_eventbrite_event(
    event: dict[str, Any],
    config: FeedConfig,
    fetched_at: datetime,
    run_id: str,
) -> RawFeedEntry:
    """Map an Eventbrite event object to ``RawFeedEntry``."""
    name = _text_field(event.get("name"))
    description = _eventbrite_description(event)
    start = event.get("start") or {}
    published_raw = start.get("local") or start.get("utc")
    organizer = event.get("organizer") or {}
    author = _text_field(organizer.get("name"))
    category = event.get("category") or {}
    category_name = _text_field(category.get("name"))
    categories = [category_name] if category_name else []

    return RawFeedEntry(
        feed_id=config.id,
        feed_name=config.name,
        source_url=config.url,
        run_id=run_id,
        title=name,
        link=event.get("url"),
        description=description,
        published_raw=published_raw,
        author=author,
        categories=categories,
        fetched_at=fetched_at,
    )


def _eventbrite_search_params(config: FeedConfig) -> dict[str, str]:
    """Build Eventbrite search query parameters from feed config."""
    geo = _CITY_SEARCH_PARAMS.get(config.city)
    if geo is None:
        logger.warning(
            "No predefined geo params for city %r; using city name as address",
            config.city,
        )
        return {
            "location.address": config.city,
            "location.within": "50km",
            "expand": "organizer,category,venue",
        }
    return {**geo, "expand": "organizer,category,venue"}


def _detect_platform(url: str) -> str:
    """Return the API platform identifier encoded in ``url``."""
    parsed = urlparse(url)
    if parsed.scheme == "eventbrite":
        return "eventbrite"
    if "eventbriteapi.com" in (parsed.netloc or ""):
        return "eventbrite"
    return parsed.scheme or "unknown"


def _parse_cursor(cursor: Optional[str]) -> int:
    """Parse a 1-based page number from ``FeedConfig.cursor``."""
    if not cursor:
        return 1
    try:
        page = int(cursor)
    except ValueError:
        return 1
    return max(page, 1)


def _eventbrite_venue_name(event: dict[str, Any]) -> Optional[str]:
    """Return the venue name from an expanded Eventbrite event payload."""
    venue = event.get("venue")
    if not isinstance(venue, dict):
        return None
    return _text_field(venue.get("name"))


def _eventbrite_description(event: dict[str, Any]) -> Optional[str]:
    """Build entry description with venue context for downstream extraction."""
    body = _text_field(event.get("description")) or _text_field(event.get("summary"))
    venue = _eventbrite_venue_name(event)
    if venue and body:
        return f"Venue: {venue}\n\n{body}"
    if venue:
        return venue
    return body


def _text_field(value: Any) -> Optional[str]:
    """Extract plain text from Eventbrite's ``{text: ...}`` field objects."""
    if value is None:
        return None
    if isinstance(value, dict):
        text = value.get("text")
        return str(text).strip() if text else None
    text = str(value).strip()
    return text or None


def _unsupported_platform(
    config: FeedConfig,
    platform: str,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Return a health report for an unrecognized API platform URL."""
    fetched_at = datetime.now(timezone.utc)
    return [], _health_report(
        config,
        FeedStatus.UNREACHABLE,
        fetched_at,
        error_message=f"Unsupported API platform: {platform}",
    )


def _health_report(
    config: FeedConfig,
    status: FeedStatus,
    fetched_at: datetime,
    *,
    entries_fetched: int = 0,
    error_message: Optional[str] = None,
) -> FeedHealthReport:
    """Build a ``FeedHealthReport`` for an API fetch attempt."""
    return FeedHealthReport(
        feed_id=config.id,
        feed_name=config.name,
        feed_url=config.url,
        status=status,
        entries_fetched=entries_fetched,
        error_message=error_message,
        fetched_at=fetched_at,
    )
