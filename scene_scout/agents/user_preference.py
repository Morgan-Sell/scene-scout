"""
User Preference Agent

Responsibility
--------------
Parse a cold-start onboarding prompt into a structured ``UserProfile``, persist it
to ``vol-profiles/profile.json``, load the current profile for downstream agents,
and apply decay-weighted behavioral feedback updates.

Design
------
Inputs  : name, email, cold-start prompt, run_id; feedback signals (Phase 8.2)
Outputs : ``UserProfile`` written to ``vol-profiles/profile.json``
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from scene_scout.config import vol_profiles_dir
from scene_scout.logging import get_logger
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.feedback import FeedbackEvent
from scene_scout.models.user import UserProfile, UserProfileParseLLMOutput
from scene_scout.services import chroma as chroma_service
from scene_scout.services.llm import complete
from scene_scout.services.prompt_loader import render_prompt
from scene_scout.user_preference_config import (
    FEEDBACK_CLICK_CATEGORY_DELTA,
    FEEDBACK_DECAY_LAMBDA,
    FEEDBACK_DEFAULT_CATEGORY_WEIGHT,
    FEEDBACK_NEGATIVE_CATEGORY_DELTA,
    VIBE_UPDATE_MIN_DECAY_WEIGHT,
)
from scene_scout.vibe_classifier_config import VIBE_VOCABULARY

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection

_PROFILE_FILENAME = "profile.json"

_SYSTEM_PROMPT = (
    "You are a user preference parser for SceneScout. "
    "Return only valid JSON matching the requested schema."
)


class UserProfileNotFoundError(FileNotFoundError):
    """Raised when no persisted user profile exists."""


def profile_path() -> Path:
    """Return the canonical profile file path under ``vol-profiles``."""
    return vol_profiles_dir() / _PROFILE_FILENAME


def _user_id_from_email(email: str) -> str:
    """Return a stable user identifier derived from the onboarding email."""
    normalized = email.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def write_profile(profile: UserProfile) -> Path:
    """Persist a user profile to ``vol-profiles/profile.json``.

    Parameters
    ----------
    profile : UserProfile
        Profile to write.

    Returns
    -------
    Path
        Path to the written profile file.
    """
    path = profile_path()
    path.write_text(
        json.dumps(profile.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_profile() -> UserProfile:
    """Load the current user profile from ``vol-profiles/profile.json``.

    Returns
    -------
    UserProfile
        Persisted user profile.

    Raises
    ------
    UserProfileNotFoundError
        If no profile file exists yet.
    """
    path = profile_path()
    if not path.is_file():
        raise UserProfileNotFoundError(
            f"No user profile found at {path}. "
            "Run user_preference.parse_cold_start() during onboarding first."
        )

    return UserProfile.model_validate_json(path.read_text(encoding="utf-8"))


async def parse_cold_start(
    name: str,
    email: str,
    prompt: str,
    run_id: str,
    *,
    home_city: str,
    horizon_days: int,
) -> UserProfile:
    """Parse a cold-start prompt into a ``UserProfile`` and persist it.

    Calls ``llm.complete()`` with the rendered ``user_preference_parse`` prompt,
    validates the response, merges onboarding metadata, and writes
    ``vol-profiles/profile.json``.

    ``home_city`` and ``horizon_days`` are supplied by onboarding or UAT CLI flags;
    they are not extracted from the taste prompt by the LLM.

    Parameters
    ----------
    name : str
        User display name for email salutation.
    email : str
        User email address copied from onboarding or Modal Secret.
    prompt : str
        Free-text cold-start interests, dislikes, and constraints.
    run_id : str
        Pipeline run identifier for logging.
    home_city : str
        U.S. metro for feed catalog selection (Phase 1C.2).
    horizon_days : int
        Days ahead to include events in normalization windows (Phase 1C.5).

    Returns
    -------
    UserProfile
        Parsed and persisted user profile.

    Raises
    ------
    LLMInfrastructureError
        On API outage or unrecoverable provider error (fail-fast).
    LLMValidationError
        When the LLM response fails schema validation.
    """
    logger = get_logger("user_preference", run_id=run_id)

    llm_output = await complete(
        prompt=render_prompt(
            "user_preference_parse",
            user_name=name,
            email=email,
            user_prompt=prompt,
        ),
        system=_SYSTEM_PROMPT,
        response_model=UserProfileParseLLMOutput,
        run_id=run_id,
        agent_name="user_preference",
    )

    now = datetime.now(timezone.utc)
    profile = UserProfile(
        user_id=_user_id_from_email(email),
        name=name.strip(),
        email=email.strip(),
        home_city=home_city.strip(),
        horizon_days=horizon_days,
        created_at=now,
        last_updated=now,
        profile_version=1,
        **llm_output.model_dump(),
    )

    written_path = write_profile(profile)
    logger.info(
        "Cold-start profile parsed and saved",
        data={
            "profile_path": str(written_path),
            "user_id": profile.user_id,
            "home_city": profile.home_city,
            "horizon_days": profile.horizon_days,
            "stated_interests_count": len(profile.stated_interests),
            "vibe_preferences_count": len(profile.vibe_preferences),
            "excluded_categories_count": len(profile.excluded_categories),
        },
    )
    return profile


def _clamp_unit_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def _normalize_label(value: str) -> str:
    return value.strip().lower()


def _normalize_category_key(value: str) -> str:
    return _normalize_label(value).replace(" ", "_").replace("-", "_")


def _category_matches(label: str, candidate: str) -> bool:
    left = _normalize_category_key(label)
    right = _normalize_category_key(candidate)
    return left == right or left in right or right in left


def _find_category_weight_key(
    category_weights: dict[str, float],
    category: str,
) -> str:
    for key in category_weights:
        if _category_matches(category, key):
            return key
    return _normalize_category_key(category)


def _feedback_decay_weight(
    received_at: datetime,
    *,
    reference_time: datetime,
) -> float:
    age_seconds = (
        reference_time - received_at.astimezone(timezone.utc)
    ).total_seconds()
    age_days = max(0.0, age_seconds / 86_400.0)
    return math.exp(-FEEDBACK_DECAY_LAMBDA * age_days)


def _event_vibe_tags(
    signal: FeedbackEvent,
    events_by_id: Mapping[str, EnrichedEvent] | None,
) -> list[str]:
    if events_by_id and signal.event_id:
        event = events_by_id.get(signal.event_id)
        if event is not None:
            return [tag for tag in event.vibe_tags if tag in VIBE_VOCABULARY]
    return []


def _apply_category_feedback(
    category_weights: dict[str, float],
    signal: FeedbackEvent,
    *,
    decay_weight: float,
) -> None:
    if not signal.categories:
        return

    base_delta = (
        FEEDBACK_CLICK_CATEGORY_DELTA
        if signal.signal == "click"
        else FEEDBACK_NEGATIVE_CATEGORY_DELTA
    )
    delta = base_delta * decay_weight
    if delta == 0.0:
        return

    for category in signal.categories:
        key = _find_category_weight_key(category_weights, category)
        current = category_weights.get(key, FEEDBACK_DEFAULT_CATEGORY_WEIGHT)
        category_weights[key] = _clamp_unit_score(current + delta)


def _apply_vibe_feedback(
    vibe_preferences: list[str],
    vibe_tags: list[str],
    *,
    signal: FeedbackEvent,
    decay_weight: float,
) -> None:
    if decay_weight < VIBE_UPDATE_MIN_DECAY_WEIGHT or not vibe_tags:
        return

    if signal.signal == "click":
        for tag in vibe_tags:
            if tag not in vibe_preferences:
                vibe_preferences.append(tag)
        return

    vibe_preferences[:] = [tag for tag in vibe_preferences if tag not in vibe_tags]


def apply_feedback_signals(
    profile: UserProfile,
    signals: Sequence[FeedbackEvent],
    *,
    events_by_id: Mapping[str, EnrichedEvent] | None = None,
    chroma_collection: Collection | None = None,
    reference_time: datetime | None = None,
    persist: bool = True,
    run_id: str | None = None,
) -> UserProfile:
    """Apply decay-weighted feedback updates to ``profile`` and persist it.

    Category weights move by ``+0.03`` (click) or ``-0.05`` (negative) per
    category, scaled by ``e^(-λt)`` with a 30-day half-life. Vibe preferences
    add event vibe tags on clicks and remove them on negatives when the decay
    weight is at least :data:`VIBE_UPDATE_MIN_DECAY_WEIGHT`. Click signals also
    index liked events in Chroma when ``events_by_id`` supplies the event.

    Parameters
    ----------
    profile : UserProfile
        Profile to update in place logically; a new instance is returned.
    signals : Sequence[FeedbackEvent]
        Feedback events to apply, typically loaded from ``vol-feedback``.
    events_by_id : Mapping[str, EnrichedEvent], optional
        Enriched events keyed by ``event_id`` for Chroma indexing and vibe tags.
    chroma_collection : Collection, optional
        Target liked-events collection. Defaults to the persistent collection.
    reference_time : datetime, optional
        Time used for decay calculations. Defaults to current UTC time.
    persist : bool, optional
        When ``True``, write the updated profile to ``vol-profiles``.
    run_id : str, optional
        Pipeline run identifier for structured logging.

    Returns
    -------
    UserProfile
        Updated profile with refreshed ``last_updated``.
    """
    if not signals:
        return profile

    now = reference_time or datetime.now(timezone.utc)
    updated_weights = dict(profile.category_weights)
    updated_vibes = list(profile.vibe_preferences)
    chroma_events: list[EnrichedEvent] = []

    for signal in signals:
        decay_weight = _feedback_decay_weight(signal.received_at, reference_time=now)
        _apply_category_feedback(
            updated_weights,
            signal,
            decay_weight=decay_weight,
        )
        _apply_vibe_feedback(
            updated_vibes,
            _event_vibe_tags(signal, events_by_id),
            signal=signal,
            decay_weight=decay_weight,
        )
        if (
            signal.signal == "click"
            and signal.event_id
            and events_by_id
            and signal.event_id in events_by_id
        ):
            chroma_events.append(events_by_id[signal.event_id])

    updated_profile = profile.model_copy(
        update={
            "category_weights": updated_weights,
            "vibe_preferences": updated_vibes,
            "last_updated": now,
        },
    )

    collection = chroma_collection
    for event in chroma_events:
        chroma_service.add_liked_event(event, collection)

    if persist:
        write_profile(updated_profile)

    logger = get_logger("user_preference", run_id=run_id)
    logger.info(
        "Applied feedback signals to user profile",
        data={
            "signals_applied": len(signals),
            "chroma_events_indexed": len(chroma_events),
            "category_weights_count": len(updated_weights),
            "vibe_preferences_count": len(updated_vibes),
        },
    )
    return updated_profile
