"""
Tests for the SceneScout logging service.

Covers agent color mapping, JSONL file creation, structured entry fields,
and run_id attachment via constructor and set_run_id().
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from scene_scout.logging import get_logger, prune_old_run_logs
from scene_scout.logging.logger import (
    AGENT_COLORS,
    RUN_LOG_RETENTION_DAYS,
    AgentLogger,
)
from tests.conftest import TEST_RUN_ID


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
    logger.info("Feed OK: Mr. Mertle's Events — 2 entries fetched")

    log_file = logs_dir / f"{TEST_RUN_ID}.jsonl"
    assert log_file.exists()

    entry = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert entry["run_id"] == TEST_RUN_ID
    assert entry["agent"] == "feed_scout"
    assert entry["level"] == "INFO"
    assert entry["message"] == "Feed OK: Mr. Mertle's Events — 2 entries fetched"
    assert entry["data"] == {}
    assert "timestamp" in entry


def test_jsonl_includes_optional_data(logs_dir: Path) -> None:
    logger = get_logger("ranking", run_id=TEST_RUN_ID)
    logger.info(
        "Scored event",
        data={"event_id": "the-great-bambino", "score": 0.87},
    )

    entry = json.loads(
        (logs_dir / f"{TEST_RUN_ID}.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["data"] == {"event_id": "the-great-bambino", "score": 0.87}


def test_set_run_id_enables_jsonl(logs_dir: Path) -> None:
    logger = get_logger("orchestrator")
    logger.info("No JSONL yet")

    assert not list(logs_dir.glob("*.jsonl"))

    logger.set_run_id(TEST_RUN_ID)
    logger.info("Pipeline started")

    lines = (
        (logs_dir / f"{TEST_RUN_ID}.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
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
    logger.warning("Feed failed: %s — status=%s", "The Beast's Yard RSS", "unreachable")

    entry = json.loads(
        (logs_dir / f"{TEST_RUN_ID}.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["message"] == "Feed failed: The Beast's Yard RSS — status=unreachable"
    assert entry["level"] == "WARNING"


def test_prune_old_run_logs_deletes_expired_files(logs_dir: Path) -> None:
    expired = logs_dir / "20250101-120000.jsonl"
    recent = logs_dir / "20260618-120000.jsonl"
    expired.write_text('{"old": true}\n', encoding="utf-8")
    recent.write_text('{"recent": true}\n', encoding="utf-8")

    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    stats = prune_old_run_logs(now=now)

    assert stats["deleted"] == 1
    assert stats["kept"] == 1
    assert stats["retention_days"] == RUN_LOG_RETENTION_DAYS
    assert not expired.exists()
    assert recent.exists()


def test_prune_old_run_logs_uses_mtime_for_non_run_id_names(logs_dir: Path) -> None:
    stale = logs_dir / "youre-killing-me-smalls.jsonl"
    stale.write_text('{"test": true}\n', encoding="utf-8")
    old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(stale, (old_ts, old_ts))

    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    stats = prune_old_run_logs(now=now)

    assert stats["deleted"] == 1
    assert not stale.exists()
