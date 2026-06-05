"""
Configuration loading for SceneScout.

Loads feed configuration from feeds.yaml and environment variables
from .env. All config access goes through this module — no other
module should read files or environment variables directly.
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from scene_scout.models.feed import FeedConfig

load_dotenv()

# Resolve config directory relative to the project root.
# This works whether the code is run from the repo root or elsewhere.
_PROJECT_ROOT = Path(__file__).parent.parent
_FEEDS_CONFIG_PATH = _PROJECT_ROOT / "config" / "feeds.yaml"


def load_feed_configs(path: Path = _FEEDS_CONFIG_PATH) -> list[FeedConfig]:
    """
    Load and validate feed configurations from feeds.yaml.

    Returns only active feeds. Raises on malformed config so failures
    are loud at startup rather than silent during a pipeline run.
    """
    if not path.exists():
        raise FileNotFoundError(f"Feed config not found at: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw or "feeds" not in raw:
        raise ValueError(f"Feed config at {path} is missing a 'feeds' key")

    configs = [FeedConfig(**entry) for entry in raw["feeds"]]
    active = [c for c in configs if c.active]

    return active


def is_dry_run() -> bool:
    """Return True if the pipeline should run without sending email."""
    return os.getenv("DRY_RUN", "false").lower() == "true"
