"""
Event Normalization Agent

Responsibility
--------------
Convert ``EventCandidate`` records into validated ``NormalizedEvent`` records.
Deterministic parsing and cleanup — no LLM calls.

Design
------
Inputs  : list[EventCandidate], run_id: str
Outputs : list[NormalizedEvent]
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from dateutil import parser as date_parser

from scene_scout.config import load_feed_configs
from scene_scout.logging import get_logger
from scene_scout.models.event import (
    EventCandidate,
    NormalizedEvent,
    compute_normalized_event_id,
)
from scene_scout.normalization_config import (
    CATEGORY_ALIASES,
    EVENT_CATEGORIES,
    MAX_RECURRING_OCCURRENCES,
    NORMALIZATION_DISCARD_JSONL_SAMPLE_SIZE,
    NORMALIZATION_DISCARD_TERMINAL_SAMPLE_SIZE,
    NORMALIZATION_WINDOW_DAYS,
)

_TRAILING_PUNCTUATION = re.compile(r"[.,;:!?]+$")
_WHITESPACE = re.compile(r"\s+")
_PRICE_DOLLARS = re.compile(r"\$(\d+(?:\.\d{2})?)")
_FREE_PRICE_TOKENS = frozenset({"free", "no cover", "gratis", "complimentary"})

_FESTIVAL_CROSS_MONTH_RE = re.compile(
    r"^(?P<start_month>[A-Za-z]+)\s+(?P<start_day>\d{1,2})\s*[-–]\s*"
    r"(?P<end_month>[A-Za-z]+)\s+(?P<end_day>\d{1,2})$",
    re.IGNORECASE,
)
_FESTIVAL_SAME_MONTH_RE = re.compile(
    r"^(?P<month>[A-Za-z]+)\s+(?P<start_day>\d{1,2})\s*(?:[-–]|&)\s*"
    r"(?P<end_day>\d{1,2})$",
    re.IGNORECASE,
)
_WEEKDAY_SERIES_RE = re.compile(
    r"^(?P<weekday>mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|"
    r"saturdays?|sundays?)\s*,\s*(?P<start>.+?)\s+through\s+(?P<end>.+)$",
    re.IGNORECASE,
)
_WEEKDAY_TO_INDEX = {
    "monday": 0,
    "mondays": 0,
    "tuesday": 1,
    "tuesdays": 1,
    "wednesday": 2,
    "wednesdays": 2,
    "thursday": 3,
    "thursdays": 3,
    "friday": 4,
    "fridays": 4,
    "saturday": 5,
    "saturdays": 5,
    "sunday": 6,
    "sundays": 6,
}

DISCARD_UNPARSEABLE_DATE = "unparseable_date"
DISCARD_MISSING_VENUE = "missing_venue"
DISCARD_INVALID_URL = "invalid_url"
DISCARD_OUTSIDE_WINDOW = "outside_window"
DISCARD_NORMALIZATION_ERROR = "normalization_error"

_DISCARD_REASON_LABELS: dict[str, str] = {
    DISCARD_UNPARSEABLE_DATE: "unparseable date",
    DISCARD_MISSING_VENUE: "missing venue",
    DISCARD_INVALID_URL: "invalid URL",
    DISCARD_OUTSIDE_WINDOW: "outside normalization window",
    DISCARD_NORMALIZATION_ERROR: "normalization error",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ParsedEventDatetime:
    """Result of parsing candidate date/time strings."""

    value: datetime | None
    time_dropped: bool = False


@dataclass(frozen=True)
class NormalizationResult:
    """Outcome of normalizing a single candidate."""

    events: tuple[NormalizedEvent, ...] = ()
    discard_reason: str | None = None
    discard_data: dict[str, Any] | None = None
    time_dropped: bool = False

    @property
    def event(self) -> NormalizedEvent | None:
        """First normalized event, if any (single-candidate convenience)."""
        return self.events[0] if self.events else None


@dataclass(frozen=True)
class FestivalRange:
    """Inclusive festival date span."""

    start: datetime
    end: datetime
    time_dropped: bool = False


@dataclass(frozen=True)
class ExpandedSchedule:
    """Parsed schedule shapes from a candidate date string."""

    festivals: tuple[FestivalRange, ...] = ()
    occurrences: tuple[ParsedEventDatetime, ...] = ()
    singles: tuple[ParsedEventDatetime, ...] = ()
    unparseable: bool = False


class NormalizationDiscardCollector:
    """Aggregate normalization discards for capped terminal and JSONL output."""

    def __init__(self) -> None:
        self._by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def record(self, reason: str, discard_data: dict[str, Any]) -> None:
        self._by_reason[reason].append(discard_data)

    @property
    def total(self) -> int:
        return sum(len(entries) for entries in self._by_reason.values())

    def emit(self, logger: Any) -> None:
        if not self._by_reason:
            return

        for reason in sorted(self._by_reason):
            discards = self._by_reason[reason]
            count = len(discards)
            sample_titles = [
                str(entry.get("title", ""))
                for entry in discards[:NORMALIZATION_DISCARD_TERMINAL_SAMPLE_SIZE]
                if entry.get("title")
            ]
            label = _DISCARD_REASON_LABELS.get(reason, reason)
            examples = ""
            if sample_titles:
                examples = f" (e.g. {', '.join(sample_titles)}"
                if count > len(sample_titles):
                    examples += ", …"
                examples += ")"

            logger.info(
                "Normalization discards — %s: %d%s",
                label,
                count,
                examples,
                data={
                    "discard_reason": reason,
                    "count": count,
                    "sample_titles": sample_titles,
                    "discards": discards[:NORMALIZATION_DISCARD_JSONL_SAMPLE_SIZE],
                    "discards_truncated": count
                    > NORMALIZATION_DISCARD_JSONL_SAMPLE_SIZE,
                },
            )

        logger.info(
            "Normalization discard summary",
            data={
                "total_discarded": self.total,
                "by_reason": {
                    reason: len(entries)
                    for reason, entries in sorted(self._by_reason.items())
                },
            },
        )


def normalize_venue_name(venue: str | None) -> str | None:
    """Strip trailing punctuation and collapse internal whitespace."""
    if venue is None:
        return None
    cleaned = _TRAILING_PUNCTUATION.sub("", venue.strip())
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or None


def standardize_categories(categories: list[str]) -> list[str]:
    """Map raw category labels to the controlled vocabulary."""
    standardized: list[str] = []
    seen: set[str] = set()

    for raw in categories:
        key = raw.strip().lower()
        if not key:
            continue

        canonical = CATEGORY_ALIASES.get(key)
        if canonical is None:
            title_key = raw.strip().title()
            if title_key in EVENT_CATEGORIES:
                canonical = title_key
            elif key.title() in EVENT_CATEGORIES:
                canonical = key.title()

        if canonical is None or canonical in seen:
            continue

        seen.add(canonical)
        standardized.append(canonical)

    return standardized


def is_valid_url(url: str) -> bool:
    """Return True when ``url`` has an HTTP(S) scheme and host."""
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _try_parse_datetime(
    value: str,
    *,
    default: datetime | None = None,
) -> datetime | None:
    """Parse a single date/time string via ``dateutil``."""
    try:
        parsed = date_parser.parse(value.strip(), default=default)
    except (ValueError, OverflowError, TypeError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_event_datetime(
    date: str | None,
    time: str | None,
    *,
    default: datetime | None = None,
) -> ParsedEventDatetime:
    """Parse candidate date/time strings via ``dateutil``.

    When ``date + time`` fails (e.g. duration-style ranges), falls back to
    date-only parsing using ``default`` (noon UTC in normalization).
    """
    date_part = date.strip() if date else ""
    time_part = time.strip() if time else ""
    if not date_part and not time_part:
        return ParsedEventDatetime(None)

    parts = [part for part in (date_part, time_part) if part]
    combined = " ".join(parts)
    parsed = _try_parse_datetime(combined, default=default)
    if parsed is not None:
        return ParsedEventDatetime(parsed)

    if date_part and time_part:
        date_only = _try_parse_datetime(date_part, default=default)
        if date_only is not None:
            return ParsedEventDatetime(date_only, time_dropped=True)

    if date_part:
        date_only = _try_parse_datetime(date_part, default=default)
        if date_only is not None:
            return ParsedEventDatetime(date_only)

    return ParsedEventDatetime(None)


def _parse_month_day(
    month: str,
    day: int,
    *,
    time: str | None,
    default: datetime,
) -> ParsedEventDatetime:
    """Parse a month/day token with optional time."""
    date_text = f"{month} {day}"
    if time and time.strip():
        return parse_event_datetime(date_text, time, default=default)
    noon_default = default.replace(hour=12, minute=0, second=0, microsecond=0)
    return parse_event_datetime(date_text, None, default=noon_default)


def _parse_festival_range(
    date: str,
    time: str | None,
    *,
    default: datetime,
) -> FestivalRange | None:
    """Parse multi-day festival spans such as ``June 27-28`` or ``August 28 & 29``."""
    cross_month = _FESTIVAL_CROSS_MONTH_RE.match(date.strip())
    if cross_month:
        start = _parse_month_day(
            cross_month.group("start_month"),
            int(cross_month.group("start_day")),
            time=time,
            default=default,
        )
        end = _parse_month_day(
            cross_month.group("end_month"),
            int(cross_month.group("end_day")),
            time=None,
            default=default,
        )
        if start.value is None or end.value is None:
            return None
        start_dt = start.value
        end_dt = end.value.replace(hour=23, minute=59, second=59, microsecond=0)
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt
        return FestivalRange(
            start=start_dt,
            end=end_dt,
            time_dropped=start.time_dropped,
        )

    same_month = _FESTIVAL_SAME_MONTH_RE.match(date.strip())
    if same_month:
        month = same_month.group("month")
        start = _parse_month_day(
            month,
            int(same_month.group("start_day")),
            time=time,
            default=default,
        )
        end = _parse_month_day(
            month,
            int(same_month.group("end_day")),
            time=None,
            default=default,
        )
        if start.value is None or end.value is None:
            return None
        start_dt = start.value
        end_dt = end.value.replace(hour=23, minute=59, second=59, microsecond=0)
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt
        return FestivalRange(
            start=start_dt,
            end=end_dt,
            time_dropped=start.time_dropped,
        )

    return None


def _parse_recurring_list(
    date: str,
    time: str | None,
    *,
    default: datetime,
) -> tuple[ParsedEventDatetime, ...] | None:
    """Parse comma-separated date lists such as ``July 2, July 9, July 15``."""
    if "," not in date:
        return None

    parts = [part.strip() for part in date.split(",") if part.strip()]
    if len(parts) < 2:
        return None

    if not all(re.match(r"^[A-Za-z]+\s+\d", part) for part in parts):
        return None

    parsed_parts: list[ParsedEventDatetime] = []
    for part in parts:
        parsed = parse_event_datetime(part, time, default=default)
        if parsed.value is None:
            return None
        parsed_parts.append(parsed)
    return tuple(parsed_parts)


def _parse_weekday_series(
    date: str,
    time: str | None,
    *,
    default: datetime,
) -> tuple[ParsedEventDatetime, ...] | None:
    """Expand weekday series such as ``Tuesdays, July 7 through September 15``."""
    match = _WEEKDAY_SERIES_RE.match(date.strip())
    if match is None:
        return None

    weekday_key = match.group("weekday").lower()
    weekday_index = _WEEKDAY_TO_INDEX.get(weekday_key)
    if weekday_index is None:
        return None

    noon_default = default.replace(hour=12, minute=0, second=0, microsecond=0)
    range_start = _try_parse_datetime(
        match.group("start").strip(), default=noon_default
    )
    range_end = _try_parse_datetime(match.group("end").strip(), default=noon_default)
    if range_start is None or range_end is None:
        return None

    if range_end < range_start:
        range_start, range_end = range_end, range_start

    occurrences: list[ParsedEventDatetime] = []
    cursor = range_start
    while cursor.date() <= range_end.date():
        if cursor.weekday() == weekday_index:
            if time and time.strip():
                parsed = parse_event_datetime(
                    cursor.strftime("%B %d"),
                    time,
                    default=cursor,
                )
            else:
                parsed = ParsedEventDatetime(cursor)
            if parsed.value is not None:
                occurrences.append(parsed)
        cursor += timedelta(days=1)

    return tuple(occurrences)


def expand_candidate_schedule(
    date: str | None,
    time: str | None,
    *,
    default: datetime,
) -> ExpandedSchedule:
    """Classify and expand multi-day or recurring candidate date strings."""
    date_part = date.strip() if date else ""
    if not date_part:
        single = parse_event_datetime(date, time, default=default)
        return ExpandedSchedule(
            singles=(single,),
            unparseable=single.value is None,
        )

    festival = _parse_festival_range(date_part, time, default=default)
    if festival is not None:
        return ExpandedSchedule(festivals=(festival,))

    weekday_series = _parse_weekday_series(date_part, time, default=default)
    if weekday_series is not None:
        return ExpandedSchedule(occurrences=weekday_series)

    recurring_list = _parse_recurring_list(date_part, time, default=default)
    if recurring_list is not None:
        return ExpandedSchedule(occurrences=recurring_list)

    single = parse_event_datetime(date_part, time, default=default)
    return ExpandedSchedule(singles=(single,), unparseable=single.value is None)


def event_range_overlaps_window(
    start_datetime: datetime,
    end_datetime: datetime,
    *,
    now: datetime | None = None,
    window_days: int = NORMALIZATION_WINDOW_DAYS,
) -> bool:
    """Return True when ``[start, end]`` intersects the normalization window."""
    current = (now or _utc_now()).astimezone(timezone.utc)
    window_end = current + timedelta(days=window_days)
    start = start_datetime.astimezone(timezone.utc)
    end = end_datetime.astimezone(timezone.utc)
    if end < start:
        start, end = end, start
    return start <= window_end and end >= current


def parse_price(price: str | None) -> tuple[int | None, bool]:
    """Parse a price string into ``(price_cents, is_free)``."""
    if price is None or not price.strip():
        return None, False

    normalized = price.strip().lower()
    if normalized in _FREE_PRICE_TOKENS or "donation" in normalized:
        return None, True

    match = _PRICE_DOLLARS.search(price)
    if match is None:
        return None, False

    dollars = float(match.group(1))
    return int(round(dollars * 100)), False


def is_within_normalization_window(
    start_datetime: datetime,
    *,
    now: datetime | None = None,
    window_days: int = NORMALIZATION_WINDOW_DAYS,
) -> bool:
    """Return True when ``start_datetime`` falls within the next ``window_days``."""
    current = (now or _utc_now()).astimezone(timezone.utc)
    window_end = current + timedelta(days=window_days)
    start = start_datetime.astimezone(timezone.utc)
    return current <= start <= window_end


def _feed_quality_scores() -> dict[str, float]:
    return {feed.id: feed.source_quality_score for feed in load_feed_configs()}


def _candidate_discard_data(candidate: EventCandidate, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": candidate.title,
        "source_feed": candidate.source_feed,
        "url": candidate.url,
    }
    payload.update(extra)
    return payload


def _append_schedule_prose(
    description: str,
    *,
    date: str | None,
    time: str | None,
) -> str:
    """Preserve original schedule prose when expanding multi-day listings."""
    if not date or not date.strip():
        return description

    schedule = date.strip()
    if time and time.strip():
        schedule = f"{schedule} {time.strip()}"

    if schedule in description:
        return description

    prefix = f"Schedule: {schedule}"
    if description.strip():
        return f"{description.strip()}\n\n{prefix}"
    return prefix


def _build_normalized_event(
    candidate: EventCandidate,
    *,
    start_datetime: datetime,
    end_datetime: datetime | None,
    id_date: str,
    run_id: str,
    feed_quality_scores: dict[str, float],
    reference: datetime,
) -> NormalizedEvent | None:
    """Build one normalized event after shared field validation."""
    normalized_venue = normalize_venue_name(candidate.venue)
    if normalized_venue is None:
        return None

    if not is_valid_url(candidate.url):
        return None

    event_id = compute_normalized_event_id(
        candidate.title,
        id_date,
        normalized_venue,
    )
    price_cents, is_free = parse_price(candidate.price)
    description = _append_schedule_prose(
        candidate.description or candidate.title,
        date=candidate.date,
        time=candidate.time,
    )
    source_quality_score = feed_quality_scores.get(candidate.source_feed, 0.5)

    return NormalizedEvent(
        id=event_id,
        title=candidate.title.strip(),
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        venue=normalized_venue,
        neighborhood=candidate.neighborhood,
        city=candidate.city.strip(),
        url=candidate.url.strip(),
        price_cents=price_cents,
        is_free=is_free,
        description=description.strip(),
        categories=standardize_categories(candidate.categories),
        source_feeds=[candidate.source_feed],
        source_count=1,
        best_source_feed=candidate.source_feed,
        source_quality_score=source_quality_score,
        run_id=run_id,
        normalized_at=reference,
    )


def normalize_candidate(
    candidate: EventCandidate,
    *,
    run_id: str,
    feed_quality_scores: dict[str, float],
    now: datetime | None = None,
) -> NormalizationResult:
    """Normalize a single candidate or return a discard reason when filtered."""
    reference = (now or _utc_now()).astimezone(timezone.utc)
    default_dt = reference.replace(hour=12, minute=0, second=0, microsecond=0)

    schedule = expand_candidate_schedule(
        candidate.date,
        candidate.time,
        default=default_dt,
    )
    if schedule.unparseable:
        return NormalizationResult(
            discard_reason=DISCARD_UNPARSEABLE_DATE,
            discard_data=_candidate_discard_data(
                candidate,
                date=candidate.date,
                time=candidate.time,
            ),
        )

    normalized_venue = normalize_venue_name(candidate.venue)
    if normalized_venue is None:
        return NormalizationResult(
            discard_reason=DISCARD_MISSING_VENUE,
            discard_data=_candidate_discard_data(candidate),
        )

    if not is_valid_url(candidate.url):
        return NormalizationResult(
            discard_reason=DISCARD_INVALID_URL,
            discard_data=_candidate_discard_data(candidate),
        )

    events: list[NormalizedEvent] = []
    time_dropped = False

    for festival in schedule.festivals:
        if not event_range_overlaps_window(
            festival.start,
            festival.end,
            now=reference,
        ):
            continue
        id_date = candidate.date or festival.start.strftime("%Y-%m-%d")
        event = _build_normalized_event(
            candidate,
            start_datetime=festival.start,
            end_datetime=festival.end,
            id_date=id_date,
            run_id=run_id,
            feed_quality_scores=feed_quality_scores,
            reference=reference,
        )
        if event is not None:
            events.append(event)
            time_dropped = time_dropped or festival.time_dropped

    in_window_occurrences: list[ParsedEventDatetime] = []
    for occurrence in schedule.occurrences:
        if occurrence.value is None:
            continue
        if is_within_normalization_window(occurrence.value, now=reference):
            in_window_occurrences.append(occurrence)

    for occurrence in in_window_occurrences[:MAX_RECURRING_OCCURRENCES]:
        start_dt = occurrence.value
        if start_dt is None:
            continue
        id_date = start_dt.strftime("%Y-%m-%d")
        event = _build_normalized_event(
            candidate,
            start_datetime=start_dt,
            end_datetime=None,
            id_date=id_date,
            run_id=run_id,
            feed_quality_scores=feed_quality_scores,
            reference=reference,
        )
        if event is not None:
            events.append(event)
            time_dropped = time_dropped or occurrence.time_dropped

    for single in schedule.singles:
        if single.value is None:
            continue
        if not is_within_normalization_window(single.value, now=reference):
            continue
        id_date = candidate.date or single.value.strftime("%Y-%m-%d")
        event = _build_normalized_event(
            candidate,
            start_datetime=single.value,
            end_datetime=None,
            id_date=id_date,
            run_id=run_id,
            feed_quality_scores=feed_quality_scores,
            reference=reference,
        )
        if event is not None:
            events.append(event)
            time_dropped = time_dropped or single.time_dropped

    if not events:
        return NormalizationResult(
            discard_reason=DISCARD_OUTSIDE_WINDOW,
            discard_data=_candidate_discard_data(
                candidate,
                date=candidate.date,
                time=candidate.time,
            ),
        )

    return NormalizationResult(tuple(events), time_dropped=time_dropped)


async def run(candidates: list[EventCandidate], run_id: str) -> list[NormalizedEvent]:
    """Normalize event candidates into ``NormalizedEvent`` records.

    Parameters
    ----------
    candidates : list[EventCandidate]
        Extraction agent output.
    run_id : str
        Pipeline run identifier for logging and provenance.

    Returns
    -------
    list[NormalizedEvent]
        Valid normalized events within the coming 7 days.
    """
    logger = get_logger("event_normalization", run_id=run_id)
    feed_quality_scores = _feed_quality_scores()
    normalized_events: list[NormalizedEvent] = []
    discards = NormalizationDiscardCollector()
    reference = _utc_now()

    for candidate in candidates:
        try:
            result = normalize_candidate(
                candidate,
                run_id=run_id,
                feed_quality_scores=feed_quality_scores,
                now=reference,
            )
        except Exception as exc:
            discards.record(
                DISCARD_NORMALIZATION_ERROR,
                _candidate_discard_data(candidate, error=str(exc)),
            )
            continue

        if not result.events:
            if result.discard_reason and result.discard_data:
                discards.record(result.discard_reason, result.discard_data)
            continue

        if result.time_dropped:
            logger.debug(
                "Dropped unparseable time; using date-only fallback",
                data={
                    "title": candidate.title,
                    "source_feed": candidate.source_feed,
                    "date": candidate.date,
                    "time": candidate.time,
                },
            )

        for event in result.events:
            normalized_events.append(event)
            logger.debug(
                "Normalized event: %s",
                event.title,
                data={
                    "event_id": event.id,
                    "source_feed": candidate.source_feed,
                    "start_datetime": event.start_datetime.isoformat(),
                    "end_datetime": (
                        event.end_datetime.isoformat() if event.end_datetime else None
                    ),
                },
            )

    discards.emit(logger)

    logger.info(
        "Event normalization complete",
        data={
            "candidates_processed": len(candidates),
            "events_returned": len(normalized_events),
            "discarded": discards.total,
        },
    )
    return normalized_events
