"""
Recommendation Curator configuration for SceneScout.

``CuratorConfig`` holds Allegra's display name and voice brief loaded from
``scene_scout/prompts/curator_voice.txt``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from scene_scout.ranking_config import WILDCARD_SLOT_COUNT

_CURATOR_VOICE_PATH = Path(__file__).parent / "prompts" / "curator_voice.txt"

CURATOR_MAX_RECOMMENDATIONS = 10
CURATOR_MAX_PER_CATEGORY = 3
CURATOR_MAX_PER_VENUE = 2
CURATOR_MIN_DISTINCT_DATES = 2
CURATOR_MIN_WILDCARD_SLOTS = 1
CURATOR_MAX_WILDCARD_SLOTS = WILDCARD_SLOT_COUNT


class CuratorConfig(BaseModel):
    """Curator persona settings for the Recommendation Curator and Email Composer."""

    name: str = "Allegra"
    voice_brief: str


def load_curator_config(*, name: str = "Allegra") -> CuratorConfig:
    """Load curator settings with the voice brief from ``curator_voice.txt``.

    Parameters
    ----------
    name : str
        Curator display name. Defaults to ``"Allegra"``.

    Returns
    -------
    CuratorConfig
        Curator configuration with the file-backed voice brief.

    Raises
    ------
    FileNotFoundError
        If ``curator_voice.txt`` is missing.
    """
    if not _CURATOR_VOICE_PATH.is_file():
        raise FileNotFoundError(
            f"Curator voice brief not found at: {_CURATOR_VOICE_PATH}"
        )

    voice_brief = _CURATOR_VOICE_PATH.read_text(encoding="utf-8").strip()
    return CuratorConfig(name=name, voice_brief=voice_brief)
