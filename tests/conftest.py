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


@pytest.fixture
def logs_dir(_isolate_vol_logs: Path) -> Path:
    """Temp directory where JSONL run logs are written during tests."""
    return _isolate_vol_logs
