"""
Tests for the centralized LLM service.

Covers successful completion, JSON fence parsing, schema validation failures,
infrastructure errors with retry/backoff, and token usage logging.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import APIConnectionError, RateLimitError
from pydantic import BaseModel

from scene_scout.services.llm import (
    LLMInfrastructureError,
    LLMValidationError,
    complete,
)

TEST_RUN_ID = "20250606-143022"


class SampleResponse(BaseModel):
    """Minimal response model for LLM service tests."""

    name: str
    value: int


def _mock_response(
    content: str,
    *,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


@pytest.fixture
def logs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect vol-logs to a temporary directory for isolation."""
    monkeypatch.setenv("VOL_LOGS_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_complete_returns_validated_model() -> None:
    mock_completion = AsyncMock(
        return_value=_mock_response('{"name": "test", "value": 42}')
    )
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        mock_completion,
    ):
        result = await complete(
            prompt="extract this",
            system="you are helpful",
            response_model=SampleResponse,
            run_id=TEST_RUN_ID,
            agent_name="event_extraction",
        )

    assert result == SampleResponse(name="test", value=42)
    mock_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_parses_json_code_fence() -> None:
    content = '```json\n{"name": "fenced", "value": 7}\n```'
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        AsyncMock(return_value=_mock_response(content)),
    ):
        result = await complete(
            prompt="extract",
            system="system",
            response_model=SampleResponse,
            run_id=TEST_RUN_ID,
            agent_name="event_extraction",
        )

    assert result.name == "fenced"
    assert result.value == 7


@pytest.mark.asyncio
async def test_complete_raises_validation_error_on_schema_mismatch() -> None:
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        AsyncMock(return_value=_mock_response('{"name": "only-name"}')),
    ):
        with pytest.raises(LLMValidationError, match="schema validation"):
            await complete(
                prompt="extract",
                system="system",
                response_model=SampleResponse,
                run_id=TEST_RUN_ID,
                agent_name="event_extraction",
            )


@pytest.mark.asyncio
async def test_complete_raises_validation_error_on_invalid_json() -> None:
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        AsyncMock(return_value=_mock_response("not json at all")),
    ):
        with pytest.raises(LLMValidationError, match="not valid JSON"):
            await complete(
                prompt="extract",
                system="system",
                response_model=SampleResponse,
                run_id=TEST_RUN_ID,
                agent_name="event_extraction",
            )


@pytest.mark.asyncio
async def test_complete_retries_then_raises_infrastructure_error() -> None:
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
        patch(
            "scene_scout.services.llm.LLM_MAX_RETRIES",
            2,
        ),
        patch(
            "scene_scout.services.llm.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep,
    ):
        with pytest.raises(LLMInfrastructureError, match="after 3 attempts"):
            await complete(
                prompt="extract",
                system="system",
                response_model=SampleResponse,
                run_id=TEST_RUN_ID,
                agent_name="event_extraction",
            )

    assert mock_completion.await_count == 3
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_complete_retries_on_transient_error_then_succeeds() -> None:
    mock_completion = AsyncMock(
        side_effect=[
            APIConnectionError(
                message="connection reset",
                llm_provider="anthropic",
                model="claude-sonnet-4-6",
            ),
            _mock_response('{"name": "recovered", "value": 1}'),
        ]
    )
    with (
        patch(
            "scene_scout.services.llm.litellm.acompletion",
            mock_completion,
        ),
        patch(
            "scene_scout.services.llm.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        result = await complete(
            prompt="extract",
            system="system",
            response_model=SampleResponse,
            run_id=TEST_RUN_ID,
            agent_name="event_extraction",
        )

    assert result.name == "recovered"
    assert mock_completion.await_count == 2


@pytest.mark.asyncio
async def test_complete_logs_token_usage(logs_dir: Path) -> None:
    with patch(
        "scene_scout.services.llm.litellm.acompletion",
        AsyncMock(
            return_value=_mock_response(
                '{"name": "logged", "value": 3}',
                prompt_tokens=100,
                completion_tokens=25,
            )
        ),
    ):
        await complete(
            prompt="extract",
            system="system",
            response_model=SampleResponse,
            run_id=TEST_RUN_ID,
            agent_name="ranking",
        )

    log_file = logs_dir / f"{TEST_RUN_ID}.jsonl"
    entries = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").strip().splitlines()
    ]
    usage_entry = next(
        entry for entry in entries if entry["message"].startswith("LLM call complete")
    )
    assert usage_entry["data"]["prompt_tokens"] == 100
    assert usage_entry["data"]["completion_tokens"] == 25
    assert usage_entry["data"]["total_tokens"] == 125
    assert usage_entry["data"]["agent_name"] == "ranking"
