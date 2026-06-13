"""
Tests for the Neighborhood Scout agent.

Covers venue cache resolution, Mode A/B geocoding paths, confidence thresholding,
batch application, and end-to-end run() with mocked geocoding and batch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from scene_scout.agents import neighborhood_scout
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.event import NormalizedEvent, compute_normalized_event_id
from scene_scout.neighborhood_scout_config import (
    MODE_GEO_ASSISTED,
    MODE_LLM_FALLBACK,
    NEIGHBORHOOD_CONFIDENCE_THRESHOLD,
)
from scene_scout.services.batch import (
    BatchResultItem,
    BatchResults,
    ConcurrentBatchStrategy,
)
from scene_scout.services.cache import CacheService
from scene_scout.services.geocoding import venue_cache_key
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
START = datetime(2025, 6, 7, 18, 0, tzinfo=timezone.utc)
EVENT_ID = compute_normalized_event_id(
    "Sunset Sandlot Game",
    "Sat, Jun 7 2025",
    "The Sandlot",
)
VENUE = "The Sandlot"
CITY = "Los Angeles"
VENUE_KEY = venue_cache_key(VENUE, CITY)
COORDINATES = (34.0522, -118.2437)
POIS = [{"name": "Treehouse Cafe", "type": "cafe", "lat": 34.0525, "lon": -118.244}]
CONTEXT_JSON = (
    '{"neighborhood_context": "A relaxed suburban pocket with a nearby cafe.", '
    '"neighborhood_confidence": 0.82}'
)
LOW_CONFIDENCE_JSON = (
    '{"neighborhood_context": "Hard to say much about this area.", '
    '"neighborhood_confidence": 0.35}'
)


def _normalized_event(**overrides: object) -> NormalizedEvent:
    payload = {
        "id": EVENT_ID,
        "title": "Sunset Sandlot Game",
        "start_datetime": START,
        "venue": VENUE,
        "city": CITY,
        "neighborhood": "San Fernando Valley",
        "url": "https://example.com/sunset-sandlot-game",
        "is_free": True,
        "description": "Pickup baseball under the floodlights.",
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


def _enriched_event(**overrides: object) -> EnrichedEvent:
    base = EnrichedEvent.from_normalized(_normalized_event())
    if overrides:
        return base.model_copy(update=overrides)
    return base


@pytest.fixture
def cache() -> CacheService:
    return CacheService(run_id=TEST_RUN_ID)


def test_format_poi_list_renders_structured_lines() -> None:
    rendered = neighborhood_scout.format_poi_list(POIS)

    assert "- Treehouse Cafe (cafe)" in rendered


def test_apply_confidence_threshold_clears_context_below_cutoff() -> None:
    context, confidence = neighborhood_scout.apply_confidence_threshold(
        "Some context",
        NEIGHBORHOOD_CONFIDENCE_THRESHOLD - 0.01,
    )

    assert context is None
    assert confidence == NEIGHBORHOOD_CONFIDENCE_THRESHOLD - 0.01


def test_resolve_neighborhood_from_cache_returns_cached_context(
    cache: CacheService,
) -> None:
    cache.set_venue(
        VENUE_KEY,
        coordinates=COORDINATES,
        neighborhood_context="Classic suburban sandlot vibes.",
        neighborhood_confidence=0.82,
    )

    resolved = neighborhood_scout.resolve_neighborhood_from_cache(VENUE_KEY, cache)

    assert resolved == ("Classic suburban sandlot vibes.", 0.82, COORDINATES)


def test_resolve_neighborhood_from_cache_clears_low_confidence_context(
    cache: CacheService,
) -> None:
    cache.set_venue(
        VENUE_KEY,
        neighborhood_context="Maybe useful",
        neighborhood_confidence=0.4,
    )

    resolved = neighborhood_scout.resolve_neighborhood_from_cache(VENUE_KEY, cache)

    assert resolved == (None, 0.4, None)


@pytest.mark.asyncio
async def test_prepare_neighborhood_job_uses_mode_a_when_geocoding_succeeds(
    cache: CacheService,
) -> None:
    with (
        patch(
            "scene_scout.agents.neighborhood_scout.geocode_venue",
            AsyncMock(return_value=COORDINATES),
        ) as mock_geocode,
        patch(
            "scene_scout.agents.neighborhood_scout.get_nearby_pois",
            AsyncMock(return_value=POIS),
        ) as mock_pois,
    ):
        job = await neighborhood_scout.prepare_neighborhood_job(
            _enriched_event(),
            cache=cache,
            run_id=TEST_RUN_ID,
        )

    assert job.mode == MODE_GEO_ASSISTED
    assert job.coordinates == COORDINATES
    assert job.poi_list == POIS
    mock_geocode.assert_awaited_once()
    mock_pois.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepare_neighborhood_job_falls_back_to_mode_b_on_geocode_failure(
    cache: CacheService,
) -> None:
    with patch(
        "scene_scout.agents.neighborhood_scout.geocode_venue",
        AsyncMock(return_value=None),
    ):
        job = await neighborhood_scout.prepare_neighborhood_job(
            _enriched_event(),
            cache=cache,
            run_id=TEST_RUN_ID,
        )

    assert job.mode == MODE_LLM_FALLBACK
    assert job.coordinates is None
    assert job.poi_list == []


@pytest.mark.asyncio
async def test_apply_batch_results_stores_context_in_venue_cache(
    cache: CacheService,
) -> None:
    event = _enriched_event()
    job = neighborhood_scout.NeighborhoodScoutJob(
        event=event,
        venue_key=VENUE_KEY,
        mode=MODE_GEO_ASSISTED,
        poi_list=POIS,
        coordinates=COORDINATES,
    )
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content=CONTEXT_JSON,
                success=True,
            )
        ],
    )

    enriched = await neighborhood_scout.apply_batch_results(
        [job],
        batch_results,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert enriched[0].neighborhood_context == (
        "A relaxed suburban pocket with a nearby cafe."
    )
    assert enriched[0].neighborhood_confidence == 0.82
    assert enriched[0].venue_coordinates == COORDINATES
    cached = cache.get_venue(VENUE_KEY)
    assert cached is not None
    assert cached.neighborhood_context == enriched[0].neighborhood_context


@pytest.mark.asyncio
async def test_apply_batch_results_clears_context_when_confidence_low(
    cache: CacheService,
) -> None:
    event = _enriched_event()
    job = neighborhood_scout.NeighborhoodScoutJob(
        event=event,
        venue_key=VENUE_KEY,
        mode=MODE_LLM_FALLBACK,
        poi_list=[],
        coordinates=None,
    )
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content=LOW_CONFIDENCE_JSON,
                success=True,
            )
        ],
    )

    enriched = await neighborhood_scout.apply_batch_results(
        [job],
        batch_results,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert enriched[0].neighborhood_context is None
    assert enriched[0].neighborhood_confidence == 0.35


@pytest.mark.asyncio
async def test_run_uses_venue_cache_without_batch(cache: CacheService) -> None:
    cache.set_venue(
        VENUE_KEY,
        coordinates=COORDINATES,
        neighborhood_context="Classic suburban sandlot vibes.",
        neighborhood_confidence=0.82,
    )
    mock_strategy = AsyncMock()

    enriched = await neighborhood_scout.run(
        [_enriched_event()],
        TEST_RUN_ID,
        cache=cache,
        batch_strategy=mock_strategy,
    )

    assert enriched[0].neighborhood_context == "Classic suburban sandlot vibes."
    mock_strategy.submit.assert_not_called()


@pytest.mark.asyncio
async def test_run_submits_mode_b_batch_when_geocoding_fails(
    cache: CacheService,
) -> None:
    event = _enriched_event()
    job = neighborhood_scout.NeighborhoodScoutJob(
        event=event,
        venue_key=VENUE_KEY,
        mode=MODE_LLM_FALLBACK,
        poi_list=[],
        coordinates=None,
    )
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content=CONTEXT_JSON,
                success=True,
            )
        ],
    )

    enriched = await neighborhood_scout.run(
        [event],
        TEST_RUN_ID,
        cache=cache,
        batch_strategy=ConcurrentBatchStrategy(model="gpt-4o-mini"),
        batch_results=batch_results,
        prepared_jobs=[job],
    )

    assert enriched[0].neighborhood_context == (
        "A relaxed suburban pocket with a nearby cafe."
    )
