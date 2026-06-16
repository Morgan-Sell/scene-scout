"""
Recommendation Curator Agent (Allegra)

Responsibility
--------------
Select the final weekly top 10 from ranked events, applying diversity rules,
recommendation-history safety checks, and wildcard slot assignment.

Design
------
Inputs  : list[RankedEvent], UserProfile, run_id: str
Outputs : CuratorResult with up to 10 CuratedRecommendation records
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from scene_scout.curator_config import (
    CURATOR_MAX_PER_CATEGORY,
    CURATOR_MAX_PER_VENUE,
    CURATOR_MAX_RECOMMENDATIONS,
    CURATOR_MAX_WILDCARD_SLOTS,
    CURATOR_MIN_DISTINCT_DATES,
    CURATOR_MIN_WILDCARD_SLOTS,
    CuratorConfig,
    load_curator_config,
)
from scene_scout.logging import get_logger
from scene_scout.models.curated import CuratedRecommendation, CuratorResult
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.ranking import RankedEvent
from scene_scout.services import history as history_service
from scene_scout.services.feedback import generate_feedback_token

if TYPE_CHECKING:
    from scene_scout.models.user import UserProfile

_UNCATEGORIZED = "uncategorized"
_UNKNOWN_VENUE = "unknown"


def _normalize_venue(venue: str) -> str:
    normalized = " ".join(venue.lower().split())
    return normalized or _UNKNOWN_VENUE


def _event_categories(event: EnrichedEvent) -> list[str]:
    if event.categories:
        return [category.lower() for category in event.categories]
    return [_UNCATEGORIZED]


def _event_date(event: EnrichedEvent) -> date:
    return event.start_datetime.astimezone(timezone.utc).date()


def _urgency_note(ranked_event: RankedEvent) -> str | None:
    if ranked_event.sellout_risk == "high":
        return "Tickets may sell out quickly."
    return None


def _category_counts(selected: list[RankedEvent]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for ranked_event in selected:
        for category in _event_categories(ranked_event.event):
            counts[category] += 1
    return counts


def _venue_counts(selected: list[RankedEvent]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for ranked_event in selected:
        counts[_normalize_venue(ranked_event.event.venue)] += 1
    return counts


def _distinct_dates(selected: list[RankedEvent]) -> set[date]:
    return {_event_date(ranked_event.event) for ranked_event in selected}


def _would_violate_limits(
    selected: list[RankedEvent],
    candidate: RankedEvent,
) -> bool:
    categories = _event_categories(candidate.event)
    venue = _normalize_venue(candidate.event.venue)
    category_counts = _category_counts(selected)
    venue_counts = _venue_counts(selected)

    if any(
        category_counts[category] >= CURATOR_MAX_PER_CATEGORY for category in categories
    ):
        return True
    if venue_counts[venue] >= CURATOR_MAX_PER_VENUE:
        return True
    return False


def _can_add(selected: list[RankedEvent], candidate: RankedEvent) -> bool:
    if any(item.event.id == candidate.event.id for item in selected):
        return False
    return not _would_violate_limits(selected, candidate)


def _add_selection(
    selected: list[RankedEvent],
    candidate: RankedEvent,
) -> None:
    selected.append(candidate)


def _try_swap_for_date_diversity(
    selected: list[RankedEvent],
    pool: list[RankedEvent],
) -> None:
    if len(_distinct_dates(selected)) >= CURATOR_MIN_DISTINCT_DATES:
        return
    if len(selected) < 2:
        return

    selected_dates = _distinct_dates(selected)
    replacement_candidates = [
        candidate
        for candidate in pool
        if candidate not in selected
        and _event_date(candidate.event) not in selected_dates
        and not candidate.wildcard_slot
    ]
    if not replacement_candidates:
        return

    replacement_candidates.sort(key=lambda item: item.score, reverse=True)
    removable = sorted(
        (item for item in selected if not item.wildcard_slot),
        key=lambda item: item.score,
    )
    if not removable:
        return

    for remove_target in removable:
        remaining = [item for item in selected if item is not remove_target]
        for candidate in replacement_candidates:
            if _would_violate_limits(remaining, candidate):
                continue
            selected.remove(remove_target)
            selected.append(candidate)
            return


def _ensure_wildcard_slots(
    selected: list[RankedEvent],
    pool: list[RankedEvent],
) -> None:
    wildcard_selected = sum(1 for item in selected if item.wildcard_slot)
    if wildcard_selected >= CURATOR_MIN_WILDCARD_SLOTS:
        return

    wildcard_pool = [
        candidate
        for candidate in pool
        if candidate.wildcard_slot and candidate not in selected
    ]
    if not wildcard_pool:
        return

    wildcard_pool.sort(key=lambda item: item.score, reverse=True)
    for candidate in wildcard_pool:
        if wildcard_selected >= CURATOR_MIN_WILDCARD_SLOTS:
            break
        if len(selected) < CURATOR_MAX_RECOMMENDATIONS:
            if _can_add(selected, candidate):
                _add_selection(selected, candidate)
                wildcard_selected += 1
            continue

        removable = sorted(
            (item for item in selected if not item.wildcard_slot),
            key=lambda item: item.score,
        )
        for remove_target in removable:
            remaining = [item for item in selected if item is not remove_target]
            if _would_violate_limits(remaining, candidate):
                continue
            selected.remove(remove_target)
            selected.append(candidate)
            wildcard_selected += 1
            break


def select_recommendations(
    ranked_events: list[RankedEvent],
    *,
    hard_exclude_ids: set[str] | None = None,
    max_count: int = CURATOR_MAX_RECOMMENDATIONS,
) -> list[RankedEvent]:
    """Return up to ``max_count`` ranked events satisfying diversity rules."""
    exclude_ids = hard_exclude_ids or set()
    pool = [event for event in ranked_events if event.event.id not in exclude_ids]
    selected: list[RankedEvent] = []

    wildcard_candidates = [event for event in pool if event.wildcard_slot]
    wildcard_candidates.sort(key=lambda item: item.score, reverse=True)
    for candidate in wildcard_candidates[:CURATOR_MAX_WILDCARD_SLOTS]:
        if len(selected) >= max_count:
            break
        if _can_add(selected, candidate):
            _add_selection(selected, candidate)

    for candidate in pool:
        if len(selected) >= max_count:
            break
        if candidate in selected:
            continue
        if _can_add(selected, candidate):
            _add_selection(selected, candidate)

    _try_swap_for_date_diversity(selected, pool)
    _ensure_wildcard_slots(selected, pool)

    selected.sort(key=lambda item: item.score, reverse=True)
    return selected


def _wildcard_flags(selected: list[RankedEvent]) -> dict[str, bool]:
    wildcard_ids = {
        ranked_event.event.id for ranked_event in selected if ranked_event.wildcard_slot
    }
    if not wildcard_ids:
        return {ranked_event.event.id: False for ranked_event in selected}

    ordered_wildcards = sorted(
        (event for event in selected if event.event.id in wildcard_ids),
        key=lambda item: item.score,
        reverse=True,
    )
    chosen = {
        event.event.id for event in ordered_wildcards[:CURATOR_MAX_WILDCARD_SLOTS]
    }
    return {
        ranked_event.event.id: ranked_event.event.id in chosen
        for ranked_event in selected
    }


def build_curated_recommendations(
    selected: list[RankedEvent],
    *,
    run_id: str,
    now: datetime,
) -> list[CuratedRecommendation]:
    """Map selected ranked events to curated recommendations with tokens."""
    flags = _wildcard_flags(selected)
    recommendations: list[CuratedRecommendation] = []

    for rank, ranked_event in enumerate(selected, start=1):
        event = ranked_event.event
        recommendations.append(
            CuratedRecommendation(
                rank=rank,
                event=event,
                score=ranked_event.score,
                score_breakdown=ranked_event.score_breakdown,
                explanation=ranked_event.explanation,
                neighborhood_context=event.neighborhood_context,
                sellout_risk=ranked_event.sellout_risk or "low",
                sellout_urgency_note=_urgency_note(ranked_event),
                feedback_token=generate_feedback_token(),
                is_wildcard=flags[event.id],
                run_id=run_id,
                recommended_at=now,
            )
        )

    return recommendations


async def run(
    ranked_events: list[RankedEvent],
    profile: UserProfile,
    run_id: str,
    *,
    curator_config: CuratorConfig | None = None,
    now: datetime | None = None,
) -> CuratorResult:
    """Select the final weekly recommendations and attach curator metadata.

    Parameters
    ----------
    ranked_events : list[RankedEvent]
        Score-sorted events from Sell-Out Risk (or Ranking when risk is skipped).
    profile : UserProfile
        User taste profile (reserved for future editorial constraints).
    run_id : str
        Pipeline run identifier for logging.
    curator_config : CuratorConfig, optional
        Curator persona settings. Loaded from ``curator_voice.txt`` when omitted.
    now : datetime, optional
        Reference time for history checks and ``recommended_at``. Defaults to UTC now.

    Returns
    -------
    CuratorResult
        Final recommendations, sub-10 flag, and Allegra voice brief.
    """
    _ = profile
    logger = get_logger("recommendation_curator", run_id=run_id)
    config = curator_config or load_curator_config()
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    hard_exclude_ids = history_service.get_hard_exclude_event_ids(now=reference)
    excluded = [event for event in ranked_events if event.event.id in hard_exclude_ids]
    if excluded:
        logger.info(
            "Excluded recently recommended events from curation",
            data={
                "excluded_count": len(excluded),
                "excluded_event_ids": [event.event.id for event in excluded],
            },
        )

    selected = select_recommendations(
        ranked_events,
        hard_exclude_ids=hard_exclude_ids,
    )
    recommendations = build_curated_recommendations(
        selected,
        run_id=run_id,
        now=reference,
    )
    below_minimum = len(recommendations) < CURATOR_MAX_RECOMMENDATIONS

    category_totals = _category_counts(selected)
    venue_totals = _venue_counts(selected)
    logger.info(
        "Curation complete",
        data={
            "input_events": len(ranked_events),
            "selected_events": len(recommendations),
            "below_minimum": below_minimum,
            "wildcard_slots": sum(1 for item in recommendations if item.is_wildcard),
            "distinct_dates": len(_distinct_dates(selected)),
            "category_totals": dict(category_totals),
            "venue_totals": dict(venue_totals),
            "curator_name": config.name,
            "voice_brief_loaded": bool(config.voice_brief),
        },
    )

    return CuratorResult(
        recommendations=recommendations,
        below_minimum=below_minimum,
        curator_config=config,
    )
