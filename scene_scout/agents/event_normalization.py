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
    NORMALIZATION_DISCARD_JSONL_SAMPLE_SIZE,
    NORMALIZATION_DISCARD_TERMINAL_SAMPLE_SIZE,
    NORMALIZATION_WINDOW_DAYS,
)

_TRAILING_PUNCTUATION = re.compile(r"[.,;:!?]+$")
_WHITESPACE = re.compile(r"\s+")
_PRICE_DOLLARS = re.compile(r"\$(\d+(?:\.\d{2})?)")
_FREE_PRICE_TOKENS = frozenset({"free", "no cover", "gratis", "complimentary"})

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

    event: NormalizedEvent | None
    discard_reason: str | None = None
    discard_data: dict[str, Any] | None = None
    time_dropped: bool = False


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

    parse_result = parse_event_datetime(
        candidate.date,
        candidate.time,
        default=default_dt,
    )
    if parse_result.value is None:
        return NormalizationResult(
            None,
            discard_reason=DISCARD_UNPARSEABLE_DATE,
            discard_data=_candidate_discard_data(
                candidate,
                date=candidate.date,
                time=candidate.time,
            ),
        )

    start_datetime = parse_result.value

    if not is_within_normalization_window(start_datetime, now=reference):
        return NormalizationResult(
            None,
            discard_reason=DISCARD_OUTSIDE_WINDOW,
            discard_data=_candidate_discard_data(
                candidate,
                date=candidate.date,
                time=candidate.time,
            ),
        )

    normalized_venue = normalize_venue_name(candidate.venue)
    if normalized_venue is None:
        return NormalizationResult(
            None,
            discard_reason=DISCARD_MISSING_VENUE,
            discard_data=_candidate_discard_data(candidate),
        )

    if not is_valid_url(candidate.url):
        return NormalizationResult(
            None,
            discard_reason=DISCARD_INVALID_URL,
            discard_data=_candidate_discard_data(candidate),
        )

    id_date = candidate.date or start_datetime.strftime("%Y-%m-%d")
    event_id = compute_normalized_event_id(
        candidate.title,
        id_date,
        normalized_venue,
    )

    price_cents, is_free = parse_price(candidate.price)
    description = candidate.description or candidate.title
    source_quality_score = feed_quality_scores.get(candidate.source_feed, 0.5)

    event = NormalizedEvent(
        id=event_id,
        title=candidate.title.strip(),
        start_datetime=start_datetime,
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
    return NormalizationResult(event, time_dropped=parse_result.time_dropped)


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

        if result.event is None:
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
                    "start_datetime": result.event.start_datetime.isoformat(),
                },
            )

        normalized_events.append(result.event)
        logger.debug(
            "Normalized event: %s",
            result.event.title,
            data={
                "event_id": result.event.id,
                "source_feed": candidate.source_feed,
                "start_datetime": result.event.start_datetime.isoformat(),
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
