"""
Event platform API source adapter.

Supports Eventbrite event search and Ticketmaster Discovery API with geo filtering
and pagination via ``FeedConfig.cursor``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.config import get_eventbrite_api_token, get_ticketmaster_api_key
from scene_scout.models.feed import (
    FeedConfig,
    FeedHealthReport,
    FeedStatus,
    RawFeedEntry,
)
from scene_scout.structured_categories import infer_categories_from_text

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 15
_MIN_EXPECTED_ENTRIES = 1
_MAX_PAGES_PER_RUN = 3
_EVENTBRITE_SEARCH_URL = "https://www.eventbriteapi.com/v3/events/search/"
_TICKETMASTER_SEARCH_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
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

_TICKETMASTER_CITY_PARAMS: dict[str, dict[str, str]] = {
    "New York": {
        "city": "New York",
        "stateCode": "NY",
        "countryCode": "US",
    },
    "Los Angeles": {
        "city": "Los Angeles",
        "stateCode": "CA",
        "countryCode": "US",
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
            return await _with_client(
                cache_hooks,
                lambda client: _fetch_eventbrite(config, client, run_id, cache_hooks),
            )
        if platform == "ticketmaster":
            return await _with_client(
                cache_hooks,
                lambda client: _fetch_ticketmaster(config, client, run_id, cache_hooks),
            )

        return _unsupported_platform(config, platform)


async def _with_client(
    cache_hooks: CacheHooks,
    fetch_fn,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Run ``fetch_fn(client)`` with a shared or ephemeral HTTP client."""
    if cache_hooks.client is not None:
        return await fetch_fn(cache_hooks.client)

    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        return await fetch_fn(client)


async def _fetch_eventbrite(
    config: FeedConfig,
    client: httpx.AsyncClient,
    run_id: str,
    cache_hooks: CacheHooks,
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
    params = _eventbrite_search_params(_geo_city_for_api(config, cache_hooks))
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


async def _fetch_ticketmaster(
    config: FeedConfig,
    client: httpx.AsyncClient,
    run_id: str,
    cache_hooks: CacheHooks,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Fetch Ticketmaster Discovery search results for the feed's metro."""
    fetched_at = datetime.now(timezone.utc)
    api_key = get_ticketmaster_api_key()
    if not api_key:
        return [], _health_report(
            config,
            FeedStatus.UNREACHABLE,
            fetched_at,
            error_message="TICKETMASTER_API_KEY is not configured",
        )

    start_page = _parse_ticketmaster_page(config.cursor)
    params = _ticketmaster_search_params(
        _geo_city_for_api(config, cache_hooks),
        horizon_days=cache_hooks.horizon_days,
        reference=fetched_at,
    )
    params["apikey"] = api_key

    all_events: list[dict[str, Any]] = []
    page = start_page
    pages_fetched = 0

    try:
        while pages_fetched < _MAX_PAGES_PER_RUN:
            page_params = {**params, "page": str(page)}
            response = await client.get(_TICKETMASTER_SEARCH_URL, params=page_params)
            response.raise_for_status()
            payload = response.json()
            embedded = payload.get("_embedded") or {}
            batch = embedded.get("events") or []
            if not isinstance(batch, list):
                return [], _health_report(
                    config,
                    FeedStatus.MALFORMED,
                    fetched_at,
                    error_message="Ticketmaster response events field is not a list",
                )

            all_events.extend(batch)
            pages_fetched += 1

            page_info = payload.get("page") or {}
            total_pages = int(page_info.get("totalPages") or 0)
            if page + 1 >= total_pages:
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
            error_message="Ticketmaster returned no events for this location",
        )

    entries = [
        _map_ticketmaster_event(event, config, fetched_at, run_id)
        for event in all_events
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


def _map_ticketmaster_event(
    event: dict[str, Any],
    config: FeedConfig,
    fetched_at: datetime,
    run_id: str,
) -> RawFeedEntry:
    """Map a Ticketmaster event object to ``RawFeedEntry``."""
    name = _text_field(event.get("name"))
    url = event.get("url")
    info = _text_field(event.get("info")) or _text_field(event.get("pleaseNote"))
    venue_name, venue_city = _ticketmaster_venue(event)
    published_raw = _ticketmaster_start_datetime(event)
    classification_labels = _ticketmaster_classification_labels(event)
    categories = infer_categories_from_text(
        title=name,
        description=info,
        extra_labels=classification_labels,
    )

    description = info
    if venue_name and description:
        description = f"Venue: {venue_name}\n\n{description}"
    elif venue_name:
        description = venue_name

    return RawFeedEntry(
        feed_id=config.id,
        feed_name=config.name,
        source_url=config.url,
        run_id=run_id,
        source_type="api",
        title=name,
        link=url,
        description=description,
        published_raw=published_raw,
        categories=categories,
        event_venue=venue_name,
        event_city=venue_city or config.city,
        fetched_at=fetched_at,
    )


def _ticketmaster_search_params(
    geo_city: str,
    *,
    horizon_days: int | None,
    reference: datetime,
) -> dict[str, str]:
    """Build Ticketmaster search query parameters from a metro name."""
    geo = _TICKETMASTER_CITY_PARAMS.get(geo_city)
    if geo is None:
        logger.warning(
            "No predefined Ticketmaster params for city %r; using city name only",
            geo_city,
        )
        params = {
            "city": geo_city,
            "countryCode": "US",
        }
    else:
        params = dict(geo)

    params.update(
        {
            "size": "20",
            "sort": "date,asc",
            "includeTBA": "no",
            "includeTBD": "no",
            "includeTest": "no",
        }
    )

    start = reference.replace(microsecond=0)
    params["startDateTime"] = start.isoformat().replace("+00:00", "Z")
    if horizon_days is not None and horizon_days > 0:
        end = start + timedelta(days=horizon_days)
        params["endDateTime"] = end.isoformat().replace("+00:00", "Z")

    return params


def _ticketmaster_venue(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return venue name and city from a Ticketmaster event payload."""
    embedded = event.get("_embedded") or {}
    venues = embedded.get("venues") or []
    if not venues or not isinstance(venues[0], dict):
        return None, None
    venue = venues[0]
    name = _text_field(venue.get("name"))
    city_block = venue.get("city") or {}
    city = _text_field(city_block.get("name")) if isinstance(city_block, dict) else None
    return name, city


def _ticketmaster_start_datetime(event: dict[str, Any]) -> str | None:
    """Return a datetime string suitable for structured ingest."""
    dates = event.get("dates") or {}
    start = dates.get("start") or {}
    if not isinstance(start, dict):
        return None
    date_time = start.get("dateTime")
    if date_time:
        return str(date_time).strip()
    local_date = start.get("localDate")
    local_time = start.get("localTime")
    if local_date and local_time:
        return f"{local_date}T{local_time}"
    if local_date:
        return str(local_date).strip()
    return None


def _ticketmaster_classification_labels(event: dict[str, Any]) -> list[str]:
    """Extract segment/genre/type labels from Ticketmaster classifications."""
    labels: list[str] = []
    for classification in event.get("classifications") or []:
        if not isinstance(classification, dict):
            continue
        for key in ("segment", "genre", "subGenre", "type", "subType"):
            block = classification.get(key)
            if isinstance(block, dict):
                name = _text_field(block.get("name"))
                if name:
                    labels.append(name)
    return labels


def _parse_ticketmaster_page(cursor: Optional[str]) -> int:
    """Parse a 0-based page number from ``FeedConfig.cursor``."""
    if not cursor:
        return 0
    try:
        page = int(cursor)
    except ValueError:
        return 0
    return max(page, 0)


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
    venue = _eventbrite_venue_name(event)

    return RawFeedEntry(
        feed_id=config.id,
        feed_name=config.name,
        source_url=config.url,
        run_id=run_id,
        source_type="api",
        title=name,
        link=event.get("url"),
        description=description,
        published_raw=published_raw,
        author=author,
        categories=categories,
        event_venue=venue,
        event_city=config.city,
        fetched_at=fetched_at,
    )


def _geo_city_for_api(config: FeedConfig, cache_hooks: CacheHooks) -> str:
    """Resolve the metro name used for API geo search parameters."""
    if config.is_national and cache_hooks.home_city and cache_hooks.home_city.strip():
        return cache_hooks.home_city.strip()
    return config.city


def _eventbrite_search_params(geo_city: str) -> dict[str, str]:
    """Build Eventbrite search query parameters from a metro name."""
    geo = _CITY_SEARCH_PARAMS.get(geo_city)
    if geo is None:
        logger.warning(
            "No predefined geo params for city %r; using city name as address",
            geo_city,
        )
        return {
            "location.address": geo_city,
            "location.within": "50km",
            "expand": "organizer,category,venue",
        }
    return {**geo, "expand": "organizer,category,venue"}


def _detect_platform(url: str) -> str:
    """Return the API platform identifier encoded in ``url``."""
    parsed = urlparse(url)
    if parsed.scheme == "eventbrite":
        return "eventbrite"
    if parsed.scheme == "ticketmaster":
        return "ticketmaster"
    if "eventbriteapi.com" in (parsed.netloc or ""):
        return "eventbrite"
    if "ticketmaster.com" in (parsed.netloc or ""):
        return "ticketmaster"
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
