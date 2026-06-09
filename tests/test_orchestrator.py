"""
Tests for the orchestrator skeleton, PipelineState persistence, and seen_entries
cache integration.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scene_scout.logging import get_logger
from scene_scout.models.event import (
    EventCandidate,
    EventCandidateLLMOutput,
    NormalizedEvent,
)
from scene_scout.models.feed import RawFeedEntry
from scene_scout.orchestrator import (
    Orchestrator,
    PipelineResult,
    PipelineState,
    _partition_entries_by_seen_cache,
    _seen_entries_hit_rate_pct,
    _store_seen_entries_after_normalization,
    clear_pipeline_state,
    compute_entry_hash,
    read_pipeline_state,
    write_pipeline_state,
)
from scene_scout.services.cache import CacheService
from tests.conftest import TEST_RUN_ID

SANDLOT_PROMPT = (
    "Find me sandlot events near the Beast's backyard — "
    "baseball, pool parties, and legends of the Great Bambino."
)

SANDLOT_FEED_ID = "sandlot-pickup-league"
ENTRY_LINK = "https://example.com/great-bambino-night"
ENTRY_PUBLISHED = "Fri, 06 Jun 2025 20:00:00 +0000"


@pytest.fixture
def pipeline_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect vol-pipeline-state to a temp dir for isolation."""
    state_dir = tmp_path / "squints-pipeline-locker"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VOL_PIPELINE_STATE_DIR", str(state_dir))
    return state_dir


@pytest.fixture
def cache_db(tmp_path: Path) -> Path:
    return tmp_path / "wendy-peffercorn-locker" / "cache.db"


def _raw_entry(**overrides: object) -> RawFeedEntry:
    payload = {
        "feed_id": SANDLOT_FEED_ID,
        "feed_name": "Mr. Mertle's Events Feed",
        "source_url": "https://example.com/sandlot-feed.xml",
        "run_id": TEST_RUN_ID,
        "title": "The Great Bambino Night",
        "link": ENTRY_LINK,
        "description": "Sandlot legends retell the Babe Ruth story.",
        "published_raw": ENTRY_PUBLISHED,
        "categories": ["Baseball"],
        "fetched_at": datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return RawFeedEntry.model_validate(payload)


def _normalized_event(**overrides: object) -> NormalizedEvent:
    payload = {
        "id": "sandlot-game-1993",
        "title": "The Great Bambino Night",
        "start_datetime": datetime(2025, 6, 6, 20, 0, tzinfo=timezone.utc),
        "venue": "The Sandlot",
        "city": "Los Angeles",
        "url": ENTRY_LINK,
        "is_free": True,
        "description": "Legends retell the Babe Ruth story.",
        "source_feeds": [SANDLOT_FEED_ID],
        "best_source_feed": SANDLOT_FEED_ID,
        "run_id": TEST_RUN_ID,
        "normalized_at": datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return NormalizedEvent.model_validate(payload)


def _event_candidate(**overrides: object) -> EventCandidate:
    llm_output = EventCandidateLLMOutput.model_validate(
        {
            "title": "The Great Bambino Night",
            "date": "Fri, 06 Jun 2025",
            "time": "8:00 PM",
            "venue": "The Sandlot",
            "city": "Los Angeles",
            "url": ENTRY_LINK,
            "is_event": True,
            "extraction_confidence": 0.9,
        }
    )
    return EventCandidate.from_llm_output(
        llm_output,
        source_feed=SANDLOT_FEED_ID,
        run_id=TEST_RUN_ID,
        extracted_at=datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc),
    )


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


def test_compute_entry_hash_is_stable_for_link_and_published_raw() -> None:
    entry = _raw_entry()
    expected = compute_entry_hash(entry)

    assert expected == compute_entry_hash(_raw_entry())
    assert len(expected) == 64


def test_compute_entry_hash_treats_null_fields_as_empty_strings() -> None:
    with_fields = compute_entry_hash(_raw_entry())
    without_fields = compute_entry_hash(
        _raw_entry(link=None, published_raw=None),
    )

    assert with_fields != without_fields


def test_seen_entries_hit_rate_pct() -> None:
    assert _seen_entries_hit_rate_pct(0, 0) == 0.0
    assert _seen_entries_hit_rate_pct(2, 8) == 20.0
    assert _seen_entries_hit_rate_pct(3, 7) == 30.0


def test_partition_entries_by_seen_cache_splits_hits_and_misses(
    cache_db: Path,
) -> None:
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    cached_event = _normalized_event()
    entry_hash = compute_entry_hash(_raw_entry())
    cache.set_seen_entry(SANDLOT_FEED_ID, entry_hash, cached_event)

    cached_entry = _raw_entry()
    miss_entry = _raw_entry(
        link="https://example.com/pool-party",
        published_raw="Sun, 08 Jun 2025 18:00:00 +0000",
        title="Pool Party at the Rec Center",
    )
    logger = get_logger("orchestrator", run_id=TEST_RUN_ID)

    cached_events, for_extraction, hits, misses = _partition_entries_by_seen_cache(
        [cached_entry, miss_entry],
        cache,
        logger,
    )

    assert hits == 1
    assert misses == 1
    assert len(cached_events) == 1
    assert cached_events[0] == cached_event
    assert for_extraction == [miss_entry]


def test_store_seen_entries_after_normalization(cache_db: Path) -> None:
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    source_entry = _raw_entry()
    candidate = _event_candidate()
    normalized = _normalized_event()

    _store_seen_entries_after_normalization(
        cache,
        [candidate],
        [normalized],
        [source_entry],
    )

    entry_hash = compute_entry_hash(source_entry)
    assert cache.get_seen_entry(SANDLOT_FEED_ID, entry_hash) == normalized


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
    assert result.seen_entries_cache_misses == 0
    assert result.seen_entries_hit_rate_pct == 0.0
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
async def test_orchestrator_seen_entries_cache_hit_bypasses_extraction(
    pipeline_state_dir: Path,
    cache_db: Path,
    logs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    source_entry = _raw_entry()
    cached_event = _normalized_event()
    cache.set_seen_entry(
        SANDLOT_FEED_ID,
        compute_entry_hash(source_entry),
        cached_event,
    )

    mock_extraction = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "scene_scout.orchestrator._stub_feed_scout",
        AsyncMock(return_value=([source_entry], [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_extraction.run",
        mock_extraction,
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.CacheService",
        lambda run_id, db_path=None: cache,
    )

    result = await Orchestrator().run(SANDLOT_PROMPT)

    assert result.raw_entries == 1
    assert result.seen_entries_cache_hits == 1
    assert result.seen_entries_cache_misses == 0
    assert result.seen_entries_hit_rate_pct == 100.0
    assert result.extraction_candidates == 0
    assert result.normalized_events == 1
    mock_extraction.assert_not_awaited()

    all_log_entries: list[dict[str, object]] = []
    for log_file in logs_dir.glob("*.jsonl"):
        all_log_entries.extend(
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
            if line.strip()
        )
    assert any(
        entry.get("message") == "seen_entries cache hit" for entry in all_log_entries
    )


@pytest.mark.asyncio
async def test_orchestrator_seen_entries_cache_miss_runs_extraction_and_stores(
    pipeline_state_dir: Path,
    cache_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    source_entry = _raw_entry()
    candidate = _event_candidate()
    normalized = _normalized_event()

    mock_extraction = AsyncMock(return_value=[candidate])
    monkeypatch.setattr(
        "scene_scout.orchestrator._stub_feed_scout",
        AsyncMock(return_value=([source_entry], [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_extraction.run",
        mock_extraction,
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_normalization.run",
        AsyncMock(return_value=[normalized]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.CacheService",
        lambda run_id, db_path=None: cache,
    )

    result = await Orchestrator().run(SANDLOT_PROMPT)

    assert result.raw_entries == 1
    assert result.seen_entries_cache_hits == 0
    assert result.seen_entries_cache_misses == 1
    assert result.seen_entries_hit_rate_pct == 0.0
    assert result.extraction_candidates == 1
    assert result.normalized_events == 1
    mock_extraction.assert_awaited_once()

    entry_hash = compute_entry_hash(source_entry)
    assert cache.get_seen_entry(SANDLOT_FEED_ID, entry_hash) == normalized


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
