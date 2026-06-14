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

# Resolve paths relative to the project root.
# This works whether the code is run from the repo root or elsewhere.
PROJECT_ROOT = Path(__file__).parent.parent
_FEEDS_CONFIG_PATH = PROJECT_ROOT / "config" / "feeds.yaml"


def _vol_dir(env_var: str, default_relative: str) -> Path:
    """Return a persistent volume directory, honoring ``env_var`` when set."""
    configured = os.getenv(env_var)
    path = Path(configured) if configured else PROJECT_ROOT / default_relative
    path.mkdir(parents=True, exist_ok=True)
    return path


def vol_logs_dir() -> Path:
    """Return the directory for structured JSONL run logs."""
    return _vol_dir("VOL_LOGS_DIR", "vol-logs")


def vol_pipeline_state_dir() -> Path:
    """Return the directory for persisted pipeline state."""
    return _vol_dir("VOL_PIPELINE_STATE_DIR", "vol-pipeline-state")


def vol_cache_dir() -> Path:
    """Return the directory for the SQLite cache database."""
    return _vol_dir("VOL_CACHE_DIR", "vol-cache")


def vol_history_dir() -> Path:
    """Return the directory for recommendation history storage."""
    return _vol_dir("VOL_HISTORY_DIR", "vol-history")


def vol_profiles_dir() -> Path:
    """Return the directory for persisted user profile JSON."""
    return _vol_dir("VOL_PROFILES_DIR", "vol-profiles")


def vol_chroma_dir() -> Path:
    """Return the directory for the Chroma liked-events vector index."""
    return _vol_dir("VOL_CHROMA_DIR", "vol-chroma")


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


# LiteLLM configuration — provider-swappable via LLM_MODEL only.
LLM_MODEL: str = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
LLM_API_KEY: str | None = os.getenv("LLM_API_KEY")
LLM_API_BASE: str | None = os.getenv("LLM_API_BASE")
LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RETRY_BASE_DELAY_SECONDS: float = float(
    os.getenv("LLM_RETRY_BASE_DELAY_SECONDS", "1.0")
)

# Description quality — events below this score are flagged low_information.
DESCRIPTION_QUALITY_THRESHOLD: float = float(
    os.getenv("DESCRIPTION_QUALITY_THRESHOLD", "0.3")
)

# Ranking — source coverage and component weights (must sum to 1.0).
SOURCE_COVERAGE_MAX: int = int(os.getenv("SOURCE_COVERAGE_MAX", "3"))

RANKING_WEIGHT_CATEGORY_FIT: float = float(
    os.getenv("RANKING_WEIGHT_CATEGORY_FIT", "0.20")
)
RANKING_WEIGHT_VIBE_FIT: float = float(os.getenv("RANKING_WEIGHT_VIBE_FIT", "0.15"))
RANKING_WEIGHT_SEMANTIC_SIMILARITY: float = float(
    os.getenv("RANKING_WEIGHT_SEMANTIC_SIMILARITY", "0.10")
)
RANKING_WEIGHT_PERFORMER_AFFINITY: float = float(
    os.getenv("RANKING_WEIGHT_PERFORMER_AFFINITY", "0.10")
)
RANKING_WEIGHT_LOCATION: float = float(os.getenv("RANKING_WEIGHT_LOCATION", "0.10"))
RANKING_WEIGHT_NOVELTY: float = float(os.getenv("RANKING_WEIGHT_NOVELTY", "0.10"))
RANKING_WEIGHT_SOURCE_QUALITY: float = float(
    os.getenv("RANKING_WEIGHT_SOURCE_QUALITY", "0.05")
)
RANKING_WEIGHT_SOURCE_COVERAGE: float = float(
    os.getenv("RANKING_WEIGHT_SOURCE_COVERAGE", "0.05")
)
RANKING_WEIGHT_DESCRIPTION_QUALITY: float = float(
    os.getenv("RANKING_WEIGHT_DESCRIPTION_QUALITY", "0.15")
)

RANKING_COMPONENT_WEIGHTS: dict[str, float] = {
    "category_fit": RANKING_WEIGHT_CATEGORY_FIT,
    "vibe_fit": RANKING_WEIGHT_VIBE_FIT,
    "semantic_similarity": RANKING_WEIGHT_SEMANTIC_SIMILARITY,
    "performer_affinity": RANKING_WEIGHT_PERFORMER_AFFINITY,
    "location": RANKING_WEIGHT_LOCATION,
    "novelty": RANKING_WEIGHT_NOVELTY,
    "source_quality": RANKING_WEIGHT_SOURCE_QUALITY,
    "source_coverage": RANKING_WEIGHT_SOURCE_COVERAGE,
    "description_quality": RANKING_WEIGHT_DESCRIPTION_QUALITY,
}
