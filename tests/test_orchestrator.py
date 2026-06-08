"""
Tests for the orchestrator skeleton and PipelineState persistence.
"""

from pathlib import Path

import pytest

from scene_scout.orchestrator import (
    Orchestrator,
    PipelineResult,
    PipelineState,
    clear_pipeline_state,
    read_pipeline_state,
    write_pipeline_state,
)
from tests.conftest import TEST_RUN_ID

SANDLOT_PROMPT = (
    "Find me sandlot events near the Beast's backyard — "
    "baseball, pool parties, and legends of the Great Bambino."
)


@pytest.fixture
def pipeline_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect vol-pipeline-state to a temp dir for isolation."""
    state_dir = tmp_path / "squints-pipeline-locker"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VOL_PIPELINE_STATE_DIR", str(state_dir))
    return state_dir


def test_pipeline_state_roundtrip_json() -> None:
    state = PipelineState(
        run_id=TEST_RUN_ID,
        filtered_events=[{"title": "The Great Bambino Night", "venue": "The Sandlot"}],
        batch_id="benny-the-jet-batch",
        phase="batch_submitted",
    )

    restored = PipelineState.from_json(state.to_json())

    assert restored.run_id == TEST_RUN_ID
    assert restored.filtered_events[0]["title"] == "The Great Bambino Night"
    assert restored.batch_id == "benny-the-jet-batch"
    assert restored.phase == "batch_submitted"


def test_write_and_read_pipeline_state(pipeline_state_dir: Path) -> None:
    state = PipelineState(run_id=TEST_RUN_ID, phase="phase_1")
    write_pipeline_state(state)

    path = pipeline_state_dir / "pipeline_state.json"
    assert path.exists()

    restored = read_pipeline_state()
    assert restored is not None
    assert restored.run_id == TEST_RUN_ID
    assert restored.phase == "phase_1"


def test_clear_pipeline_state_removes_file(pipeline_state_dir: Path) -> None:
    write_pipeline_state(PipelineState(run_id=TEST_RUN_ID))
    clear_pipeline_state()

    assert read_pipeline_state() is None
    assert not (pipeline_state_dir / "pipeline_state.json").exists()


@pytest.mark.asyncio
async def test_orchestrator_run_returns_zero_counts(pipeline_state_dir: Path) -> None:
    result = await Orchestrator().run(SANDLOT_PROMPT)

    assert isinstance(result, PipelineResult)
    assert result.user_prompt == SANDLOT_PROMPT
    assert len(result.run_id) == 15
    assert result.run_id[8] == "-"
    assert result.raw_entries == 0
    assert result.feeds_unchanged == 0
    assert result.seen_entries_cache_hits == 0
    assert result.extraction_candidates == 0
    assert result.normalized_events == 0
    assert result.deduplicated_events == 0
    assert result.after_description_quality == 0
    assert result.after_pre_enrichment_filter == 0
    assert result.enriched_events == 0
    assert result.ranked_events == 0
    assert result.after_sellout_risk == 0
    assert result.curated_recommendations == 0
    assert result.evaluation_flags == 0


@pytest.mark.asyncio
async def test_orchestrator_clears_pipeline_state_on_success(
    pipeline_state_dir: Path,
) -> None:
    await Orchestrator().run(SANDLOT_PROMPT)

    assert read_pipeline_state() is None


@pytest.mark.asyncio
async def test_orchestrator_persists_state_during_run(
    pipeline_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State is written at the batch boundary before being cleared on success."""
    captured_phases: list[str] = []

    original_write = write_pipeline_state

    def capture_write(state: PipelineState) -> None:
        captured_phases.append(state.phase)
        original_write(state)

    monkeypatch.setattr(
        "scene_scout.orchestrator.write_pipeline_state",
        capture_write,
    )

    await Orchestrator().run(SANDLOT_PROMPT)

    assert captured_phases == ["batch_submitted", "phase_2"]

    # Final on-disk state cleared after success
    assert read_pipeline_state() is None
