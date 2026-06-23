"""
Tests for the SceneScout CLI and UAT skeleton.
"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scene_scout.cli import (
    build_parser,
    main,
    print_uat_summary,
    run_uat,
    uat_output_dir,
    write_summary_json,
)
from scene_scout.logging.logger import _logger_cache
from scene_scout.orchestrator import PipelineResult
from tests.conftest import TEST_RUN_ID

SANDLOT_PROMPT = (
    "I love sandlot baseball, pool parties at the rec center, "
    "and legends of the Great Bambino."
)


@pytest.fixture
def output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect UAT output to a temp directory."""
    monkeypatch.setattr("scene_scout.cli._OUTPUT_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def reset_loggers() -> None:
    """Reset cached loggers between CLI tests so other modules stay isolated."""
    _logger_cache.clear()
    yield
    _logger_cache.clear()


def test_build_parser_uat_subcommand() -> None:
    args = build_parser().parse_args(
        ["uat", "--prompt", SANDLOT_PROMPT, "--dry-run", "--verbose"]
    )

    assert args.command == "uat"
    assert args.prompt == SANDLOT_PROMPT
    assert args.dry_run is True
    assert args.verbose is True


def test_write_summary_json_writes_pipeline_counts(tmp_path: Path) -> None:
    run_dir = tmp_path / f"uat_{TEST_RUN_ID}"
    run_dir.mkdir()
    result = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt=SANDLOT_PROMPT,
        raw_entries=10,
        feeds_fetched=3,
        feeds_unchanged=1,
        seen_entries_cache_hits=3,
        seen_entries_cache_misses=7,
        seen_entries_hit_rate_pct=30.0,
        enrichment_cache_hit_rates_pct={
            "performer": 50.0,
            "venue": 0.0,
            "vibe": 100.0,
        },
        top_recommendations=[
            {
                "title": "Jazz Night",
                "score": 0.91,
                "source_count": 2,
                "source_coverage": 0.67,
                "wildcard_slot": False,
            }
        ],
        email_preview_path="output/uat_test/email_preview.html",
    )

    summary_path = write_summary_json(run_dir, result)

    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["run_id"] == TEST_RUN_ID
    assert summary["raw_entries"] == 10
    assert summary["feeds_fetched"] == 3
    assert summary["feeds_unchanged"] == 1
    assert summary["seen_entries_cache_hits"] == 3
    assert summary["seen_entries_cache_misses"] == 7
    assert summary["seen_entries_hit_rate_pct"] == 30.0
    assert summary["enrichment_cache_hit_rates_pct"]["vibe"] == 100.0
    assert summary["top_recommendations"][0]["source_count"] == 2
    assert summary["email_preview_path"].endswith("email_preview.html")
    assert summary["pre_enrichment_discards"]["low_information"] == 0


def test_print_uat_summary_renders_without_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt=SANDLOT_PROMPT,
        feeds_fetched=2,
        feeds_unchanged=1,
        top_recommendations=[
            {
                "title": "Gallery Opening",
                "score": 0.88,
                "source_count": 1,
                "source_coverage": 0.33,
                "wildcard_slot": False,
            }
        ],
    )

    print_uat_summary(result)

    captured = capsys.readouterr()
    assert "Feeds UNCHANGED (304)" in captured.out
    assert "Gallery Opening" in captured.out


@pytest.mark.asyncio
async def test_run_uat_creates_output_directory_and_summary(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_result = PipelineResult(run_id=TEST_RUN_ID, user_prompt=SANDLOT_PROMPT)
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=AsyncMock(return_value=mock_result)),
    )

    result = await run_uat(SANDLOT_PROMPT)

    assert result.run_id == TEST_RUN_ID
    run_dir = output_dir / f"uat_{TEST_RUN_ID}"
    assert run_dir.is_dir()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["run_id"] == TEST_RUN_ID
    assert summary["raw_entries"] == 0
    assert summary["seen_entries_cache_hits"] == 0


@pytest.mark.asyncio
async def test_run_uat_dry_run_sets_environment(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DRY_RUN", raising=False)
    mock_result = PipelineResult(run_id=TEST_RUN_ID, user_prompt=SANDLOT_PROMPT)
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=AsyncMock(return_value=mock_result)),
    )

    await run_uat(SANDLOT_PROMPT, dry_run=True)

    import os

    assert os.environ["DRY_RUN"] == "true"


@pytest.mark.asyncio
async def test_run_uat_verbose_enables_debug_logging(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    mock_result = PipelineResult(run_id=TEST_RUN_ID, user_prompt=SANDLOT_PROMPT)
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=AsyncMock(return_value=mock_result)),
    )
    configure_mock = MagicMock()
    monkeypatch.setattr("scene_scout.cli.configure_log_level", configure_mock)

    await run_uat(SANDLOT_PROMPT, verbose=True)

    configure_mock.assert_called_once_with(logging.DEBUG)


def test_main_uat_command_exits_zero(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_result = PipelineResult(run_id=TEST_RUN_ID, user_prompt="test")
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=AsyncMock(return_value=mock_result)),
    )

    exit_code = main(["uat", "--prompt", "test"])

    assert exit_code == 0
    assert (output_dir / f"uat_{TEST_RUN_ID}" / "summary.json").exists()


def test_uat_output_dir_path() -> None:
    path = uat_output_dir(TEST_RUN_ID)
    assert path.name == f"uat_{TEST_RUN_ID}"
    assert path.parent.name == "output"
