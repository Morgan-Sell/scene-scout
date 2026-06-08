"""
Centralized LLM service for SceneScout.

All agent LLM calls go through ``complete()``. No agent imports LiteLLM
directly. Provider selection is controlled by ``LLM_MODEL`` in config.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, TypeVar

import litellm
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from pydantic import BaseModel, ValidationError

from scene_scout.config import (
    LLM_API_BASE,
    LLM_API_KEY,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_RETRY_BASE_DELAY_SECONDS,
    LLM_TIMEOUT_SECONDS,
)
from scene_scout.logging import get_logger

T = TypeVar("T", bound=BaseModel)

_INFRASTRUCTURE_ERRORS = (
    APIConnectionError,
    APIError,
    AuthenticationError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)

_JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)


class LLMInfrastructureError(Exception):
    """Raised on API outage, auth failure, or unrecoverable provider error.

    Triggers fail-fast behavior in the orchestrator.
    """


class LLMValidationError(Exception):
    """Raised on schema mismatch or unparseable LLM response.

    Triggers degrade-gracefully behavior at the record level.
    """


async def complete(
    prompt: str,
    system: str,
    response_model: type[T],
    run_id: str,
    agent_name: str,
) -> T:
    """Single entry point for all LLM calls.

    Calls LiteLLM, validates the response against ``response_model``, retries
    transient infrastructure failures with exponential backoff, and logs
    token usage per call.

    Parameters
    ----------
    prompt : str
        The user-turn content.
    system : str
        The system prompt (rendered from a prompt file).
    response_model : type[T]
        Pydantic model to validate and parse the LLM response.
    run_id : str
        Pipeline run identifier for log correlation.
    agent_name : str
        Calling agent name for log attribution and cost tracking.

    Returns
    -------
    T
        Validated instance of ``response_model``.

    Raises
    ------
    LLMInfrastructureError
        On API outage, auth failure, or unrecoverable provider error.
    LLMValidationError
        On schema mismatch or unparseable response.
    """
    logger = get_logger("llm", run_id=run_id)
    last_error: Exception | None = None

    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            raw_content, usage = await _call_litellm(prompt, system)
            parsed = _parse_json_content(raw_content)
            result = _validate_response(parsed, response_model)
            _log_token_usage(logger, agent_name, run_id, usage)
            return result
        except LLMValidationError:
            raise
        except _INFRASTRUCTURE_ERRORS as exc:
            last_error = exc
            if attempt < LLM_MAX_RETRIES:
                delay = LLM_RETRY_BASE_DELAY_SECONDS * (2**attempt)
                logger.warning(
                    "LLM call failed for %s (attempt %d/%d), retrying in %.1fs: %s",
                    agent_name,
                    attempt + 1,
                    LLM_MAX_RETRIES + 1,
                    delay,
                    exc,
                    data={
                        "agent_name": agent_name,
                        "attempt": attempt + 1,
                        "model": LLM_MODEL,
                    },
                )
                await asyncio.sleep(delay)
                continue
            break
        except BadRequestError as exc:
            raise LLMInfrastructureError(
                f"LLM provider rejected request for {agent_name}: {exc}"
            ) from exc
        except Exception as exc:
            raise LLMInfrastructureError(
                f"Unexpected LLM failure for {agent_name}: {exc}"
            ) from exc

    raise LLMInfrastructureError(
        f"LLM call failed for {agent_name} after {LLM_MAX_RETRIES + 1} attempts: "
        f"{last_error}"
    ) from last_error


async def _call_litellm(prompt: str, system: str) -> tuple[str, dict[str, int]]:
    """Invoke LiteLLM and return response text plus token usage.

    Parameters
    ----------
    prompt : str
        User-turn content.
    system : str
        System prompt.

    Returns
    -------
    tuple[str, dict[str, int]]
        Raw response text and token usage counts.

    Raises
    ------
    LLMInfrastructureError
        If the provider returns an empty response.
    """
    kwargs: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "timeout": LLM_TIMEOUT_SECONDS,
    }
    if LLM_API_KEY:
        kwargs["api_key"] = LLM_API_KEY
    if LLM_API_BASE:
        kwargs["api_base"] = LLM_API_BASE

    response = await litellm.acompletion(**kwargs)

    choice = response.choices[0]
    content = choice.message.content
    if not content or not str(content).strip():
        raise LLMInfrastructureError("LLM provider returned empty content")

    usage = _extract_usage(response)
    return str(content), usage


def _extract_usage(response: Any) -> dict[str, int]:
    """Extract token usage from a LiteLLM response object.

    Parameters
    ----------
    response : Any
        LiteLLM completion response.

    Returns
    -------
    dict[str, int]
        Token counts with keys ``prompt_tokens``, ``completion_tokens``,
        and ``total_tokens``.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _parse_json_content(raw_content: str) -> Any:
    """Parse JSON from an LLM response string.

    Supports bare JSON and fenced ```json code blocks.

    Parameters
    ----------
    raw_content : str
        Raw text returned by the LLM.

    Returns
    -------
    Any
        Parsed JSON value.

    Raises
    ------
    LLMValidationError
        If the content is not valid JSON.
    """
    text = raw_content.strip()
    if not text:
        raise LLMValidationError("LLM response was empty")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _JSON_FENCE_PATTERN.search(text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError as exc:
            raise LLMValidationError(
                f"LLM response JSON inside code fence is invalid: {exc}"
            ) from exc

    raise LLMValidationError("LLM response is not valid JSON")


def _validate_response(data: Any, response_model: type[T]) -> T:
    """Validate parsed JSON against a Pydantic response model.

    Parameters
    ----------
    data : Any
        Parsed JSON object.
    response_model : type[T]
        Expected Pydantic model class.

    Returns
    -------
    T
        Validated model instance.

    Raises
    ------
    LLMValidationError
        If validation fails.
    """
    try:
        return response_model.model_validate(data)
    except ValidationError as exc:
        raise LLMValidationError(
            f"LLM response failed schema validation for {response_model.__name__}: {exc}"
        ) from exc


def _log_token_usage(
    logger: Any,
    agent_name: str,
    run_id: str,
    usage: dict[str, int],
) -> None:
    """Log token usage for a completed LLM call.

    Parameters
    ----------
    logger : AgentLogger
        LLM service logger.
    agent_name : str
        Calling agent name.
    run_id : str
        Pipeline run identifier.
    usage : dict[str, int]
        Token usage counts from the provider response.
    """
    logger.info(
        "LLM call complete for %s",
        agent_name,
        data={
            "agent_name": agent_name,
            "run_id": run_id,
            "model": LLM_MODEL,
            **usage,
        },
    )
