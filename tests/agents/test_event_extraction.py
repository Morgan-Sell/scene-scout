"""
Tests for the event extraction agent.

Covers successful extraction, non-event discard, validation skip, infrastructure
error propagation, and golden-file regression over representative RSS entry types.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import RateLimitError

from scene_scout.agents import event_extraction
from scene_scout.models.feed import RawFeedEntry
from scene_scout.services.llm import LLMInfrastructureError
from tests.conftest import TEST_RUN_ID

GOLDEN_DIR = Path(__file__).parent.parent / "fixtures" / "golden" / "event_extraction"

GOLDEN_FIXTURE_NAMES = [
    "full_event",
    "non_event_article",
    "sparse_entry",
    "multi_category_event",
    "description_heavy_event",
]

SANDLOT_FEED_ID = "sandlot-pickup-league"


def _load_golden(name: str) -> dict[str, Any]:
    path = GOLDEN_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _entry_from_golden(data: dict[str, Any]) -> RawFeedEntry:
    return RawFeedEntry.model_validate(data["entry"])


def _llm_json_from_golden(data: dict[str, Any]) -> str:
    return json.dumps(data["llm_output"])


def _sample_entry(**overrides: object) -> RawFeedEntry:
    payload = {
        "feed_id": SANDLOT_FEED_ID,
        "feed_name": "Mr. Mertle's Events Feed",
        "source_url": "https://example.com/sandlot-feed.xml",
        "run_id": TEST_RUN_ID,
        "title": "The Great Bambino Night",
        "link": "https://example.com/great-bambino-night",
        "description": "Sandlot legends retell the Babe Ruth story.",
        "published_raw": "Fri, 06 Jun 2025 20:00:00 +0000",
        "categories": ["Baseball"],
        "fetched_at": datetime(2025, 6, 6, 12, 0, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return RawFeedEntry.model_validate(payload)


def _event_llm_json(*, is_event: bool = True) -> str:
    return json.dumps(
        {
            "title": "The Great Bambino Night",
            "date": "Fri, 06 Jun 2025",
            "time": "8:00 PM",
            "venue": "The Sandlot",
            "neighborhood": None,
            "city": "Los Angeles",
            "url": "https://example.com/great-bambino-night",
            "price": "Free",
            "description": "Legends retell the Babe Ruth story.",
            "categories": ["Baseball"],
            "is_event": is_event,
            "extraction_confidence": 0.9 if is_event else 0.2,
        }
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


@pytest.mark.asyncio
async def test_run_returns_event_candidate_for_valid_extraction() -> None:
    golden = _load_golden("full_event")
    entry = _entry_from_golden(golden)
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response(_llm_json_from_golden(golden)),
    )
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        mock_completion,
    ):
        results = await event_extraction.run([entry], run_id=TEST_RUN_ID)

    assert len(results) == 1
    candidate = results[0]
    llm_output = golden["llm_output"]
    assert candidate.title == llm_output["title"]
    assert candidate.is_event is True
    assert candidate.venue == llm_output["venue"]
    assert candidate.categories == llm_output["categories"]
    assert candidate.source_feed == entry.feed_id
    assert candidate.run_id == TEST_RUN_ID
    mock_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_discards_non_event_entries(logs_dir) -> None:
    golden = _load_golden("non_event_article")
    entry = _entry_from_golden(golden)
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response(_llm_json_from_golden(golden)),
    )
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        mock_completion,
    ):
        results = await event_extraction.run([entry], run_id=TEST_RUN_ID)

    assert results == []
    log_file = logs_dir / f"{TEST_RUN_ID}.jsonl"
    entries = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").strip().splitlines()
    ]
    discard_entry = next(
        entry
        for entry in entries
        if entry["message"].startswith("Discarding non-event")
    )
    assert discard_entry["data"]["reason"] == "is_event=False"


@pytest.mark.asyncio
async def test_run_skips_entry_on_validation_error(logs_dir) -> None:
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response('{"title": "missing required fields"}'),
    )
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        mock_completion,
    ):
        results = await event_extraction.run([_sample_entry()], run_id=TEST_RUN_ID)

    assert results == []
    log_file = logs_dir / f"{TEST_RUN_ID}.jsonl"
    entries = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert any(
        entry["message"].startswith("Skipping entry due to LLM validation error")
        for entry in entries
    )


@pytest.mark.asyncio
async def test_run_reraises_infrastructure_error() -> None:
    mock_completion = AsyncMock(
        side_effect=RateLimitError(
            message="rate limited",
            llm_provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    with (
        patch(
            "scene_scout.services.llm.litellm.acompletion",
            mock_completion,
        ),
        patch("scene_scout.services.llm.LLM_MAX_RETRIES", 0),
        pytest.raises(LLMInfrastructureError),
    ):
        await event_extraction.run([_sample_entry()], run_id=TEST_RUN_ID)


@pytest.mark.parametrize("fixture_name", GOLDEN_FIXTURE_NAMES)
@pytest.mark.asyncio
async def test_golden_fixture_extraction(fixture_name: str) -> None:
    """Regression: each golden RSS entry type yields expected agent behavior."""
    golden = _load_golden(fixture_name)
    entry = _entry_from_golden(golden)
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response(_llm_json_from_golden(golden)),
    )
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        mock_completion,
    ):
        results = await event_extraction.run([entry], run_id=TEST_RUN_ID)

    if golden["include_in_results"]:
        assert len(results) == 1
        candidate = results[0]
        expected = golden["llm_output"]
        assert candidate.title == expected["title"]
        assert candidate.city == expected["city"]
        assert candidate.url == expected["url"]
        assert candidate.is_event is True
        assert candidate.extraction_confidence == expected["extraction_confidence"]
        assert candidate.source_feed == entry.feed_id
        assert candidate.run_id == TEST_RUN_ID
    else:
        assert results == []


def test_golden_fixtures_directory_has_five_representative_types() -> None:
    fixture_files = sorted(GOLDEN_DIR.glob("*.json"))
    assert len(fixture_files) == 5
    assert [path.stem for path in fixture_files] == sorted(GOLDEN_FIXTURE_NAMES)
