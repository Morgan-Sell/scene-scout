"""
Tests for the orchestrator skeleton, PipelineState persistence, and seen_entries
cache integration.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scene_scout.cache_config import SEEN_ENTRIES_TTL_DAYS
from scene_scout.curator_config import load_curator_config
from scene_scout.logging import get_logger
from scene_scout.models.curated import CuratorResult
from scene_scout.models.email import EmailComposerResult
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.event import (
    EventCandidate,
    EventCandidateLLMOutput,
    NormalizedEvent,
)
from scene_scout.models.feed import RawFeedEntry
from scene_scout.models.ranking import RankedEvent
from scene_scout.models.user import UserProfile
from scene_scout.orchestrator import (
    Orchestrator,
    PipelineResult,
    PipelineRunError,
    PipelineState,
    PreEnrichmentFilterResult,
    _batch_custom_id,
    _batch_results_for_agent,
    _cap_extraction_entries,
    _partition_entries_by_seen_cache,
    _poll_enrichment_batch,
    _resolve_user_profile,
    _seen_entries_hit_rate_pct,
    _store_seen_entries_after_normalization,
    _top_recommendation_rows,
    clear_pipeline_state,
    compute_entry_hash,
    read_pipeline_state,
    write_pipeline_state,
)
from scene_scout.orchestrator_config import UatRunOptions
from scene_scout.services.batch import BatchRequest, BatchResultItem, BatchResults
from scene_scout.services.cache import CacheService
from tests.conftest import TEST_RUN_ID

SANDLOT_PROMPT = (
    "Find me sandlot events near the Beast's backyard — "
    "baseball, pool parties, and legends of the Great Bambino."
)

SANDLOT_FEED_ID = "sandlot-pickup-league"
ENTRY_LINK = "https://example.com/great-bambino-night"
ENTRY_PUBLISHED = "Fri, 06 Jun 2025 20:00:00 +0000"


def _uat_profile() -> UserProfile:
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    return UserProfile(
        user_id="smalls-uat",
        name="Smalls",
        email="smalls@example.com",
        stated_interests=["baseball"],
        vibe_preferences=["outdoor"],
        category_weights={"baseball": 0.9},
        created_at=now,
        last_updated=now,
    )


def _empty_email_result(run_id: str) -> EmailComposerResult:
    return EmailComposerResult(
        html="<html><body>Allegra preview</body></html>",
        subject=f"[UAT {run_id}] This week from Allegra — Smalls",
        preview_path=Path(f"output/uat_{run_id}/email_preview.html"),
        sent=False,
    )


@pytest.fixture(autouse=True)
def mock_pipeline_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep orchestrator tests offline while exercising real wiring."""
    monkeypatch.setattr(
        "scene_scout.orchestrator.feed_scout.run",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator._resolve_user_profile",
        AsyncMock(return_value=_uat_profile()),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.ranking.run",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.sellout_risk.run",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.recommendation_curator.run",
        AsyncMock(
            return_value=CuratorResult(
                recommendations=[],
                below_minimum=True,
                curator_config=load_curator_config(),
            )
        ),
    )

    async def _email_side_effect(
        recs: object,
        profile: object,
        run_id: str,
        **kwargs: object,
    ) -> EmailComposerResult:
        return _empty_email_result(run_id)

    monkeypatch.setattr(
        "scene_scout.orchestrator.email_composer.run",
        AsyncMock(side_effect=_email_side_effect),
    )


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profiles_path = tmp_path / "vol-profiles"
    profiles_path.mkdir()
    monkeypatch.setattr(
        "scene_scout.agents.user_preference.vol_profiles_dir",
        lambda: profiles_path,
    )
    return profiles_path


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


def test_store_seen_entries_after_partial_normalization(cache_db: Path) -> None:
    """Regression: UAT-scale partial normalization must not zip strict mismatch."""
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    candidate_count = 120
    normalized_count = 19

    source_entries: list[RawFeedEntry] = []
    candidates: list[EventCandidate] = []
    normalized_events: list[NormalizedEvent] = []

    for index in range(candidate_count):
        link = f"https://example.com/event-{index}"
        published = f"Fri, {index:02d} Jun 2025 20:00:00 +0000"
        source_entries.append(
            _raw_entry(
                link=link,
                published_raw=published,
                title=f"Sandlot Event {index}",
            )
        )
        candidates.append(
            EventCandidate.from_llm_output(
                EventCandidateLLMOutput.model_validate(
                    {
                        "title": f"Sandlot Event {index}",
                        "date": "Sat, Jun 7 2025",
                        "time": "6:00 PM",
                        "venue": "The Sandlot",
                        "city": "Los Angeles",
                        "url": link,
                        "is_event": True,
                        "extraction_confidence": 0.9,
                    }
                ),
                source_feed=SANDLOT_FEED_ID,
                run_id=TEST_RUN_ID,
                extracted_at=datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc),
            )
        )

    for index in range(normalized_count):
        link = f"https://example.com/event-{index}"
        normalized_events.append(
            _normalized_event(
                id=f"sandlot-event-{index}",
                title=f"Sandlot Event {index}",
                url=link,
            )
        )

    _store_seen_entries_after_normalization(
        cache,
        candidates,
        normalized_events,
        source_entries,
    )

    stored_hashes = {
        compute_entry_hash(entry) for entry in source_entries[:normalized_count]
    }
    for entry in source_entries[normalized_count:]:
        assert cache.get_seen_entry(SANDLOT_FEED_ID, compute_entry_hash(entry)) is None

    for entry in source_entries[:normalized_count]:
        entry_hash = compute_entry_hash(entry)
        assert entry_hash in stored_hashes
        assert cache.get_seen_entry(SANDLOT_FEED_ID, entry_hash) is not None


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
    assert result.pre_enrichment_discard_low_information == 0
    assert result.pre_enrichment_discard_outside_week == 0
    assert result.pre_enrichment_discard_exclude_window == 0
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
        "scene_scout.orchestrator.feed_scout.run",
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
        "scene_scout.orchestrator.feed_scout.run",
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
async def test_orchestrator_partial_normalization_stores_seen_entries_without_crash(
    pipeline_state_dir: Path,
    cache_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    source_entries = [
        _raw_entry(
            link=f"https://example.com/event-{index}",
            published_raw=f"Fri, {index:02d} Jun 2025 20:00:00 +0000",
            title=f"Sandlot Event {index}",
        )
        for index in range(5)
    ]
    candidates = [
        EventCandidate.from_llm_output(
            EventCandidateLLMOutput.model_validate(
                {
                    "title": f"Sandlot Event {index}",
                    "date": "Sat, Jun 7 2025",
                    "time": "6:00 PM",
                    "venue": "The Sandlot",
                    "city": "Los Angeles",
                    "url": f"https://example.com/event-{index}",
                    "is_event": True,
                    "extraction_confidence": 0.9,
                }
            ),
            source_feed=SANDLOT_FEED_ID,
            run_id=TEST_RUN_ID,
            extracted_at=datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc),
        )
        for index in range(5)
    ]
    normalized_subset = [
        _normalized_event(
            id=f"sandlot-event-{index}",
            title=f"Sandlot Event {index}",
            url=f"https://example.com/event-{index}",
        )
        for index in range(2)
    ]

    monkeypatch.setattr(
        "scene_scout.orchestrator.feed_scout.run",
        AsyncMock(return_value=(source_entries, [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_extraction.run",
        AsyncMock(return_value=candidates),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_normalization.run",
        AsyncMock(return_value=normalized_subset),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.CacheService",
        lambda run_id, db_path=None: cache,
    )

    result = await Orchestrator().run(SANDLOT_PROMPT)

    assert result.extraction_candidates == 5
    assert result.normalized_events == 2
    assert (
        cache.get_seen_entry(
            SANDLOT_FEED_ID,
            compute_entry_hash(source_entries[0]),
        )
        == normalized_subset[0]
    )
    assert (
        cache.get_seen_entry(
            SANDLOT_FEED_ID,
            compute_entry_hash(source_entries[1]),
        )
        == normalized_subset[1]
    )
    assert (
        cache.get_seen_entry(
            SANDLOT_FEED_ID,
            compute_entry_hash(source_entries[2]),
        )
        is None
    )


@pytest.mark.asyncio
async def test_orchestrator_writes_uat_checkpoints_through_normalization(
    pipeline_state_dir: Path,
    cache_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    source_entry = _raw_entry()
    candidate = _event_candidate()
    normalized = _normalized_event()

    monkeypatch.setattr(
        "scene_scout.orchestrator.feed_scout.run",
        AsyncMock(return_value=([source_entry], [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_extraction.run",
        AsyncMock(return_value=[candidate]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_normalization.run",
        AsyncMock(return_value=[normalized]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.deduplication.run",
        AsyncMock(side_effect=RuntimeError("dedupe failed")),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.CacheService",
        lambda run_id, db_path=None: cache,
    )

    with pytest.raises(PipelineRunError) as exc_info:
        await Orchestrator().run(SANDLOT_PROMPT, uat_output_base=tmp_path)

    result = exc_info.value.result
    run_dir = tmp_path / f"uat_{result.run_id}"
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))

    assert checkpoint["run_id"] == result.run_id
    assert checkpoint["last_completed_stage"] == "normalization"
    assert checkpoint["counts"]["raw_entries"] == 1
    assert checkpoint["counts"]["extraction_candidates"] == 1
    assert checkpoint["counts"]["normalized_events"] == 1
    assert result.last_completed_stage == "normalization"


@pytest.mark.asyncio
async def test_orchestrator_expired_seen_entry_triggers_re_extraction(
    pipeline_state_dir: Path,
    cache_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    source_entry = _raw_entry()
    cached_event = _normalized_event()
    candidate = _event_candidate()
    normalized = _normalized_event()
    entry_hash = compute_entry_hash(source_entry)
    base = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

    with patch("scene_scout.services.cache._utc_now", return_value=base):
        cache.set_seen_entry(SANDLOT_FEED_ID, entry_hash, cached_event)

    mock_extraction = AsyncMock(return_value=[candidate])
    monkeypatch.setattr(
        "scene_scout.orchestrator.feed_scout.run",
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

    with patch(
        "scene_scout.services.cache._utc_now",
        return_value=base + timedelta(days=SEEN_ENTRIES_TTL_DAYS, seconds=1),
    ):
        result = await Orchestrator().run(SANDLOT_PROMPT)
        entry_hash = compute_entry_hash(source_entry)
        assert cache.get_seen_entry(SANDLOT_FEED_ID, entry_hash) == normalized

    assert result.seen_entries_cache_hits == 0
    assert result.seen_entries_cache_misses == 1
    assert result.extraction_candidates == 1
    mock_extraction.assert_awaited_once()


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


def test_batch_custom_id_and_split_results() -> None:
    custom_id = _batch_custom_id("vibe_classifier", "sandlot-game-1993")
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id="talent_scout:sandlot-game-1993",
                content='{"performers": []}',
                success=True,
            ),
            BatchResultItem(
                custom_id="vibe_classifier:sandlot-game-1993",
                content='{"vibe_tags": ["outdoor", "social"]}',
                success=True,
            ),
        ],
    )

    assert custom_id == "vibe_classifier:sandlot-game-1993"
    vibe_only = _batch_results_for_agent(batch_results, "vibe_classifier")

    assert len(vibe_only.results) == 1
    assert vibe_only.results[0].custom_id == "sandlot-game-1993"


@pytest.mark.asyncio
async def test_poll_enrichment_batch_waits_between_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_mock = AsyncMock()
    monkeypatch.setattr("scene_scout.orchestrator.asyncio.sleep", sleep_mock)
    poll_mock = AsyncMock(
        side_effect=[
            BatchResults(batch_id="batch-1", status="processing"),
            BatchResults(batch_id="batch-1", status="completed", results=[]),
        ]
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.get_batch_strategy",
        lambda: type("Strategy", (), {"poll": poll_mock})(),
    )

    results = await _poll_enrichment_batch("batch-1", run_id=TEST_RUN_ID)

    assert results.status == "completed"
    sleep_mock.assert_awaited_once()
    assert poll_mock.await_count == 2


@pytest.mark.asyncio
async def test_orchestrator_writes_batch_id_to_pipeline_state(
    pipeline_state_dir: Path,
    cache_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future_start = datetime.now(timezone.utc) + timedelta(days=3)
    filtered_event = _normalized_event(
        start_datetime=future_start,
        description_quality_score=0.8,
        low_information=False,
    )
    batch_request = BatchRequest(
        custom_id="vibe_classifier:sandlot-game-1993",
        prompt="Classify the sandlot vibe.",
        system="Return JSON.",
        agent_name="vibe_classifier",
    )
    completed_batch = BatchResults(
        batch_id="batch-sandlot-1993",
        status="completed",
        results=[
            BatchResultItem(
                custom_id="vibe_classifier:sandlot-game-1993",
                content='{"vibe_tags": ["outdoor", "social"]}',
                success=True,
            )
        ],
    )

    monkeypatch.setattr(
        "scene_scout.orchestrator._collect_enrichment_batch_requests",
        AsyncMock(return_value=([batch_request], [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator._poll_enrichment_batch",
        AsyncMock(return_value=completed_batch),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.get_batch_strategy",
        lambda: type(
            "Strategy",
            (),
            {"submit": AsyncMock(return_value="batch-sandlot-1993")},
        )(),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator._apply_enrichment_batch",
        AsyncMock(
            return_value=[EnrichedEvent.from_normalized(filtered_event)],
        ),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.deduplication.run",
        AsyncMock(return_value=[filtered_event]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.description_quality.run",
        AsyncMock(return_value=[filtered_event]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.apply_pre_enrichment_filter",
        lambda events, run_id, **kwargs: PreEnrichmentFilterResult(
            events=[filtered_event],
            discards={
                "low_information": 0,
                "outside_coming_week": 0,
                "in_exclude_window": 0,
            },
        ),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.CacheService",
        lambda run_id, db_path=None: CacheService(run_id=run_id, db_path=cache_db),
    )

    captured_states: list[PipelineState] = []

    def capture_write(state: PipelineState) -> None:
        captured_states.append(
            PipelineState.from_json(state.to_json()),
        )
        write_pipeline_state(state)

    monkeypatch.setattr(
        "scene_scout.orchestrator.write_pipeline_state",
        capture_write,
    )

    result = await Orchestrator().run(SANDLOT_PROMPT)

    assert result.enriched_events == 1
    batch_submitted = next(
        state for state in captured_states if state.phase == "batch_submitted"
    )
    assert batch_submitted.batch_id == "batch-sandlot-1993"
    assert len(batch_submitted.filtered_events) == 1
    assert read_pipeline_state() is None


def test_top_recommendation_rows_includes_source_coverage() -> None:
    event = _normalized_event(source_count=2)
    ranked = RankedEvent(
        event=EnrichedEvent.from_normalized(event),
        score=0.82,
        score_breakdown={
            "category_fit": 0.8,
            "vibe_fit": 0.7,
            "semantic_similarity": 0.5,
            "performer_affinity": 0.4,
            "location": 0.6,
            "novelty": 0.9,
            "source_quality": 0.7,
            "source_coverage": 0.67,
            "description_quality": 0.75,
        },
        explanation="Great sandlot energy.",
        run_id=TEST_RUN_ID,
    )

    rows = _top_recommendation_rows([ranked])

    assert rows[0]["title"] == "The Great Bambino Night"
    assert rows[0]["source_count"] == 2
    assert rows[0]["source_coverage"] == 0.67


@pytest.mark.asyncio
async def test_resolve_user_profile_loads_existing(
    profiles_dir: Path,
) -> None:
    profile = _uat_profile()
    (profiles_dir / "profile.json").write_text(
        profile.model_dump_json(),
        encoding="utf-8",
    )

    loaded = await _resolve_user_profile(SANDLOT_PROMPT, TEST_RUN_ID)

    assert loaded.user_id == profile.user_id


@pytest.mark.asyncio
async def test_resolve_user_profile_parses_cold_start_when_missing(
    profiles_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER_EMAIL", "smalls@example.com")
    monkeypatch.setenv("USER_NAME", "Smalls")
    parsed = _uat_profile()
    with patch(
        "scene_scout.orchestrator.user_preference.parse_cold_start",
        AsyncMock(return_value=parsed),
    ) as mock_parse:
        loaded = await _resolve_user_profile(SANDLOT_PROMPT, TEST_RUN_ID)

    assert loaded == parsed
    mock_parse.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_calls_email_composer_and_records_preview(
    pipeline_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email_mock = AsyncMock(
        return_value=EmailComposerResult(
            html="<html><body><p>Allegra</p></body></html>",
            subject="[UAT test] Allegra",
            preview_path=Path("output/uat_test/email_preview.html"),
            sent=False,
        )
    )
    monkeypatch.setattr("scene_scout.orchestrator.email_composer.run", email_mock)

    result = await Orchestrator().run(SANDLOT_PROMPT)

    email_mock.assert_awaited_once()
    assert result.email_preview_path == "output/uat_test/email_preview.html"
    assert result.email_sent is False


def test_cap_extraction_entries_returns_all_when_under_cap() -> None:
    entries = [_raw_entry(link=f"https://example.com/{index}") for index in range(3)]
    capped = _cap_extraction_entries(entries, 25, get_logger("test"))
    assert capped == entries


def test_cap_extraction_entries_truncates_to_cap() -> None:
    entries = [_raw_entry(link=f"https://example.com/{index}") for index in range(5)]
    capped = _cap_extraction_entries(entries, 2, get_logger("test"))
    assert len(capped) == 2
    assert capped == entries[:2]


@pytest.mark.asyncio
async def test_orchestrator_stop_after_feeds_skips_extraction(
    pipeline_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_entry = _raw_entry()
    mock_extraction = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "scene_scout.orchestrator.feed_scout.run",
        AsyncMock(return_value=([source_entry], [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_extraction.run",
        mock_extraction,
    )

    result = await Orchestrator().run(
        SANDLOT_PROMPT,
        uat_options=UatRunOptions(stop_after="feeds"),
    )

    mock_extraction.assert_not_awaited()
    assert result.raw_entries == 1
    assert result.extraction_candidates == 0
    assert result.last_completed_stage == "feeds"


@pytest.mark.asyncio
async def test_orchestrator_max_extraction_caps_cache_miss_entries(
    pipeline_state_dir: Path,
    cache_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheService(run_id=TEST_RUN_ID, db_path=cache_db)
    entries = [
        _raw_entry(
            link=f"https://example.com/event-{index}",
            title=f"Event {index}",
        )
        for index in range(5)
    ]
    mock_extraction = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "scene_scout.orchestrator.feed_scout.run",
        AsyncMock(return_value=(entries, [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.event_extraction.run",
        mock_extraction,
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.CacheService",
        lambda run_id, db_path=None: cache,
    )

    await Orchestrator().run(
        SANDLOT_PROMPT,
        uat_options=UatRunOptions(max_extraction=2, stop_after="extract"),
    )

    mock_extraction.assert_awaited_once()
    passed_entries = mock_extraction.await_args.args[0]
    assert len(passed_entries) == 2
    assert passed_entries == entries[:2]
