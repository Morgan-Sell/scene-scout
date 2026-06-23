"""
iCal/ICS source adapter.

Fetches calendar files and maps VEVENT components to RawFeedEntry.
"""

from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from icalendar import Calendar, Component

from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.models.feed import (
    FeedConfig,
    FeedHealthReport,
    FeedStatus,
    RawFeedEntry,
)

_FETCH_TIMEOUT_SECONDS = 10
_MIN_EXPECTED_ENTRIES = 1
_USER_AGENT = "SceneScout/0.1 (event discovery agent)"


class IcalSourceAdapter:
    """Adapter for iCalendar (.ics) subscription URLs."""

    async def fetch(
        self,
        config: FeedConfig,
        run_id: str,
        cache_hooks: CacheHooks,
    ) -> tuple[list[RawFeedEntry], FeedHealthReport]:
        """Fetch and parse a single ICS calendar."""
        if cache_hooks.client is not None:
            return await _fetch_ical(
                config,
                cache_hooks.client,
                run_id,
            )

        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            return await _fetch_ical(config, client, run_id)


async def _fetch_ical(
    config: FeedConfig,
    client: httpx.AsyncClient,
    run_id: str,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Fetch and parse a single ICS calendar."""
    fetched_at = datetime.now(timezone.utc)
    request_url = _normalize_ics_url(config.url)

    try:
        response = await client.get(request_url)
        response.raise_for_status()
        raw_content = response.content
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

    try:
        calendar = Calendar.from_ical(raw_content)
    except (ValueError, TypeError) as exc:
        return [], _health_report(
            config,
            FeedStatus.MALFORMED,
            fetched_at,
            error_message=f"ICS parse error: {exc}",
        )

    if not isinstance(calendar, Component):
        return [], _health_report(
            config,
            FeedStatus.MALFORMED,
            fetched_at,
            error_message="ICS parse error: root component is not a calendar",
        )

    entries = [
        _parse_vevent(component, config, fetched_at, run_id)
        for component in calendar.walk("VEVENT")
    ]

    if not entries:
        return [], _health_report(
            config,
            FeedStatus.EMPTY,
            fetched_at,
            entries_fetched=0,
            error_message="Calendar returned no VEVENT entries",
        )

    status = (
        FeedStatus.OK if len(entries) >= _MIN_EXPECTED_ENTRIES else FeedStatus.STALE
    )
    return entries, _health_report(
        config,
        status,
        fetched_at,
        entries_fetched=len(entries),
    )


def _parse_vevent(
    component: Component,
    config: FeedConfig,
    fetched_at: datetime,
    run_id: str,
) -> RawFeedEntry:
    """Convert a VEVENT component to a RawFeedEntry model."""
    summary = _decode_text(component.get("summary"))
    description = _decode_text(component.get("description"))
    link = _decode_text(component.get("url"))
    published_raw = _format_dtstart(component.get("dtstart"))
    author = _decode_organizer(component.get("organizer"))
    categories = _decode_categories(component.get("categories"))

    return RawFeedEntry(
        feed_id=config.id,
        feed_name=config.name,
        source_url=config.url,
        run_id=run_id,
        title=summary,
        link=link,
        description=description,
        published_raw=published_raw,
        author=author,
        categories=categories,
        fetched_at=fetched_at,
    )


def _decode_text(value) -> Optional[str]:
    """Return a plain string from an iCalendar property, if present."""
    if value is None:
        return None
    decoded = (
        value.to_ical().decode("utf-8") if hasattr(value, "to_ical") else str(value)
    )
    stripped = decoded.strip()
    return stripped or None


def _decode_organizer(value) -> Optional[str]:
    """Return organizer common name or email from an ORGANIZER property."""
    if value is None:
        return None
    params = getattr(value, "params", {}) or {}
    common_name = params.get("CN") or params.get("cn")
    if common_name:
        return str(common_name)
    return _decode_text(value)


def _decode_categories(value) -> list[str]:
    """Return category tags from a CATEGORIES property."""
    if value is None:
        return []
    decoded = _decode_text(value)
    if not decoded:
        return []
    return [part.strip() for part in decoded.split(",") if part.strip()]


def _format_dtstart(value) -> Optional[str]:
    """Preserve DTSTART as a readable raw string without downstream parsing."""
    if value is None:
        return None

    dt_value = getattr(value, "dt", None)
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            return dt_value.isoformat()
        return dt_value.astimezone(timezone.utc).isoformat()
    if isinstance(dt_value, date):
        return dt_value.isoformat()

    return _decode_text(value)


def _normalize_ics_url(url: str) -> str:
    """Convert webcal:// subscription links to fetchable https:// URLs."""
    parsed = urlparse(url)
    if parsed.scheme.lower() == "webcal":
        return parsed._replace(scheme="https").geturl()
    return url


def _health_report(
    config: FeedConfig,
    status: FeedStatus,
    fetched_at: datetime,
    *,
    entries_fetched: int = 0,
    error_message: Optional[str] = None,
) -> FeedHealthReport:
    """Build a FeedHealthReport for an ICS fetch attempt."""
    return FeedHealthReport(
        feed_id=config.id,
        feed_name=config.name,
        feed_url=config.url,
        status=status,
        entries_fetched=entries_fetched,
        error_message=error_message,
        fetched_at=fetched_at,
    )
