"""
Tests for the Neighborhood Scout agent.

Covers venue cache resolution, Mode A/B geocoding paths, confidence thresholding,
batch application, and end-to-end run() with mocked geocoding and batch.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scene_scout.agents import neighborhood_scout
from scene_scout.models.curated import CuratedRecommendation
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
from scene_scout.services.feedback import generate_feedback_token
from scene_scout.services.geocoding import venue_cache_key
from tests.conftest import TEST_RUN_ID

GOLDEN_DIR = (
    Path(__file__).parent.parent
    / "fixtures"
    / "golden"
    / "enrichment"
    / "neighborhood_scout"
)

GOLDEN_FIXTURE_NAMES = [
    "mode_a_geocoded_venue",
    "mode_b_geocode_failure",
    "low_confidence_context",
    "venue_cache_hit",
    "validation_error_response",
]

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


def _score_breakdown() -> dict[str, float]:
    return {
        "category_fit": 0.8,
        "vibe_fit": 0.7,
        "semantic_similarity": 0.0,
        "performer_affinity": 0.7,
        "location": 1.0,
        "novelty": 1.0,
        "source_quality": 0.8,
        "source_coverage": 0.33,
        "description_quality": 0.9,
    }


def _curated_recommendation(**overrides: object) -> CuratedRecommendation:
    event = overrides.pop("event", _enriched_event())
    payload = {
        "rank": 1,
        "event": event,
        "score": 0.85,
        "score_breakdown": _score_breakdown(),
        "explanation": "Strong fit for your profile.",
        "neighborhood_context": None,
        "sellout_risk": "low",
        "sellout_urgency_note": None,
        "feedback_token": generate_feedback_token(),
        "is_wildcard": False,
        "run_id": TEST_RUN_ID,
        "recommended_at": START,
    }
    payload.update(overrides)
    return CuratedRecommendation.model_validate(payload)


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _enriched_event_from_golden(data: dict) -> EnrichedEvent:
    return EnrichedEvent.from_normalized(
        NormalizedEvent.model_validate(data["event"]),
    )


def _mock_litellm_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        ),
    )


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


@pytest.mark.asyncio
async def test_apply_batch_results_returns_empty_on_validation_error(
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
                content='{"context": "missing required keys"}',
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
    assert enriched[0].neighborhood_confidence == 0.0


@pytest.mark.asyncio
async def test_enrich_curated_neighborhoods_dedupes_same_venue(
    cache: CacheService,
) -> None:
    second_event = _enriched_event(
        id=compute_normalized_event_id(
            "Morning Sandlot Practice",
            "Sun, Jun 8 2025",
            VENUE,
        ),
        title="Morning Sandlot Practice",
    )
    recommendations = [
        _curated_recommendation(rank=1, event=_enriched_event()),
        _curated_recommendation(rank=2, event=second_event),
    ]
    mock_strategy = AsyncMock()
    mock_strategy.submit = AsyncMock(return_value="batch-curated")
    mock_strategy.poll = AsyncMock(
        return_value=BatchResults(
            batch_id="batch-curated",
            status="completed",
            results=[
                BatchResultItem(
                    custom_id=EVENT_ID,
                    content=CONTEXT_JSON,
                    success=True,
                )
            ],
        )
    )

    with (
        patch(
            "scene_scout.agents.neighborhood_scout.geocode_venue",
            AsyncMock(return_value=COORDINATES),
        ) as mock_geocode,
        patch(
            "scene_scout.agents.neighborhood_scout.get_nearby_pois",
            AsyncMock(return_value=POIS),
        ),
        patch(
            "scene_scout.agents.neighborhood_scout.get_batch_strategy",
            lambda: mock_strategy,
        ),
    ):
        enriched = await neighborhood_scout.enrich_curated_neighborhoods(
            recommendations,
            cache=cache,
            run_id=TEST_RUN_ID,
        )

    mock_geocode.assert_awaited_once()
    mock_strategy.submit.assert_awaited_once()
    assert enriched[0].neighborhood_context == enriched[1].neighborhood_context
    assert (
        enriched[0].event.neighborhood_context == enriched[1].event.neighborhood_context
    )
    assert enriched[0].event.venue_coordinates == COORDINATES


@pytest.mark.asyncio
async def test_enrich_curated_neighborhoods_uses_cache_without_external_calls(
    cache: CacheService,
) -> None:
    cache.set_venue(
        VENUE_KEY,
        coordinates=COORDINATES,
        neighborhood_context="Classic suburban sandlot vibes.",
        neighborhood_confidence=0.82,
    )
    recommendations = [
        _curated_recommendation(rank=1),
        _curated_recommendation(
            rank=2,
            event=_enriched_event(
                id=compute_normalized_event_id(
                    "Morning Sandlot Practice",
                    "Sun, Jun 8 2025",
                    VENUE,
                ),
                title="Morning Sandlot Practice",
            ),
        ),
    ]
    mock_strategy = AsyncMock()

    with (
        patch(
            "scene_scout.agents.neighborhood_scout.geocode_venue",
            AsyncMock(),
        ) as mock_geocode,
        patch(
            "scene_scout.agents.neighborhood_scout.get_batch_strategy",
            lambda: mock_strategy,
        ),
    ):
        enriched = await neighborhood_scout.enrich_curated_neighborhoods(
            recommendations,
            cache=cache,
            run_id=TEST_RUN_ID,
        )

    mock_geocode.assert_not_called()
    mock_strategy.submit.assert_not_called()
    assert enriched[0].neighborhood_context == "Classic suburban sandlot vibes."
    assert enriched[1].event.neighborhood_context == "Classic suburban sandlot vibes."


@pytest.mark.parametrize("fixture_name", GOLDEN_FIXTURE_NAMES)
@pytest.mark.asyncio
async def test_golden_fixture_neighborhood_scout(
    fixture_name: str,
    cache: CacheService,
) -> None:
    """Regression: golden event types yield expected Neighborhood Scout behavior."""
    golden = _load_golden(fixture_name)
    event = _enriched_event_from_golden(golden)
    venue_key = venue_cache_key(event.venue, event.city)
    mock_strategy = AsyncMock()

    if golden.get("uses_cache"):
        cache.set_venue(
            venue_key,
            coordinates=tuple(golden["cached_coordinates"]),
            neighborhood_context=golden["cached_context"],
            neighborhood_confidence=golden["cached_confidence"],
        )
        enriched = await neighborhood_scout.run(
            [event],
            TEST_RUN_ID,
            cache=cache,
            batch_strategy=mock_strategy,
        )
        mock_strategy.submit.assert_not_called()
    else:
        geocode_result = golden.get("geocode_result")
        geocode_coords = tuple(geocode_result) if geocode_result else None
        llm_json = json.dumps(golden["llm_output"])
        mock_completion = AsyncMock(
            return_value=_mock_litellm_response(llm_json),
        )
        with (
            patch(
                "scene_scout.agents.neighborhood_scout.geocode_venue",
                AsyncMock(return_value=geocode_coords),
            ),
            patch(
                "scene_scout.agents.neighborhood_scout.get_nearby_pois",
                AsyncMock(return_value=golden.get("poi_list", [])),
            ),
            patch(
                "scene_scout.services.batch.litellm.acompletion",
                mock_completion,
            ),
        ):
            enriched = await neighborhood_scout.run(
                [event],
                TEST_RUN_ID,
                cache=cache,
                batch_strategy=ConcurrentBatchStrategy(model="gpt-4o-mini"),
            )
        mock_completion.assert_awaited_once()

    assert enriched[0].neighborhood_context == golden["expected_neighborhood_context"]
    assert (
        enriched[0].neighborhood_confidence
        == golden["expected_neighborhood_confidence"]
    )


def test_golden_fixtures_directory_has_five_representative_types() -> None:
    fixture_files = sorted(GOLDEN_DIR.glob("*.json"))
    assert len(fixture_files) == 5
    assert [path.stem for path in fixture_files] == sorted(GOLDEN_FIXTURE_NAMES)
