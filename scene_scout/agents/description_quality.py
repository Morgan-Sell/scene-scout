"""
Description Quality Agent

Responsibility
--------------
Score each ``NormalizedEvent`` with a deterministic rubric and populate
``description_quality_score`` and ``low_information``.

Design
------
Inputs  : list[NormalizedEvent], run_id: str
Outputs : list[NormalizedEvent] with quality fields populated
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scene_scout import config
from scene_scout.agents.event_normalization import is_valid_url
from scene_scout.description_quality_config import (
    GENERIC_CATEGORY_NAMES,
    GENERIC_PERFORMER_PHRASES,
    GENERIC_VENUE_NAMES,
    WEIGHT_CATEGORY_COVERAGE,
    WEIGHT_DATE_TIME_PRESENT,
    WEIGHT_DESCRIPTION_LENGTH,
    WEIGHT_PERFORMER_NAMED,
    WEIGHT_PRICE_CLARITY,
    WEIGHT_URL_VALIDITY,
    WEIGHT_VENUE_PRESENCE,
)
from scene_scout.logging import get_logger
from scene_scout.models.event import NormalizedEvent

_PERFORMER_PATTERNS = (
    re.compile(
        r"\b(?:featuring|feat\.?|starring|presents|presented by)\s+"
        r"([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwith\s+(?:special guest\s+)?([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)",
        re.IGNORECASE,
    ),
    re.compile(r"\bDJ\s+([A-Z][\w'-]+)", re.IGNORECASE),
)


@dataclass(frozen=True)
class DescriptionQualitySignals:
    """Per-signal rubric scores, each in the range 0.0–1.0."""

    description_length: float
    venue_presence: float
    date_time_present: float
    performer_named: float
    category_coverage: float
    url_validity: float
    price_clarity: float

    def composite_score(self) -> float:
        """Return the weighted rubric score."""
        return round(
            WEIGHT_DESCRIPTION_LENGTH * self.description_length
            + WEIGHT_VENUE_PRESENCE * self.venue_presence
            + WEIGHT_DATE_TIME_PRESENT * self.date_time_present
            + WEIGHT_PERFORMER_NAMED * self.performer_named
            + WEIGHT_CATEGORY_COVERAGE * self.category_coverage
            + WEIGHT_URL_VALIDITY * self.url_validity
            + WEIGHT_PRICE_CLARITY * self.price_clarity,
            4,
        )


def score_description_length(description: str) -> float:
    """Score description length per rubric tiers."""
    length = len(description.strip())
    if length == 0:
        return 0.0
    if length < 50:
        return 0.3
    if length < 150:
        return 0.7
    return 1.0


def score_venue_presence(venue: str | None) -> float:
    """Score venue specificity."""
    if venue is None or not venue.strip():
        return 0.0
    if venue.strip().lower() in GENERIC_VENUE_NAMES:
        return 0.0
    return 1.0


def score_date_time_present(event: NormalizedEvent) -> float:
    """Score whether both date and time are present on the record."""
    start = event.start_datetime
    if start is None:
        return 0.0
    if event.end_datetime is not None:
        return 1.0
    if start.hour == 0 and start.minute == 0 and start.second == 0:
        return 0.5
    return 1.0


def _performer_match_is_specific(name: str) -> bool:
    normalized = name.strip().lower()
    if not normalized:
        return False
    if normalized in GENERIC_PERFORMER_PHRASES:
        return False
    return not any(
        normalized == phrase or normalized.startswith(f"{phrase} ")
        for phrase in GENERIC_PERFORMER_PHRASES
    )


def has_named_performer(title: str, description: str) -> bool:
    """Return True when a specific performer is named in title or description."""
    combined = f"{title}\n{description}"
    for pattern in _PERFORMER_PATTERNS:
        match = pattern.search(combined)
        if match and _performer_match_is_specific(match.group(1)):
            return True
    return False


def score_performer_named(title: str, description: str) -> float:
    """Score whether a named performer appears in title or description."""
    return 1.0 if has_named_performer(title, description) else 0.0


def score_category_coverage(categories: list[str]) -> float:
    """Score whether at least one non-generic category is present."""
    for category in categories:
        if category.strip().lower() not in GENERIC_CATEGORY_NAMES:
            return 1.0
    return 0.0


def score_url_validity(url: str) -> float:
    """Score URL format validity."""
    return 1.0 if is_valid_url(url) else 0.0


def score_price_clarity(price_cents: int | None, is_free: bool) -> float:
    """Score whether price information is clear."""
    if is_free or price_cents is not None:
        return 1.0
    return 0.0


def score_event(event: NormalizedEvent) -> DescriptionQualitySignals:
    """Compute all seven rubric signals for an event."""
    return DescriptionQualitySignals(
        description_length=score_description_length(event.description),
        venue_presence=score_venue_presence(event.venue),
        date_time_present=score_date_time_present(event),
        performer_named=score_performer_named(event.title, event.description),
        category_coverage=score_category_coverage(event.categories),
        url_validity=score_url_validity(event.url),
        price_clarity=score_price_clarity(event.price_cents, event.is_free),
    )


def apply_quality_scores(event: NormalizedEvent) -> NormalizedEvent:
    """Populate quality fields on a single event."""
    signals = score_event(event)
    quality_score = signals.composite_score()
    return event.model_copy(
        update={
            "description_quality_score": quality_score,
            "low_information": quality_score < config.DESCRIPTION_QUALITY_THRESHOLD,
        }
    )


async def run(events: list[NormalizedEvent], run_id: str) -> list[NormalizedEvent]:
    """Score description quality for every normalized event.

    Parameters
    ----------
    events : list[NormalizedEvent]
        Deduplicated normalized events.
    run_id : str
        Pipeline run identifier for logging.

    Returns
    -------
    list[NormalizedEvent]
        Events with ``description_quality_score`` and ``low_information`` set.
    """
    logger = get_logger("description_quality", run_id=run_id)
    scored = [apply_quality_scores(event) for event in events]
    low_information_count = sum(1 for event in scored if event.low_information)

    logger.info(
        "Description quality scoring complete",
        data={
            "events_scored": len(scored),
            "low_information_count": low_information_count,
            "threshold": config.DESCRIPTION_QUALITY_THRESHOLD,
        },
    )
    return scored
