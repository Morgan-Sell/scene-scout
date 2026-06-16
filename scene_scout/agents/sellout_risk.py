"""
Sell-Out Risk Agent

Responsibility
--------------
Assign a heuristic sell-out risk band to every ranked event using venue size,
price, date proximity, description language, and performer demand signals.

Design
------
Inputs  : list[RankedEvent], run_id: str
Outputs : list[RankedEvent] with ``sellout_risk`` populated
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from scene_scout.logging import get_logger
from scene_scout.models.ranking import RankedEvent
from scene_scout.sellout_risk_config import (
    DAYS_FAR,
    DAYS_NEAR,
    DAYS_SOON,
    DAYS_VERY_SOON,
    HIGH_URGENCY_PHRASES,
    LARGE_VENUE_TOKENS,
    LOW_URGENCY_PHRASES,
    MEDIUM_VENUE_TOKENS,
    PRICE_LOW_CENTS,
    PRICE_MID_CENTS,
    RISK_THRESHOLD_HIGH,
    RISK_THRESHOLD_MEDIUM,
    SMALL_VENUE_TOKENS,
    WEIGHT_DATE_PROXIMITY,
    WEIGHT_DESCRIPTION_LANGUAGE,
    WEIGHT_PERFORMER_AFFINITY,
    WEIGHT_PRICE,
    WEIGHT_VENUE_SIZE,
)

SelloutRisk = Literal["low", "medium", "high"]
VenueSizeCategory = Literal["small", "medium", "large", "unknown"]

_VENUE_SIZE_SCORES: dict[VenueSizeCategory, float] = {
    "small": 0.90,
    "medium": 0.55,
    "large": 0.25,
    "unknown": 0.50,
}


def _clamp_unit_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def classify_venue_size_category(venue: str) -> VenueSizeCategory:
    """Return a coarse venue capacity category inferred from the venue name."""
    normalized = _normalize_text(venue)
    if not normalized:
        return "unknown"

    for token in LARGE_VENUE_TOKENS:
        if token in normalized:
            return "large"
    for token in SMALL_VENUE_TOKENS:
        if token in normalized:
            return "small"
    for token in MEDIUM_VENUE_TOKENS:
        if token in normalized:
            return "medium"
    return "unknown"


def score_venue_size(venue: str) -> float:
    """Return sell-out pressure from venue capacity category."""
    return _VENUE_SIZE_SCORES[classify_venue_size_category(venue)]


def score_price(*, price_cents: int | None, is_free: bool) -> float:
    """Return sell-out pressure from ticket price signals."""
    if is_free:
        return 0.85
    if price_cents is None:
        return 0.50
    if price_cents <= 0:
        return 0.85
    if price_cents < PRICE_LOW_CENTS:
        return 0.75
    if price_cents < PRICE_MID_CENTS:
        return 0.55
    return 0.35


def score_date_proximity(
    start_datetime: datetime,
    *,
    now: datetime | None = None,
) -> float:
    """Return sell-out pressure from how soon the event starts."""
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    event_time = start_datetime.astimezone(timezone.utc)
    days_until = (event_time - reference).total_seconds() / 86_400

    if days_until < 0:
        return 0.20
    if days_until <= DAYS_VERY_SOON:
        return 0.95
    if days_until <= DAYS_SOON:
        return 0.75
    if days_until <= DAYS_NEAR:
        return 0.55
    if days_until <= DAYS_FAR:
        return 0.40
    return 0.25


def score_description_language(description: str) -> float:
    """Return sell-out pressure from urgency language in the description."""
    normalized = _normalize_text(description)
    if not normalized:
        return 0.50

    high_hits = sum(1 for phrase in HIGH_URGENCY_PHRASES if phrase in normalized)
    low_hits = sum(1 for phrase in LOW_URGENCY_PHRASES if phrase in normalized)

    if high_hits == 0 and low_hits == 0:
        return 0.50
    if high_hits >= low_hits:
        return _clamp_unit_score(0.55 + 0.15 * high_hits)
    return _clamp_unit_score(0.45 - 0.10 * low_hits)


def score_performer_demand(top_performer_affinity: float) -> float:
    """Return sell-out pressure from headliner/performer draw."""
    return _clamp_unit_score(top_performer_affinity)


def composite_risk_score(
    *,
    venue: str,
    price_cents: int | None,
    is_free: bool,
    start_datetime: datetime,
    description: str,
    top_performer_affinity: float,
    now: datetime | None = None,
) -> float:
    """Return the weighted composite sell-out pressure score."""
    total = (
        WEIGHT_VENUE_SIZE * score_venue_size(venue)
        + WEIGHT_PRICE * score_price(price_cents=price_cents, is_free=is_free)
        + WEIGHT_DATE_PROXIMITY * score_date_proximity(start_datetime, now=now)
        + WEIGHT_DESCRIPTION_LANGUAGE * score_description_language(description)
        + WEIGHT_PERFORMER_AFFINITY * score_performer_demand(top_performer_affinity)
    )
    return _clamp_unit_score(total)


def classify_risk(score: float) -> SelloutRisk:
    """Map a composite score to ``low``, ``medium``, or ``high``."""
    if score >= RISK_THRESHOLD_HIGH:
        return "high"
    if score >= RISK_THRESHOLD_MEDIUM:
        return "medium"
    return "low"


def classify_event_risk(
    ranked_event: RankedEvent,
    *,
    now: datetime | None = None,
) -> SelloutRisk:
    """Classify a single ranked event."""
    event = ranked_event.event
    score = composite_risk_score(
        venue=event.venue,
        price_cents=event.price_cents,
        is_free=event.is_free,
        start_datetime=event.start_datetime,
        description=event.description,
        top_performer_affinity=event.top_performer_affinity,
        now=now,
    )
    return classify_risk(score)


def compute_risk_distribution(events: list[RankedEvent]) -> dict[str, int]:
    """Count sell-out risk bands across ranked events."""
    distribution = {"low": 0, "medium": 0, "high": 0}
    for event in events:
        if event.sellout_risk is None:
            continue
        distribution[event.sellout_risk] += 1
    return distribution


async def run(
    events: list[RankedEvent],
    run_id: str,
    *,
    now: datetime | None = None,
) -> list[RankedEvent]:
    """Assign sell-out risk to every ranked event and log the distribution.

    Parameters
    ----------
    events : list[RankedEvent]
        Ranked events from the Ranking Agent.
    run_id : str
        Pipeline run identifier for logging.
    now : datetime, optional
        Reference time for date-proximity scoring. Defaults to current UTC time.

    Returns
    -------
    list[RankedEvent]
        Input events with ``sellout_risk`` populated.
    """
    logger = get_logger("sellout_risk", run_id=run_id)
    scored: list[RankedEvent] = []

    for ranked_event in events:
        risk = classify_event_risk(ranked_event, now=now)
        updated = ranked_event.model_copy(update={"sellout_risk": risk})
        scored.append(updated)
        logger.info(
            "Classified sell-out risk",
            data={
                "event_id": ranked_event.event.id,
                "event_title": ranked_event.event.title,
                "sellout_risk": risk,
            },
        )

    distribution = compute_risk_distribution(scored)
    logger.info("Sell-out risk distribution", data={"distribution": distribution})
    return scored
