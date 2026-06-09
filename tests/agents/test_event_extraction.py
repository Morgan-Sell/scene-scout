"""
Tests for the event extraction agent.

Covers successful extraction, non-event discard, validation skip, and
infrastructure error propagation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import RateLimitError

from scene_scout.agents import event_extraction
from scene_scout.models.feed import RawFeedEntry
from scene_scout.services.llm import LLMInfrastructureError
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED_ID = "sandlot-pickup-league"


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
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response(_event_llm_json(is_event=True)),
    )
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        mock_completion,
    ):
        results = await event_extraction.run([_sample_entry()], run_id=TEST_RUN_ID)

    assert len(results) == 1
    assert results[0].title == "The Great Bambino Night"
    assert results[0].is_event is True
    assert results[0].source_feed == SANDLOT_FEED_ID
    assert results[0].run_id == TEST_RUN_ID
    mock_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_discards_non_event_entries(logs_dir) -> None:
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response(_event_llm_json(is_event=False)),
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
