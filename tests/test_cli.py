"""
Tests for the SceneScout CLI and UAT skeleton.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from scene_scout.cli import (
    build_feed_probe_payload,
    build_parser,
    build_uat_run_options,
    feed_probe_output_path,
    feed_probe_run_id,
    main,
    print_uat_summary,
    run_feed_probe,
    run_uat,
    uat_output_dir,
    uat_summary_status,
)
from scene_scout.logging.logger import _logger_cache
from scene_scout.models.feed import FeedHealthReport, FeedStatus
from scene_scout.models.user import HORIZON_DAYS_MAX
from scene_scout.orchestrator import PipelineResult, PipelineRunError
from scene_scout.orchestrator_config import UatRunOptions
from scene_scout.uat_artifacts import write_summary_json
from tests.conftest import TEST_RUN_ID

SANDLOT_PROMPT = (
    "I love sandlot baseball, pool parties at the rec center, "
    "and legends of the Great Bambino."
)

PROBE_NOW = datetime(2026, 6, 29, 12, 34, 56, tzinfo=timezone.utc)
PROBE_RUN_ID = "20260629-123456"


def _feed_health_report(**overrides: object) -> FeedHealthReport:
    payload = {
        "feed_id": "brooklynvegan",
        "feed_name": "BrooklynVegan",
        "feed_url": "https://www.brooklynvegan.com/feed/",
        "status": FeedStatus.OK,
        "entries_fetched": 12,
        "fetched_at": PROBE_NOW,
    }
    payload.update(overrides)
    return FeedHealthReport.model_validate(payload)


def _mock_cache_service() -> MagicMock:
    cache = MagicMock()
    cache.get_feed_etag.return_value = None
    return cache


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


def test_build_parser_feed_probe_city_flag() -> None:
    args = build_parser().parse_args(
        ["feed-probe", "--city", "New York", "--allow-failures"]
    )

    assert args.command == "feed-probe"
    assert args.city == "New York"
    assert args.allow_failures is True


def test_build_parser_uat_subcommand() -> None:
    args = build_parser().parse_args(
        ["uat", "--prompt", SANDLOT_PROMPT, "--dry-run", "--verbose"]
    )

    assert args.command == "uat"
    assert args.prompt == SANDLOT_PROMPT
    assert args.dry_run is True
    assert args.verbose is True


def test_build_parser_uat_abbreviated_flags() -> None:
    args = build_parser().parse_args(
        [
            "uat",
            "--prompt",
            SANDLOT_PROMPT,
            "--dry-run",
            "--max-extraction",
            "25",
            "--feeds",
            "brooklynvegan,theskint",
            "--stop-after",
            "extract",
        ]
    )

    assert args.max_extraction == 25
    assert args.feeds == "brooklynvegan,theskint"
    assert args.stop_after == "extract"


def test_build_parser_uat_city_and_horizon_flags() -> None:
    args = build_parser().parse_args(
        [
            "uat",
            "--prompt",
            SANDLOT_PROMPT,
            "--city",
            "New York",
            "--horizon-days",
            "21",
        ]
    )

    assert args.city == "New York"
    assert args.horizon_days == 21


def test_build_uat_run_options_resolves_city_and_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAT_HOME_CITY", "Chicago")
    monkeypatch.setenv("UAT_HORIZON_DAYS", "30")
    options = build_uat_run_options()
    assert options.home_city == "Chicago"
    assert options.horizon_days == 30


def test_build_uat_run_options_cli_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAT_HOME_CITY", "Chicago")
    monkeypatch.setenv("UAT_HORIZON_DAYS", "30")
    options = build_uat_run_options(home_city="Boston", horizon_days=7)
    assert options.home_city == "Boston"
    assert options.horizon_days == 7


def test_main_uat_rejects_invalid_horizon_days() -> None:
    exit_code = main(
        ["uat", "--prompt", "test", "--horizon-days", str(HORIZON_DAYS_MAX + 1)]
    )
    assert exit_code == 1


def test_build_uat_run_options_resolves_env_max_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAT_MAX_EXTRACTION", "15")
    options = build_uat_run_options(feeds="a,b")
    assert options.max_extraction == 15
    assert options.feed_ids == frozenset({"a", "b"})


def test_uat_summary_status_completed_only_on_full_run() -> None:
    assert (
        uat_summary_status(
            PipelineResult(
                run_id=TEST_RUN_ID,
                user_prompt=SANDLOT_PROMPT,
                last_completed_stage="complete",
            )
        )
        == "completed"
    )
    assert (
        uat_summary_status(
            PipelineResult(
                run_id=TEST_RUN_ID,
                user_prompt=SANDLOT_PROMPT,
                last_completed_stage="feeds",
            )
        )
        == "partial"
    )


def test_build_parser_feed_probe_subcommand() -> None:
    args = build_parser().parse_args(["feed-probe", "--allow-failures", "--verbose"])

    assert args.command == "feed-probe"
    assert args.allow_failures is True
    assert args.verbose is True


def test_feed_probe_run_id_and_output_path() -> None:
    assert feed_probe_run_id(PROBE_NOW) == PROBE_RUN_ID
    assert (
        feed_probe_output_path(PROBE_RUN_ID).name == f"feed_probe_{PROBE_RUN_ID}.json"
    )


def test_build_feed_probe_payload_marks_all_ok_when_feeds_healthy() -> None:
    reports = [
        _feed_health_report(),
        _feed_health_report(
            feed_id="theskint",
            feed_name="the skint",
            feed_url="https://www.theskint.com/feed/",
            status=FeedStatus.UNCHANGED,
            entries_fetched=0,
        ),
    ]

    payload = build_feed_probe_payload(PROBE_RUN_ID, ["entry"] * 12, reports)

    assert payload["run_id"] == PROBE_RUN_ID
    assert payload["feeds_fetched"] == 2
    assert payload["feeds_unchanged"] == 1
    assert payload["raw_entries"] == 12
    assert payload["all_ok"] is True
    assert payload["feed_health"][1]["status"] == "unchanged"


def test_build_feed_probe_payload_all_ok_false_when_no_feeds() -> None:
    payload = build_feed_probe_payload(PROBE_RUN_ID, [], [])

    assert payload["feeds_fetched"] == 0
    assert payload["raw_entries"] == 0
    assert payload["all_ok"] is False
    assert payload["feed_health"] == []


@pytest.mark.asyncio
async def test_run_feed_probe_returns_nonzero_when_no_active_feeds(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scene_scout.cli.load_feed_configs",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "scene_scout.cli.feed_scout.run",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        "scene_scout.cli.CacheService",
        lambda run_id, db_path=None: _mock_cache_service(),
    )

    result = await run_feed_probe(now=PROBE_NOW)

    assert result.exit_code == 1
    assert result.payload["all_ok"] is False
    assert result.payload["feeds_fetched"] == 0


@pytest.mark.asyncio
async def test_run_feed_probe_writes_json_and_returns_zero_on_success(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [_feed_health_report()]
    monkeypatch.setattr(
        "scene_scout.cli.load_feed_configs",
        lambda *args, **kwargs: [object()],
    )
    monkeypatch.setattr(
        "scene_scout.cli.feed_scout.run",
        AsyncMock(return_value=(["entry"] * 12, reports)),
    )
    monkeypatch.setattr(
        "scene_scout.cli.CacheService",
        lambda run_id, db_path=None: _mock_cache_service(),
    )

    result = await run_feed_probe(now=PROBE_NOW)

    assert result.exit_code == 0
    assert result.output_path == output_dir / f"feed_probe_{PROBE_RUN_ID}.json"
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["all_ok"] is True
    assert payload["raw_entries"] == 12
    assert payload["feed_health"][0]["feed_name"] == "BrooklynVegan"


@pytest.mark.asyncio
async def test_run_feed_probe_returns_nonzero_when_feed_unhealthy(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [
        _feed_health_report(
            status=FeedStatus.UNREACHABLE,
            entries_fetched=0,
            error_message="HTTP 401",
        )
    ]
    monkeypatch.setattr(
        "scene_scout.cli.load_feed_configs",
        lambda *args, **kwargs: [object()],
    )
    monkeypatch.setattr(
        "scene_scout.cli.feed_scout.run",
        AsyncMock(return_value=([], reports)),
    )
    monkeypatch.setattr(
        "scene_scout.cli.CacheService",
        lambda run_id, db_path=None: _mock_cache_service(),
    )

    result = await run_feed_probe(now=PROBE_NOW)

    assert result.exit_code == 1
    assert result.payload["all_ok"] is False


@pytest.mark.asyncio
async def test_run_feed_probe_allow_failures_returns_zero(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [
        _feed_health_report(
            status=FeedStatus.MALFORMED,
            entries_fetched=0,
            error_message="bad xml",
        )
    ]
    monkeypatch.setattr(
        "scene_scout.cli.load_feed_configs",
        lambda *args, **kwargs: [object()],
    )
    monkeypatch.setattr(
        "scene_scout.cli.feed_scout.run",
        AsyncMock(return_value=([], reports)),
    )
    monkeypatch.setattr(
        "scene_scout.cli.CacheService",
        lambda run_id, db_path=None: _mock_cache_service(),
    )

    result = await run_feed_probe(now=PROBE_NOW, allow_failures=True)

    assert result.exit_code == 0


def test_main_feed_probe_exits_nonzero_on_failed_feed(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [
        _feed_health_report(
            status=FeedStatus.STALE,
            entries_fetched=1,
            error_message="below threshold",
        )
    ]
    monkeypatch.setattr(
        "scene_scout.cli.feed_probe_run_id", lambda now=None: PROBE_RUN_ID
    )
    monkeypatch.setattr(
        "scene_scout.cli.load_feed_configs",
        lambda *args, **kwargs: [object()],
    )
    monkeypatch.setattr(
        "scene_scout.cli.feed_scout.run",
        AsyncMock(return_value=([], reports)),
    )
    monkeypatch.setattr(
        "scene_scout.cli.CacheService",
        lambda run_id, db_path=None: _mock_cache_service(),
    )

    exit_code = main(["feed-probe"])

    assert exit_code == 1
    assert (output_dir / f"feed_probe_{PROBE_RUN_ID}.json").exists()


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
    assert summary["status"] == "completed"
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
    mock_result = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt=SANDLOT_PROMPT,
        last_completed_stage="complete",
    )
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
    assert summary["status"] == "completed"
    assert summary["raw_entries"] == 0
    assert summary["seen_entries_cache_hits"] == 0


@pytest.mark.asyncio
async def test_run_uat_dry_run_sets_environment(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DRY_RUN", raising=False)
    mock_result = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt=SANDLOT_PROMPT,
        last_completed_stage="complete",
    )
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=AsyncMock(return_value=mock_result)),
    )

    await run_uat(SANDLOT_PROMPT, dry_run=True)

    import os

    assert os.environ["DRY_RUN"] == "true"


@pytest.mark.asyncio
async def test_run_uat_writes_partial_summary_on_early_stop(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UAT_HOME_CITY", raising=False)
    monkeypatch.delenv("UAT_HORIZON_DAYS", raising=False)
    monkeypatch.delenv("UAT_MAX_EXTRACTION", raising=False)

    partial = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt=SANDLOT_PROMPT,
        raw_entries=42,
        last_completed_stage="feeds",
    )
    run_mock = AsyncMock(return_value=partial)
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=run_mock),
    )

    await run_uat(
        SANDLOT_PROMPT,
        dry_run=True,
        feeds="brooklynvegan,theskint",
        stop_after="feeds",
    )

    run_mock.assert_awaited_once()
    _, kwargs = run_mock.await_args
    assert kwargs["uat_options"] == UatRunOptions(
        feed_ids=frozenset({"brooklynvegan", "theskint"}),
        stop_after="feeds",
    )

    summary = json.loads(
        (output_dir / f"uat_{TEST_RUN_ID}" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "partial"
    assert summary["last_completed_stage"] == "feeds"
    assert summary["raw_entries"] == 42


@pytest.mark.asyncio
async def test_run_uat_passes_max_extraction_to_orchestrator(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_result = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt=SANDLOT_PROMPT,
        last_completed_stage="complete",
    )
    run_mock = AsyncMock(return_value=mock_result)
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=run_mock),
    )

    await run_uat(SANDLOT_PROMPT, max_extraction=25)

    _, kwargs = run_mock.await_args
    assert kwargs["uat_options"].max_extraction == 25


def test_main_uat_rejects_invalid_max_extraction() -> None:
    exit_code = main(["uat", "--prompt", "test", "--max-extraction", "0"])
    assert exit_code == 1


@pytest.mark.asyncio
async def test_run_uat_verbose_enables_debug_logging(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    mock_result = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt=SANDLOT_PROMPT,
        last_completed_stage="complete",
    )
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=AsyncMock(return_value=mock_result)),
    )
    configure_mock = MagicMock()
    monkeypatch.setattr("scene_scout.cli.configure_log_level", configure_mock)

    await run_uat(SANDLOT_PROMPT, verbose=True)

    configure_mock.assert_called_once_with(logging.DEBUG)


@pytest.mark.asyncio
async def test_run_uat_writes_failure_artifacts_on_pipeline_error(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt=SANDLOT_PROMPT,
        raw_entries=12,
        extraction_candidates=8,
        normalized_events=3,
        last_completed_stage="normalization",
    )

    async def _raise_pipeline_error(prompt: str, *, uat_output_base=None, **kwargs):
        raise PipelineRunError(partial, ValueError("zip() argument 2 is shorter"))

    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=_raise_pipeline_error),
    )

    with pytest.raises(PipelineRunError):
        await run_uat(SANDLOT_PROMPT)

    run_dir = output_dir / f"uat_{TEST_RUN_ID}"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "error.json").read_text(encoding="utf-8"))

    assert summary["status"] == "failed"
    assert summary["raw_entries"] == 12
    assert summary["normalized_events"] == 3
    assert summary["last_completed_stage"] == "normalization"
    assert error["exception_type"] == "ValueError"
    assert "zip()" in error["message"]
    assert error["last_completed_stage"] == "normalization"


def test_main_uat_command_exits_zero(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_result = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt="test",
        last_completed_stage="complete",
    )
    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=AsyncMock(return_value=mock_result)),
    )

    exit_code = main(["uat", "--prompt", "test"])

    assert exit_code == 0
    assert (output_dir / f"uat_{TEST_RUN_ID}" / "summary.json").exists()


def test_main_uat_command_exits_one_on_pipeline_failure(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = PipelineResult(
        run_id=TEST_RUN_ID,
        user_prompt="test",
        last_completed_stage="feed_scout",
    )

    async def _raise_pipeline_error(prompt: str, *, uat_output_base=None, **kwargs):
        raise PipelineRunError(partial, RuntimeError("boom"))

    monkeypatch.setattr(
        "scene_scout.cli.Orchestrator",
        lambda: AsyncMock(run=_raise_pipeline_error),
    )

    exit_code = main(["uat", "--prompt", "test"])

    assert exit_code == 1
    run_dir = output_dir / f"uat_{TEST_RUN_ID}"
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "error.json").exists()


def test_uat_output_dir_path() -> None:
    path = uat_output_dir(TEST_RUN_ID)
    assert path.name == f"uat_{TEST_RUN_ID}"
    assert path.parent.name == "output"
