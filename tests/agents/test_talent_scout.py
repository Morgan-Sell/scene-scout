"""
Tests for the Talent Scout agent.

Covers performer cache resolution, batch application, confidence-based summary
stripping, validation-error degradation, and end-to-end run() with mocked batch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from scene_scout.agents import talent_scout
from scene_scout.models.enrichment import PerformerInfo
from scene_scout.models.event import NormalizedEvent, compute_normalized_event_id
from scene_scout.services.batch import (
    BatchResultItem,
    BatchResults,
    ConcurrentBatchStrategy,
)
from scene_scout.services.cache import CacheService
from scene_scout.talent_scout_config import CONFIDENCE_SUMMARY_THRESHOLD
from tests.conftest import TEST_RUN_ID

BENNY_PERFORMER_JSON = (
    '{"performers": [{"name": "Benny Rodriguez", "entity_type": "other", '
    '"genre_tags": ["baseball"], "one_line_summary": "The Jet", '
    '"confidence": 0.95, "affinity_score": 0.88}]}'
)

BENNY_PERFORMER_JSON_NO_TAGS = (
    '{"performers": [{"name": "Benny Rodriguez", "entity_type": "other", '
    '"genre_tags": [], "one_line_summary": "The Jet", '
    '"confidence": 0.95, "affinity_score": 0.88}]}'
)
SANDLOT_FEED = "sandlot-pickup-league"
START = datetime(2025, 6, 7, 18, 0, tzinfo=timezone.utc)
EVENT_ID = compute_normalized_event_id(
    "Night with Benny Rodriguez",
    "Sat, Jun 7 2025",
    "The Sandlot",
)


def _event(**overrides: object) -> NormalizedEvent:
    payload = {
        "id": EVENT_ID,
        "title": "Night with Benny Rodriguez",
        "start_datetime": START,
        "venue": "The Sandlot",
        "city": "Los Angeles",
        "url": "https://example.com/benny-night",
        "is_free": True,
        "description": "Featuring Benny Rodriguez at the sandlot \
            under the floodlights.",
        "categories": ["Sports"],
        "source_feeds": [SANDLOT_FEED],
        "source_count": 1,
        "best_source_feed": SANDLOT_FEED,
        "source_quality_score": 0.8,
        "run_id": TEST_RUN_ID,
        "normalized_at": START,
    }
    payload.update(overrides)
    return NormalizedEvent.model_validate(payload)


@pytest.fixture
def cache() -> CacheService:
    return CacheService(run_id=TEST_RUN_ID)


def test_normalize_performer_name_collapses_whitespace_and_case() -> None:
    assert talent_scout.normalize_performer_name("  Benny   Rodriguez ") == (
        "benny rodriguez"
    )


def test_prepare_performer_for_cache_strips_summary_below_threshold() -> None:
    performer = PerformerInfo(
        name="Benny Rodriguez",
        entity_type="athlete",
        one_line_summary="The Jet",
        confidence=CONFIDENCE_SUMMARY_THRESHOLD - 0.01,
        affinity_score=0.8,
    )

    prepared = talent_scout.prepare_performer_for_cache(performer)

    assert prepared.one_line_summary is None
    assert prepared.confidence == performer.confidence


def test_prepare_performer_for_cache_keeps_summary_at_threshold() -> None:
    performer = PerformerInfo(
        name="Benny Rodriguez",
        entity_type="athlete",
        one_line_summary="The Jet",
        confidence=CONFIDENCE_SUMMARY_THRESHOLD,
        affinity_score=0.8,
    )

    prepared = talent_scout.prepare_performer_for_cache(performer)

    assert prepared.one_line_summary == "The Jet"


def test_resolve_performers_from_cache_returns_all_cached_performers(
    cache: CacheService,
) -> None:
    cache.set_performer(
        "benny rodriguez",
        PerformerInfo(
            name="Benny Rodriguez",
            entity_type="other",
            one_line_summary="The Jet",
            confidence=0.95,
            affinity_score=0.9,
        ),
    )
    event = _event()

    performers = talent_scout.resolve_performers_from_cache(
        event.title,
        event.description,
        cache,
    )

    assert performers is not None
    assert len(performers) == 1
    assert performers[0].name == "Benny Rodriguez"


def test_resolve_performers_from_cache_returns_none_when_any_performer_missing(
    cache: CacheService,
) -> None:
    event = _event()

    assert (
        talent_scout.resolve_performers_from_cache(
            event.title,
            event.description,
            cache,
        )
        is None
    )


def test_merge_llm_performers_with_cache_uses_cache_and_stores_new_performers(
    cache: CacheService,
) -> None:
    cache.set_performer(
        "benny rodriguez",
        PerformerInfo(
            name="Benny Rodriguez",
            entity_type="other",
            one_line_summary="Cached Jet",
            confidence=0.95,
            affinity_score=0.9,
        ),
    )
    llm_output = talent_scout.TalentScoutLLMOutput.model_validate(
        {
            "performers": [
                {
                    "name": "Benny Rodriguez",
                    "entity_type": "other",
                    "genre_tags": [],
                    "one_line_summary": "Fresh summary",
                    "confidence": 0.95,
                    "affinity_score": 0.9,
                },
                {
                    "name": "Smalls",
                    "entity_type": "other",
                    "genre_tags": [],
                    "one_line_summary": "The new kid",
                    "confidence": 0.5,
                    "affinity_score": 0.4,
                },
            ]
        }
    )

    performers = talent_scout.merge_llm_performers_with_cache(
        llm_output.performers,
        cache,
    )

    assert performers[0].one_line_summary == "Cached Jet"
    assert performers[1].name == "Smalls"
    assert performers[1].one_line_summary is None
    stored = cache.get_performer("smalls")
    assert stored is not None
    assert stored.one_line_summary is None


@pytest.mark.asyncio
async def test_apply_batch_results_returns_empty_performers_on_validation_error(
    cache: CacheService,
) -> None:
    event = _event()
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content='{"performers": [{"name": "Benny"}]}',
                success=True,
            )
        ],
    )

    enriched = await talent_scout.apply_batch_results(
        [event],
        batch_results,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert enriched[0].performers == []
    assert enriched[0].top_performer_affinity == 0.0


@pytest.mark.asyncio
async def test_apply_batch_results_populates_performers(cache: CacheService) -> None:
    event = _event()
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content=BENNY_PERFORMER_JSON,
                success=True,
            )
        ],
    )

    enriched = await talent_scout.apply_batch_results(
        [event],
        batch_results,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert len(enriched[0].performers) == 1
    assert enriched[0].performers[0].name == "Benny Rodriguez"
    assert enriched[0].top_performer_affinity == 0.88


@pytest.mark.asyncio
async def test_run_uses_performer_cache_without_batch(
    cache: CacheService,
) -> None:
    cache.set_performer(
        "benny rodriguez",
        PerformerInfo(
            name="Benny Rodriguez",
            entity_type="other",
            one_line_summary="The Jet",
            confidence=0.95,
            affinity_score=0.91,
        ),
    )
    mock_strategy = AsyncMock()

    enriched = await talent_scout.run(
        [_event()],
        ["baseball", "legends"],
        TEST_RUN_ID,
        cache=cache,
        batch_strategy=mock_strategy,
    )

    assert len(enriched[0].performers) == 1
    mock_strategy.submit.assert_not_called()


@pytest.mark.asyncio
async def test_run_submits_uncached_events_to_batch_strategy(
    cache: CacheService,
) -> None:
    event = _event()
    strategy = ConcurrentBatchStrategy(model="gpt-4o-mini")
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content=BENNY_PERFORMER_JSON_NO_TAGS,
                success=True,
            )
        ],
    )

    enriched = await talent_scout.run(
        [event],
        ["baseball"],
        TEST_RUN_ID,
        cache=cache,
        batch_strategy=strategy,
        batch_results=batch_results,
    )

    assert enriched[0].performers[0].name == "Benny Rodriguez"


@pytest.mark.asyncio
async def test_run_returns_empty_performers_when_no_named_performer_signal(
    cache: CacheService,
) -> None:
    event = _event(
        title="Community Night",
        description="A casual evening at the park and local food vendors.",
    )
    mock_strategy = AsyncMock()

    enriched = await talent_scout.run(
        [event],
        [],
        TEST_RUN_ID,
        cache=cache,
        batch_strategy=mock_strategy,
    )

    assert enriched[0].performers == []
    mock_strategy.submit.assert_not_called()
