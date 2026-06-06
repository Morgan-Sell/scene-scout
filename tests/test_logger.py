"""
Tests for the SceneScout logging service.

Covers agent color mapping, JSONL file creation, structured entry fields,
and run_id attachment via constructor and set_run_id().
"""

import json
import logging
from pathlib import Path

import pytest

from scene_scout.logging import get_logger
from scene_scout.logging.logger import AGENT_COLORS, AgentLogger

TEST_RUN_ID = "20250606-143022"


@pytest.fixture
def logs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect vol-logs to a temporary directory for isolation."""
    monkeypatch.setenv("VOL_LOGS_DIR", str(tmp_path))
    return tmp_path


def test_get_logger_returns_agent_logger() -> None:
    logger = get_logger("feed_scout")
    assert isinstance(logger, AgentLogger)


def test_agent_color_mapping() -> None:
    logger = get_logger("feed_scout")
    assert logger.display_name == "FEED SCOUT"
    assert logger.color == "cyan"

    curator = get_logger("recommendation_curator")
    assert curator.display_name == "RECOMMENDATION CURATOR"
    assert curator.color == "gold1"

    cache = get_logger("cache")
    assert cache.display_name == "CACHE"
    assert cache.color == "dim white"


def test_unknown_agent_uses_default_color() -> None:
    logger = get_logger("custom_agent")
    assert logger.display_name == "CUSTOM AGENT"
    assert logger.color == "white"


def test_all_documented_agents_have_color_assignments() -> None:
    expected_agents = {
        "orchestrator",
        "feed_scout",
        "event_extraction",
        "event_normalization",
        "deduplication",
        "description_quality",
        "geocoding",
        "talent_scout",
        "vibe_classifier",
        "neighborhood_scout",
        "user_preference",
        "ranking",
        "sellout_risk",
        "recommendation_curator",
        "email_composer",
        "evaluation",
        "cache",
        "llm",
    }
    assert expected_agents == set(AGENT_COLORS.keys())


def test_jsonl_written_with_run_id(logs_dir: Path) -> None:
    logger = get_logger("feed_scout", run_id=TEST_RUN_ID)
    logger.info("Feed OK: Test Feed — 2 entries fetched")

    log_file = logs_dir / f"{TEST_RUN_ID}.jsonl"
    assert log_file.exists()

    entry = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert entry["run_id"] == TEST_RUN_ID
    assert entry["agent"] == "feed_scout"
    assert entry["level"] == "INFO"
    assert entry["message"] == "Feed OK: Test Feed — 2 entries fetched"
    assert entry["data"] == {}
    assert "timestamp" in entry


def test_jsonl_includes_optional_data(logs_dir: Path) -> None:
    logger = get_logger("ranking", run_id=TEST_RUN_ID)
    logger.info(
        "Scored event",
        data={"event_id": "abc123", "score": 0.87},
    )

    entry = json.loads(
        (logs_dir / f"{TEST_RUN_ID}.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["data"] == {"event_id": "abc123", "score": 0.87}


def test_set_run_id_enables_jsonl(logs_dir: Path) -> None:
    logger = get_logger("orchestrator")
    logger.info("No JSONL yet")

    assert not list(logs_dir.glob("*.jsonl"))

    logger.set_run_id(TEST_RUN_ID)
    logger.info("Pipeline started")

    lines = (logs_dir / f"{TEST_RUN_ID}.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "Pipeline started"


def test_debug_respects_level_filter(logs_dir: Path) -> None:
    logger = get_logger("evaluation", run_id=TEST_RUN_ID)
    logger.debug("Hidden debug detail")

    assert not (logs_dir / f"{TEST_RUN_ID}.jsonl").exists()

    logger.set_level(logging.DEBUG)
    logger.debug("Visible debug detail")

    entry = json.loads(
        (logs_dir / f"{TEST_RUN_ID}.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["level"] == "DEBUG"
    assert entry["message"] == "Visible debug detail"


def test_message_formatting_with_args(logs_dir: Path) -> None:
    logger = get_logger("feed_scout", run_id=TEST_RUN_ID)
    logger.warning("Feed failed: %s — status=%s", "Broken Feed", "unreachable")

    entry = json.loads(
        (logs_dir / f"{TEST_RUN_ID}.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["message"] == "Feed failed: Broken Feed — status=unreachable"
    assert entry["level"] == "WARNING"
