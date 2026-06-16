"""
Shared pytest fixtures for SceneScout tests.

Cross-cutting fixtures live here so individual test modules stay focused on
behavior under test. Domain-specific helpers (RSS payloads, LLM mocks) remain
in their respective test files until a second consumer appears.

Test fixture strings use Sandlot (1993) references — obviously fake, never
confusable with production run IDs or real feed data.
"""

from pathlib import Path

import pytest

from scene_scout.db import run_migrations
from scene_scout.services import feedback as feedback_service
from scene_scout.services import history as history_service

# Ham to Scotty — our canonical fake pipeline run ID.
TEST_RUN_ID = "youre-killing-me-smalls"


@pytest.fixture(autouse=True)
def _isolate_vol_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect vol-logs to a temp dir so tests never write to the real vol-logs/.

    The Beast's backyard: off-limits in the movie, safe for test debris.
    """
    log_dir = tmp_path / "the-beasts-backyard"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VOL_LOGS_DIR", str(log_dir))
    return log_dir


@pytest.fixture(autouse=True)
def _isolate_vol_pipeline_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect vol-pipeline-state to a temp dir for every test."""
    state_dir = tmp_path / "squints-pipeline-locker"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VOL_PIPELINE_STATE_DIR", str(state_dir))
    return state_dir


@pytest.fixture(autouse=True)
def _isolate_vol_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect vol-cache to a temp dir so tests never write to the real vol-cache/."""
    cache_dir = tmp_path / "wendy-peffercorn-locker"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VOL_CACHE_DIR", str(cache_dir))
    return cache_dir


@pytest.fixture
def logs_dir(_isolate_vol_logs: Path) -> Path:
    """Temp directory where JSONL run logs are written during tests."""
    return _isolate_vol_logs


@pytest.fixture
def migration_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Isolated feedback and history directories for migration-backed tests."""
    feedback_dir = tmp_path / "vol-feedback"
    history_dir = tmp_path / "vol-history"
    feedback_dir.mkdir()
    history_dir.mkdir()
    feedback_url = f"sqlite:///{feedback_dir / 'feedback.db'}"
    history_url = f"sqlite:///{history_dir / 'history.db'}"
    monkeypatch.setenv("DATABASE_FEEDBACK_URL", feedback_url)
    monkeypatch.setenv("DATABASE_HISTORY_URL", history_url)
    return feedback_dir, history_dir


@pytest.fixture
def migrated_databases(migration_dirs: tuple[Path, Path]) -> tuple[Path, Path]:
    """Run Alembic migrations and reset service engines for isolated DBs."""
    run_migrations()
    feedback_service.reset_feedback_engine()
    history_service.reset_history_engine()
    yield migration_dirs
    feedback_service.reset_feedback_engine()
    history_service.reset_history_engine()
