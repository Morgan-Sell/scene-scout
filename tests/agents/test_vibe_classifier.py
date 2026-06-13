"""
Tests for the Vibe Classifier agent.

Covers vocabulary enforcement, cache resolution, batch application, validation-error
degradation, tag distribution logging, and end-to-end run() with mocked batch.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scene_scout.agents import vibe_classifier
from scene_scout.models.enrichment import EnrichedEvent, PerformerInfo
from scene_scout.models.event import NormalizedEvent, compute_normalized_event_id
from scene_scout.services.batch import (
    BatchResultItem,
    BatchResults,
    ConcurrentBatchStrategy,
)
from scene_scout.services.cache import CacheService
from tests.conftest import TEST_RUN_ID

GOLDEN_DIR = (
    Path(__file__).parent.parent
    / "fixtures"
    / "golden"
    / "enrichment"
    / "vibe_classifier"
)

GOLDEN_FIXTURE_NAMES = [
    "outdoor_sports_event",
    "intimate_acoustic_event",
    "family_friendly_event",
    "late_night_dance_event",
    "invalid_vocabulary_response",
]

SANDLOT_FEED = "sandlot-pickup-league"
START = datetime(2025, 6, 7, 18, 0, tzinfo=timezone.utc)
EVENT_ID = compute_normalized_event_id(
    "Sunset Sandlot Game",
    "Sat, Jun 7 2025",
    "The Sandlot",
)
DESCRIPTION = "Pickup baseball under the floodlights with the whole neighborhood."
VIBE_HASH = vibe_classifier.compute_vibe_content_hash(DESCRIPTION, ["Sports"])


def _normalized_event(**overrides: object) -> NormalizedEvent:
    payload = {
        "id": EVENT_ID,
        "title": "Sunset Sandlot Game",
        "start_datetime": START,
        "venue": "The Sandlot",
        "city": "Los Angeles",
        "url": "https://example.com/sunset-sandlot-game",
        "is_free": True,
        "description": DESCRIPTION,
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


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _enriched_event_from_golden(data: dict) -> EnrichedEvent:
    return EnrichedEvent.model_validate(data["event"])


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


def test_compute_vibe_content_hash_is_stable() -> None:
    first = vibe_classifier.compute_vibe_content_hash(DESCRIPTION, ["Sports"])
    second = vibe_classifier.compute_vibe_content_hash(DESCRIPTION, ["Sports"])

    assert first == second
    assert len(first) == 64


def test_validate_vibe_tags_accepts_controlled_vocabulary() -> None:
    tags = vibe_classifier.validate_vibe_tags(["outdoor", "social", "family-friendly"])

    assert tags == ["outdoor", "social", "family-friendly"]


def test_validate_vibe_tags_rejects_unknown_tags() -> None:
    assert vibe_classifier.validate_vibe_tags(["outdoor", "mystery-vibe"]) is None


def test_validate_vibe_tags_rejects_wrong_count() -> None:
    assert vibe_classifier.validate_vibe_tags(["outdoor"]) is None
    assert (
        vibe_classifier.validate_vibe_tags(
            ["outdoor", "social", "inclusive", "late-night", "niche", "immersive"]
        )
        is None
    )


def test_compute_tag_distribution_counts_tags() -> None:
    events = [
        _enriched_event(vibe_tags=["outdoor", "social"]),
        _enriched_event(vibe_tags=["outdoor", "inclusive"]),
    ]

    distribution = vibe_classifier.compute_tag_distribution(events)

    assert distribution == {"inclusive": 1, "outdoor": 2, "social": 1}


@pytest.mark.asyncio
async def test_apply_batch_results_rejects_invalid_vocabulary_tags(
    cache: CacheService,
) -> None:
    event = _enriched_event()
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content='{"vibe_tags": ["outdoor", "not-a-real-vibe"]}',
                success=True,
            )
        ],
    )

    enriched = await vibe_classifier.apply_batch_results(
        [event],
        batch_results,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert enriched[0].vibe_tags == []
    assert cache.get_vibe(VIBE_HASH) is None


@pytest.mark.asyncio
async def test_apply_batch_results_stores_valid_tags_in_vibe_cache(
    cache: CacheService,
) -> None:
    event = _enriched_event()
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content='{"vibe_tags": ["outdoor", "social", "family-friendly"]}',
                success=True,
            )
        ],
    )

    enriched = await vibe_classifier.apply_batch_results(
        [event],
        batch_results,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert enriched[0].vibe_tags == ["outdoor", "social", "family-friendly"]
    assert cache.get_vibe(VIBE_HASH) == ["outdoor", "social", "family-friendly"]


@pytest.mark.asyncio
async def test_apply_batch_results_returns_empty_tags_on_validation_error(
    cache: CacheService,
) -> None:
    event = _enriched_event()
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content='{"tags": ["outdoor", "social"]}',
                success=True,
            )
        ],
    )

    enriched = await vibe_classifier.apply_batch_results(
        [event],
        batch_results,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert enriched[0].vibe_tags == []


@pytest.mark.asyncio
async def test_run_uses_vibe_cache_without_batch(cache: CacheService) -> None:
    cache.set_vibe(VIBE_HASH, ["outdoor", "social"])
    mock_strategy = AsyncMock()

    enriched = await vibe_classifier.run(
        [_enriched_event()],
        TEST_RUN_ID,
        cache=cache,
        batch_strategy=mock_strategy,
    )

    assert enriched[0].vibe_tags == ["outdoor", "social"]
    mock_strategy.submit.assert_not_called()


@pytest.mark.asyncio
async def test_run_submits_uncached_events_to_batch_strategy(
    cache: CacheService,
) -> None:
    event = _enriched_event(
        performers=[
            PerformerInfo(
                name="Benny Rodriguez",
                entity_type="other",
                confidence=0.9,
                affinity_score=0.8,
            )
        ]
    )
    batch_results = BatchResults(
        batch_id="batch-1",
        status="completed",
        results=[
            BatchResultItem(
                custom_id=event.id,
                content='{"vibe_tags": ["outdoor", "high-energy", "social"]}',
                success=True,
            )
        ],
    )

    enriched = await vibe_classifier.run(
        [event],
        TEST_RUN_ID,
        cache=cache,
        batch_strategy=ConcurrentBatchStrategy(model="gpt-4o-mini"),
        batch_results=batch_results,
    )

    assert enriched[0].vibe_tags == ["outdoor", "high-energy", "social"]
    assert enriched[0].performers[0].name == "Benny Rodriguez"


@pytest.mark.parametrize("fixture_name", GOLDEN_FIXTURE_NAMES)
@pytest.mark.asyncio
async def test_golden_fixture_vibe_classifier(
    fixture_name: str,
    cache: CacheService,
) -> None:
    """Regression: each golden event type yields expected Vibe Classifier behavior."""
    golden = _load_golden(fixture_name)
    event = _enriched_event_from_golden(golden)
    llm_json = json.dumps(golden["llm_output"])
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response(llm_json),
    )

    with patch(
        "scene_scout.services.batch.litellm.acompletion",
        mock_completion,
    ):
        enriched = await vibe_classifier.run(
            [event],
            TEST_RUN_ID,
            cache=cache,
            batch_strategy=ConcurrentBatchStrategy(model="gpt-4o-mini"),
        )

    assert enriched[0].vibe_tags == golden["expected_vibe_tags"]
    mock_completion.assert_awaited_once()


def test_golden_fixtures_directory_has_five_representative_types() -> None:
    fixture_files = sorted(GOLDEN_DIR.glob("*.json"))
    assert len(fixture_files) == 5
    assert [path.stem for path in fixture_files] == sorted(GOLDEN_FIXTURE_NAMES)


@pytest.mark.asyncio
async def test_run_calls_batch_submit_on_cache_miss(cache: CacheService) -> None:
    """Cache miss should invoke the batch strategy LLM path."""
    event = _enriched_event()
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response(
            '{"vibe_tags": ["outdoor", "social", "high-energy"]}'
        ),
    )

    with patch(
        "scene_scout.services.batch.litellm.acompletion",
        mock_completion,
    ):
        enriched = await vibe_classifier.run(
            [event],
            TEST_RUN_ID,
            cache=cache,
            batch_strategy=ConcurrentBatchStrategy(model="gpt-4o-mini"),
        )

    assert enriched[0].vibe_tags == ["outdoor", "social", "high-energy"]
    mock_completion.assert_awaited_once()
