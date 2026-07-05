"""
User Preference Agent

Responsibility
--------------
Parse a cold-start onboarding prompt into a structured ``UserProfile``, persist it
to ``vol-profiles/profile.json``, and load the current profile for downstream agents.

Design
------
Inputs  : name, email, cold-start prompt, run_id
Outputs : ``UserProfile`` written to ``vol-profiles/profile.json``
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from scene_scout.config import vol_profiles_dir
from scene_scout.logging import get_logger
from scene_scout.models.user import UserProfile, UserProfileParseLLMOutput
from scene_scout.services.llm import complete
from scene_scout.services.prompt_loader import render_prompt

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
