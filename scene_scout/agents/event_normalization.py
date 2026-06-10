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
from datetime import datetime, timedelta, timezone
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
    NORMALIZATION_WINDOW_DAYS,
)

_TRAILING_PUNCTUATION = re.compile(r"[.,;:!?]+$")
_WHITESPACE = re.compile(r"\s+")
_PRICE_DOLLARS = re.compile(r"\$(\d+(?:\.\d{2})?)")
_FREE_PRICE_TOKENS = frozenset({"free", "no cover", "gratis", "complimentary"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def parse_event_datetime(
    date: str | None,
    time: str | None,
    *,
    default: datetime | None = None,
) -> datetime | None:
    """Parse candidate date/time strings via ``dateutil``."""
    parts = [part.strip() for part in (date, time) if part and part.strip()]
    if not parts:
        return None

    combined = " ".join(parts)
    try:
        parsed = date_parser.parse(combined, default=default)
    except (ValueError, OverflowError, TypeError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def normalize_candidate(
    candidate: EventCandidate,
    *,
    run_id: str,
    feed_quality_scores: dict[str, float],
    now: datetime | None = None,
) -> NormalizedEvent | None:
    """Normalize a single candidate or return ``None`` when it should be discarded."""
    reference = (now or _utc_now()).astimezone(timezone.utc)
    default_dt = reference.replace(hour=12, minute=0, second=0, microsecond=0)

    start_datetime = parse_event_datetime(
        candidate.date,
        candidate.time,
        default=default_dt,
    )
    if start_datetime is None:
        return None

    if not is_within_normalization_window(start_datetime, now=reference):
        return None

    normalized_venue = normalize_venue_name(candidate.venue)
    if normalized_venue is None:
        return None

    if not is_valid_url(candidate.url):
        return None

    id_date = candidate.date or start_datetime.strftime("%Y-%m-%d")
    event_id = compute_normalized_event_id(
        candidate.title,
        id_date,
        normalized_venue,
    )

    price_cents, is_free = parse_price(candidate.price)
    description = candidate.description or candidate.title
    source_quality_score = feed_quality_scores.get(candidate.source_feed, 0.5)

    return NormalizedEvent(
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
    reference = _utc_now()

    for candidate in candidates:
        try:
            event = normalize_candidate(
                candidate,
                run_id=run_id,
                feed_quality_scores=feed_quality_scores,
                now=reference,
            )
        except Exception as exc:
            logger.warning(
                "Skipping candidate due to normalization error: %s",
                candidate.title,
                data={
                    "source_feed": candidate.source_feed,
                    "url": candidate.url,
                    "error": str(exc),
                },
            )
            continue

        if event is None:
            if (
                parse_event_datetime(
                    candidate.date,
                    candidate.time,
                    default=reference.replace(
                        hour=12, minute=0, second=0, microsecond=0
                    ),
                )
                is None
            ):
                logger.warning(
                    "Discarding candidate with unparseable date: %s",
                    candidate.title,
                    data={
                        "source_feed": candidate.source_feed,
                        "date": candidate.date,
                        "time": candidate.time,
                    },
                )
            elif normalize_venue_name(candidate.venue) is None:
                logger.warning(
                    "Discarding candidate with missing venue: %s",
                    candidate.title,
                    data={"source_feed": candidate.source_feed},
                )
            elif not is_valid_url(candidate.url):
                logger.warning(
                    "Discarding candidate with invalid URL: %s",
                    candidate.title,
                    data={
                        "source_feed": candidate.source_feed,
                        "url": candidate.url,
                    },
                )
            else:
                logger.info(
                    "Discarding candidate outside normalization window: %s",
                    candidate.title,
                    data={
                        "source_feed": candidate.source_feed,
                        "date": candidate.date,
                        "time": candidate.time,
                    },
                )
            continue

        normalized_events.append(event)
        logger.debug(
            "Normalized event: %s",
            event.title,
            data={
                "event_id": event.id,
                "source_feed": candidate.source_feed,
                "start_datetime": event.start_datetime.isoformat(),
            },
        )

    logger.info(
        "Event normalization complete",
        data={
            "candidates_processed": len(candidates),
            "events_returned": len(normalized_events),
        },
    )
    return normalized_events
