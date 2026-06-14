"""
Ranking Agent

Responsibility
--------------
Score enriched events deterministically against a user profile, generate grounded
LLM explanations, and flag wildcard candidates.

Design
------
Inputs  : list[EnrichedEvent], UserProfile, run_id
Outputs : list[RankedEvent] sorted by score descending
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from scene_scout.config import (
    RANKING_COMPONENT_WEIGHTS,
    SOURCE_COVERAGE_MAX,
    vol_history_dir,
)
from scene_scout.logging import get_logger
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.ranking import (
    SCORE_COMPONENT_KEYS,
    RankedEvent,
    RankingExplanationLLMOutput,
)
from scene_scout.models.user import UserProfile
from scene_scout.ranking_config import (
    FALLBACK_EXPLANATION_TEMPLATE,
    NEUTRAL_CATEGORY_FIT,
    NEUTRAL_LOCATION_FIT,
    NEUTRAL_VIBE_FIT,
    NOVELTY_PENALIZED_SCORE,
    NOVELTY_UNSEEN_BONUS,
    RANKING_SOFT_RECENCY_DAYS,
    WILDCARD_MIN_NOVELTY,
    WILDCARD_SCORE_MAX,
    WILDCARD_SCORE_MIN,
    WILDCARD_SLOT_COUNT,
)
from scene_scout.services.chroma import get_liked_events_collection, similarity_score
from scene_scout.services.llm import LLMValidationError, complete
from scene_scout.services.prompt_loader import render_prompt

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection

_SYSTEM_PROMPT = (
    "You are a recommendation explainer for SceneScout. "
    'Return only valid JSON with a single key "explanation".'
)


def _clamp_unit_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def _normalize_label(value: str) -> str:
    return value.strip().lower()


def _category_matches(label: str, candidate: str) -> bool:
    left = _normalize_label(label)
    right = _normalize_label(candidate)
    return left == right or left in right or right in left


def _event_has_excluded_category(event: EnrichedEvent, profile: UserProfile) -> bool:
    excluded = {_normalize_label(category) for category in profile.excluded_categories}
    if not excluded:
        return False
    return any(_normalize_label(category) in excluded for category in event.categories)


def compute_source_coverage(source_count: int) -> float:
    """Return normalized cross-feed coverage in ``[0.0, 1.0]``."""
    if source_count < 1:
        return 0.0
    return _clamp_unit_score(source_count / SOURCE_COVERAGE_MAX)


def compute_category_fit(event: EnrichedEvent, profile: UserProfile) -> float:
    """Return overlap between event categories and profile category weights."""
    if not event.categories:
        return 0.0

    if profile.category_weights:
        matched_weights: list[float] = []
        for category in event.categories:
            for key, weight in profile.category_weights.items():
                if _category_matches(category, key):
                    matched_weights.append(weight)
                    break
        if not matched_weights:
            return 0.0
        return _clamp_unit_score(sum(matched_weights) / len(matched_weights))

    if profile.stated_interests:
        matches = sum(
            1
            for category in event.categories
            if any(
                _category_matches(category, interest)
                for interest in profile.stated_interests
            )
        )
        return _clamp_unit_score(matches / len(event.categories))

    return NEUTRAL_CATEGORY_FIT


def compute_vibe_fit(event: EnrichedEvent, profile: UserProfile) -> float:
    """Return overlap between event vibe tags and profile vibe preferences."""
    if not profile.vibe_preferences:
        return NEUTRAL_VIBE_FIT if event.vibe_tags else 0.0
    if not event.vibe_tags:
        return 0.0

    preferred = set(profile.vibe_preferences)
    overlap = len(set(event.vibe_tags) & preferred)
    return _clamp_unit_score(overlap / len(preferred))


def compute_location_fit(event: EnrichedEvent, profile: UserProfile) -> float:
    """Return neighborhood alignment with the user's preferred areas."""
    if not profile.preferred_neighborhoods:
        return NEUTRAL_LOCATION_FIT

    neighborhood = _normalize_label(event.neighborhood or "")
    if not neighborhood:
        return 0.0

    for preferred in profile.preferred_neighborhoods:
        pref = _normalize_label(preferred)
        if pref in neighborhood or neighborhood in pref:
            return 1.0
    return 0.0


def compute_novelty(
    event: EnrichedEvent,
    profile: UserProfile,
    *,
    previously_recommended_ids: set[str],
) -> tuple[float, bool, bool]:
    """Return novelty score, prior recommendation flag, and penalty flag."""
    is_previously_recommended = event.id in previously_recommended_ids
    penalty_applied = is_previously_recommended
    score = NOVELTY_PENALIZED_SCORE if is_previously_recommended else 1.0

    known_categories = {_normalize_label(key) for key in profile.category_weights}
    known_vibes = set(profile.vibe_preferences)
    unseen_category = any(
        _normalize_label(category) not in known_categories
        for category in event.categories
    )
    unseen_vibe = any(vibe not in known_vibes for vibe in event.vibe_tags)
    if unseen_category or unseen_vibe:
        score = _clamp_unit_score(score + NOVELTY_UNSEEN_BONUS)

    return score, is_previously_recommended, penalty_applied


def compute_score_breakdown(
    event: EnrichedEvent,
    profile: UserProfile,
    *,
    semantic_similarity: float,
    previously_recommended_ids: set[str],
) -> tuple[dict[str, float], bool, bool]:
    """Compute all nine ranking components for an event."""
    novelty, is_previously_recommended, penalty_applied = compute_novelty(
        event,
        profile,
        previously_recommended_ids=previously_recommended_ids,
    )
    breakdown = {
        "category_fit": compute_category_fit(event, profile),
        "vibe_fit": compute_vibe_fit(event, profile),
        "semantic_similarity": _clamp_unit_score(semantic_similarity),
        "performer_affinity": _clamp_unit_score(event.top_performer_affinity),
        "location": compute_location_fit(event, profile),
        "novelty": novelty,
        "source_quality": _clamp_unit_score(event.source_quality_score),
        "source_coverage": compute_source_coverage(event.source_count),
        "description_quality": _clamp_unit_score(event.description_quality_score),
    }
    return breakdown, is_previously_recommended, penalty_applied


def composite_score(breakdown: dict[str, float]) -> float:
    """Return the weighted composite score for a component breakdown."""
    total = sum(
        breakdown[component] * RANKING_COMPONENT_WEIGHTS[component]
        for component in SCORE_COMPONENT_KEYS
    )
    return _clamp_unit_score(total)


def load_previously_recommended_event_ids(
    *,
    now: datetime | None = None,
    recency_days: int = RANKING_SOFT_RECENCY_DAYS,
) -> set[str]:
    """Return event IDs recommended within the soft-recency window."""
    index_path = vol_history_dir() / "hard_exclude_index.json"
    if not index_path.is_file():
        return set()

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=recency_days)
    recommended: set[str] = set()

    for entry in entries:
        event_id = entry.get("event_id")
        recommended_at_raw = entry.get("recommended_at")
        if not event_id or not recommended_at_raw:
            continue
        recommended_at = datetime.fromisoformat(
            str(recommended_at_raw).replace("Z", "+00:00")
        )
        if recommended_at.astimezone(timezone.utc) >= cutoff.astimezone(timezone.utc):
            recommended.add(str(event_id))

    return recommended


def assign_wildcard_slots(ranked_events: list[RankedEvent]) -> list[RankedEvent]:
    """Mark up to ``WILDCARD_SLOT_COUNT`` moderate-fit, high-novelty events."""
    candidates = [
        event
        for event in ranked_events
        if WILDCARD_SCORE_MIN <= event.score <= WILDCARD_SCORE_MAX
        and event.score_breakdown["novelty"] >= WILDCARD_MIN_NOVELTY
    ]
    candidates.sort(
        key=lambda item: item.score_breakdown["novelty"],
        reverse=True,
    )
    wildcard_ids = {event.event.id for event in candidates[:WILDCARD_SLOT_COUNT]}
    if not wildcard_ids:
        return ranked_events

    return [
        event.model_copy(update={"wildcard_slot": event.event.id in wildcard_ids})
        for event in ranked_events
    ]


def fallback_explanation(event: EnrichedEvent) -> str:
    """Return a deterministic explanation when LLM validation fails."""
    return FALLBACK_EXPLANATION_TEMPLATE.format(title=event.title)


async def _generate_explanation(
    event: EnrichedEvent,
    profile: UserProfile,
    *,
    score_breakdown: dict[str, float],
    total_score: float,
    run_id: str,
    logger: Any,
) -> str:
    """Generate an LLM explanation or fall back on validation failure."""
    try:
        llm_output = await complete(
            prompt=render_prompt(
                "ranking_explanation",
                event=event,
                score_breakdown=score_breakdown,
                total_score=total_score,
                stated_interests=profile.stated_interests,
                vibe_preferences=profile.vibe_preferences,
            ),
            system=_SYSTEM_PROMPT,
            response_model=RankingExplanationLLMOutput,
            run_id=run_id,
            agent_name="ranking",
        )
        return llm_output.explanation.strip()
    except LLMValidationError as exc:
        logger.warning(
            "Using fallback ranking explanation for %s",
            event.title,
            data={"event_id": event.id, "error": str(exc)},
        )
        return fallback_explanation(event)


async def run(
    events: list[EnrichedEvent],
    profile: UserProfile,
    run_id: str,
    *,
    chroma_collection: Collection | None = None,
    previously_recommended_ids: set[str] | None = None,
) -> list[RankedEvent]:
    """Rank enriched events and attach grounded explanations.

    Parameters
    ----------
    events : list[EnrichedEvent]
        Candidate events after enrichment.
    profile : UserProfile
        User taste profile for scoring.
    run_id : str
        Pipeline run identifier for logging.
    chroma_collection : Collection, optional
        Liked-events collection for semantic similarity. Defaults to the
        persistent ``vol-chroma`` collection.
    previously_recommended_ids : set[str], optional
        Event IDs within the soft-recency window. Loaded from history when omitted.

    Returns
    -------
    list[RankedEvent]
        Ranked events sorted by score descending.

    Raises
    ------
    scene_scout.services.llm.LLMInfrastructureError
        On unrecoverable LLM provider failures during explanation generation.
    """
    logger = get_logger("ranking", run_id=run_id)
    collection = chroma_collection or get_liked_events_collection()
    recent_ids = (
        previously_recommended_ids
        if previously_recommended_ids is not None
        else load_previously_recommended_event_ids()
    )

    eligible_events = [
        event for event in events if not _event_has_excluded_category(event, profile)
    ]
    skipped = len(events) - len(eligible_events)
    if skipped:
        logger.info(
            "Skipped events with excluded categories",
            data={"skipped_count": skipped},
        )

    ranked: list[RankedEvent] = []
    for event in eligible_events:
        breakdown, is_previously_recommended, penalty_applied = compute_score_breakdown(
            event,
            profile,
            semantic_similarity=similarity_score(event, collection),
            previously_recommended_ids=recent_ids,
        )
        total_score = composite_score(breakdown)
        logger.info(
            "Ranked event",
            data={
                "event_id": event.id,
                "event_title": event.title,
                "score": round(total_score, 4),
                "score_breakdown": {
                    key: round(value, 4) for key, value in breakdown.items()
                },
            },
        )

        explanation = await _generate_explanation(
            event,
            profile,
            score_breakdown=breakdown,
            total_score=total_score,
            run_id=run_id,
            logger=logger,
        )
        ranked.append(
            RankedEvent(
                event=event,
                score=total_score,
                score_breakdown=breakdown,
                explanation=explanation,
                is_previously_recommended=is_previously_recommended,
                novelty_penalty_applied=penalty_applied,
                run_id=run_id,
            )
        )

    ranked.sort(key=lambda item: item.score, reverse=True)
    ranked = assign_wildcard_slots(ranked)

    logger.info(
        "Ranking complete",
        data={
            "input_events": len(events),
            "ranked_events": len(ranked),
            "wildcard_slots": sum(1 for item in ranked if item.wildcard_slot),
        },
    )
    return ranked
