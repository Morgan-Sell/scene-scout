"""
Tests for the batch strategy service.

Covers strategy routing, concurrent LiteLLM execution with mocks, and
Anthropic batch submit/poll with mocked HTTP and LiteLLM batch retrieval.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scene_scout.services.batch import (
    AnthropicBatchStrategy,
    BatchInfrastructureError,
    BatchRequest,
    BatchResults,
    ConcurrentBatchStrategy,
    _concurrent_batches,
    get_batch_strategy,
)
from tests.conftest import TEST_RUN_ID

SQUINTS_VIBE_REQUEST = BatchRequest(
    custom_id="squints-magic-eye",
    prompt="Describe the vibe at the sandlot game.",
    system="You are a neighborhood scout.",
    agent_name="neighborhood_scout",
)

HAM_TALENT_REQUEST = BatchRequest(
    custom_id="ham-porter-homerun",
    prompt="Rate the talent at this pickup baseball game.",
    system="You are a talent scout.",
    agent_name="talent_scout",
)


@pytest.fixture(autouse=True)
def _clear_concurrent_batches() -> None:
    """Reset in-memory concurrent batch store between tests."""
    _concurrent_batches.clear()
    yield
    _concurrent_batches.clear()


def _mock_litellm_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=8,
            total_tokens=20,
        ),
    )


def test_get_batch_strategy_returns_anthropic_for_claude() -> None:
    strategy = get_batch_strategy("claude-sonnet-4-6")
    assert isinstance(strategy, AnthropicBatchStrategy)


def test_get_batch_strategy_returns_anthropic_for_litellm_claude_prefix() -> None:
    strategy = get_batch_strategy("anthropic/claude-sonnet-4-6")
    assert isinstance(strategy, AnthropicBatchStrategy)


def test_get_batch_strategy_returns_concurrent_for_non_claude() -> None:
    strategy = get_batch_strategy("gpt-4o-mini")
    assert isinstance(strategy, ConcurrentBatchStrategy)


@pytest.mark.asyncio
async def test_concurrent_submit_returns_batch_id() -> None:
    strategy = ConcurrentBatchStrategy(model="gpt-4o-mini")
    batch_id = await strategy.submit([SQUINTS_VIBE_REQUEST], run_id=TEST_RUN_ID)

    assert batch_id
    assert batch_id in _concurrent_batches


@pytest.mark.asyncio
async def test_concurrent_submit_rejects_empty_requests() -> None:
    strategy = ConcurrentBatchStrategy(model="gpt-4o-mini")
    with pytest.raises(ValueError, match="empty batch"):
        await strategy.submit([], run_id=TEST_RUN_ID)


@pytest.mark.asyncio
async def test_concurrent_poll_executes_litellm_calls() -> None:
    strategy = ConcurrentBatchStrategy(model="gpt-4o-mini")
    mock_completion = AsyncMock(
        side_effect=[
            _mock_litellm_response('{"vibe": "nostalgic"}'),
            _mock_litellm_response('{"talent": "legendary"}'),
        ]
    )

    with patch(
        "scene_scout.services.batch.litellm.acompletion",
        mock_completion,
    ):
        batch_id = await strategy.submit(
            [SQUINTS_VIBE_REQUEST, HAM_TALENT_REQUEST],
            run_id=TEST_RUN_ID,
        )
        results = await strategy.poll(batch_id)

    assert isinstance(results, BatchResults)
    assert results.status == "completed"
    assert len(results.results) == 2
    assert results.results[0].custom_id == "squints-magic-eye"
    assert results.results[0].success is True
    assert results.results[0].content == '{"vibe": "nostalgic"}'
    assert results.results[1].custom_id == "ham-porter-homerun"
    assert mock_completion.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_poll_returns_cached_results_on_second_poll() -> None:
    strategy = ConcurrentBatchStrategy(model="gpt-4o-mini")
    mock_completion = AsyncMock(
        return_value=_mock_litellm_response('{"vibe": "sunny"}'),
    )

    with patch(
        "scene_scout.services.batch.litellm.acompletion",
        mock_completion,
    ):
        batch_id = await strategy.submit([SQUINTS_VIBE_REQUEST], run_id=TEST_RUN_ID)
        first = await strategy.poll(batch_id)
        second = await strategy.poll(batch_id)

    assert first.results == second.results
    mock_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_poll_unknown_batch_raises() -> None:
    strategy = ConcurrentBatchStrategy(model="gpt-4o-mini")
    with pytest.raises(BatchInfrastructureError, match="Unknown batch_id"):
        await strategy.poll("missing-batch-id")


@pytest.mark.asyncio
async def test_anthropic_submit_returns_batch_id() -> None:
    strategy = AnthropicBatchStrategy(model="claude-sonnet-4-6")
    mock_post = AsyncMock(return_value={"id": "msgbatch_sandlot-1993"})

    with patch("scene_scout.services.batch._anthropic_post", mock_post):
        batch_id = await strategy.submit([SQUINTS_VIBE_REQUEST], run_id=TEST_RUN_ID)

    assert batch_id == "msgbatch_sandlot-1993"
    mock_post.assert_awaited_once()
    payload = mock_post.await_args.args[1]
    assert payload["requests"][0]["custom_id"] == "squints-magic-eye"
    assert payload["requests"][0]["params"]["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_anthropic_poll_returns_processing_while_in_progress() -> None:
    strategy = AnthropicBatchStrategy(model="claude-sonnet-4-6")
    mock_retrieve = AsyncMock(
        return_value=SimpleNamespace(status="in_progress"),
    )

    with patch("scene_scout.services.batch.litellm.aretrieve_batch", mock_retrieve):
        results = await strategy.poll("msgbatch_sandlot-1993")

    assert results.status == "processing"
    assert results.results == []


@pytest.mark.asyncio
async def test_anthropic_poll_returns_completed_results() -> None:
    strategy = AnthropicBatchStrategy(model="claude-sonnet-4-6")
    mock_retrieve = AsyncMock(return_value=SimpleNamespace(status="completed"))
    results_jsonl = (
        '{"custom_id":"squints-magic-eye","result":{"type":"succeeded",'
        '"message":{"content":[{"type":"text","text":"Sunset over the sandlot."}]}}}\n'
    )
    meta_response = httpx.Response(
        200,
        json={
            "id": "msgbatch_sandlot-1993",
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_sandlot-1993/results",
        },
    )
    results_response = httpx.Response(200, text=results_jsonl)

    mock_get = AsyncMock(side_effect=[meta_response, results_response])

    with (
        patch("scene_scout.services.batch.litellm.aretrieve_batch", mock_retrieve),
        patch("scene_scout.services.batch._anthropic_get", mock_get),
    ):
        results = await strategy.poll("msgbatch_sandlot-1993")

    assert results.status == "completed"
    assert len(results.results) == 1
    assert results.results[0].success is True
    assert results.results[0].content == "Sunset over the sandlot."
