"""
iCal/ICS source adapter.

Fetches calendar files and maps VEVENT components to RawFeedEntry.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
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
from scene_scout.normalization_config import DEFAULT_PIPELINE_HORIZON_DAYS

_FETCH_TIMEOUT_SECONDS = 10
_MIN_EXPECTED_ENTRIES = 1
_USER_AGENT = "SceneScout/0.1 (event discovery agent)"
logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current UTC time (patchable in tests)."""
    return datetime.now(timezone.utc)


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
                cache_hooks,
            )

        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            return await _fetch_ical(config, client, run_id, cache_hooks)


async def _fetch_ical(
    config: FeedConfig,
    client: httpx.AsyncClient,
    run_id: str,
    cache_hooks: CacheHooks,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Fetch and parse a single ICS calendar."""
    fetched_at = _utc_now()
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

    vevents = list(calendar.walk("VEVENT"))
    if not vevents:
        return [], _health_report(
            config,
            FeedStatus.EMPTY,
            fetched_at,
            entries_fetched=0,
            error_message="Calendar returned no VEVENT entries",
        )

    horizon_days = cache_hooks.horizon_days or DEFAULT_PIPELINE_HORIZON_DAYS
    kept_vevents = _filter_vevents_by_window(
        vevents,
        now=fetched_at,
        window_days=horizon_days,
        feed_id=config.id,
    )

    if not kept_vevents:
        return [], _health_report(
            config,
            FeedStatus.EMPTY,
            fetched_at,
            entries_fetched=0,
            error_message=(f"No VEVENT entries within {horizon_days}-day window"),
        )

    entries = [
        _parse_vevent(component, config, fetched_at, run_id)
        for component in kept_vevents
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


def _parse_ical_datetime(value: Any) -> datetime | None:
    """Parse an iCalendar date/time property to a timezone-aware UTC datetime."""
    if value is None:
        return None

    dt_value = getattr(value, "dt", None)
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            return dt_value.replace(tzinfo=timezone.utc)
        return dt_value.astimezone(timezone.utc)
    if isinstance(dt_value, date):
        return datetime.combine(dt_value, datetime.min.time(), tzinfo=timezone.utc)

    return None


def _is_all_day_property(value: Any) -> bool:
    """Return True when an iCalendar property uses a DATE (all-day) value."""
    dt_value = getattr(value, "dt", None)
    return isinstance(dt_value, date) and not isinstance(dt_value, datetime)


def _end_of_utc_day(day: date) -> datetime:
    """Return the last representable instant on ``day`` in UTC."""
    return datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc).replace(
        microsecond=0,
    )


def _vevent_end(component: Component, start: datetime) -> datetime:
    """Return the inclusive end instant for overlap checks."""
    dtend_prop = component.get("dtend")
    if dtend_prop is None:
        if _is_all_day_property(component.get("dtstart")):
            return _end_of_utc_day(start.date())
        return start

    end = _parse_ical_datetime(dtend_prop)
    if end is None:
        return start

    if _is_all_day_property(dtend_prop):
        inclusive_last_day = end.date() - timedelta(days=1)
        return _end_of_utc_day(inclusive_last_day)

    return end


def _vevent_intersects_window(
    component: Component,
    *,
    now: datetime,
    window_days: int = DEFAULT_PIPELINE_HORIZON_DAYS,
) -> bool:
    """Return True when a VEVENT overlaps the coming ``window_days``."""
    start = _parse_ical_datetime(component.get("dtstart"))
    if start is None:
        return False

    end = _vevent_end(component, start)
    reference = now.astimezone(timezone.utc)
    window_end = reference + timedelta(days=window_days)
    return start <= window_end and end >= reference


def _filter_vevents_by_window(
    components: list[Component],
    *,
    now: datetime,
    window_days: int,
    feed_id: str,
) -> list[Component]:
    """Keep VEVENT components whose DTSTART/DTEND overlap the window."""
    kept = [
        component
        for component in components
        if _vevent_intersects_window(component, now=now, window_days=window_days)
    ]
    dropped = len(components) - len(kept)
    if dropped:
        logger.info(
            "Filtered iCal VEVENTs outside %s-day window for feed %s",
            window_days,
            feed_id,
            extra={
                "data": {
                    "feed_id": feed_id,
                    "vevents_total": len(components),
                    "vevents_kept": len(kept),
                    "vevents_dropped": dropped,
                    "window_days": window_days,
                }
            },
        )
    return kept


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
    location = _decode_text(component.get("location"))

    return RawFeedEntry(
        feed_id=config.id,
        feed_name=config.name,
        source_url=config.url,
        run_id=run_id,
        source_type="ical",
        title=summary,
        link=link,
        description=description,
        published_raw=published_raw,
        author=author,
        categories=categories,
        event_venue=location,
        event_city=config.city,
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
