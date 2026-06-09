"""
Batch strategy service for enrichment LLM calls.

Routes Claude models to Anthropic's native Message Batches API (async, 50% cost).
All other models use concurrent LiteLLM calls via ``asyncio.gather()``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import httpx
import litellm
from pydantic import BaseModel, Field

from scene_scout.config import LLM_API_BASE, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT_SECONDS
from scene_scout.logging import get_logger

BatchStatus = Literal["processing", "completed", "failed"]

_ANTHROPIC_BATCHES_BETA = "message-batches-2024-09-24"
_DEFAULT_ANTHROPIC_API_BASE = "https://api.anthropic.com"


class BatchRequest(BaseModel):
    """Single LLM request within an enrichment batch."""

    custom_id: str
    prompt: str
    system: str
    agent_name: str
    max_tokens: int = 4096


class BatchResultItem(BaseModel):
    """Result for one request in a completed batch."""

    custom_id: str
    content: str | None = None
    error: str | None = None
    success: bool


class BatchResults(BaseModel):
    """Poll response for a submitted batch job."""

    batch_id: str
    status: BatchStatus
    results: list[BatchResultItem] = Field(default_factory=list)


@runtime_checkable
class BatchStrategy(Protocol):
    """Provider-specific batch submit/poll interface."""

    async def submit(self, requests: list[BatchRequest], run_id: str) -> str: ...
    async def poll(self, batch_id: str) -> BatchResults: ...


class BatchInfrastructureError(Exception):
    """Raised when batch submission or polling fails at the provider level."""


@dataclass
class _ConcurrentBatchJob:
    requests: list[BatchRequest]
    run_id: str
    status: BatchStatus = "processing"
    results: list[BatchResultItem] = field(default_factory=list)


_concurrent_batches: dict[str, _ConcurrentBatchJob] = {}


def _is_claude_model(model: str) -> bool:
    """Return True when ``model`` refers to an Anthropic Claude model."""
    base = model.lower().split("/")[-1]
    return base.startswith("claude")


def _anthropic_model_name(model: str) -> str:
    """Strip LiteLLM provider prefix for the native Anthropic batch API."""
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _anthropic_api_base() -> str:
    return (LLM_API_BASE or _DEFAULT_ANTHROPIC_API_BASE).rstrip("/")


def _anthropic_headers() -> dict[str, str]:
    if not LLM_API_KEY:
        raise BatchInfrastructureError("Missing Anthropic API key for batch submission")
    return {
        "accept": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": _ANTHROPIC_BATCHES_BETA,
        "content-type": "application/json",
        "x-api-key": LLM_API_KEY,
    }


async def _anthropic_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST to the Anthropic API and return parsed JSON."""
    url = f"{_anthropic_api_base()}{path}"
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS) as client:
        response = await client.post(url, headers=_anthropic_headers(), json=body)
        response.raise_for_status()
        return response.json()


async def _anthropic_get(url: str) -> httpx.Response:
    """GET from an Anthropic URL and return the raw response."""
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=_anthropic_headers())
        response.raise_for_status()
        return response


def _extract_message_text(message: dict[str, Any]) -> str:
    """Extract plain text from an Anthropic message content array."""
    parts: list[str] = []
    for block in message.get("content", []):
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _parse_anthropic_results_jsonl(raw_text: str) -> list[BatchResultItem]:
    """Parse Anthropic batch results JSONL into ``BatchResultItem`` rows."""
    results: list[BatchResultItem] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        custom_id = str(row["custom_id"])
        result = row.get("result", {})
        result_type = result.get("type")
        if result_type == "succeeded":
            message = result.get("message", {})
            results.append(
                BatchResultItem(
                    custom_id=custom_id,
                    content=_extract_message_text(message),
                    success=True,
                )
            )
        elif result_type == "errored":
            error = result.get("error", {})
            message = error.get("message", "Unknown batch item error")
            results.append(
                BatchResultItem(
                    custom_id=custom_id,
                    error=str(message),
                    success=False,
                )
            )
        else:
            results.append(
                BatchResultItem(
                    custom_id=custom_id,
                    error=f"Unexpected result type: {result_type}",
                    success=False,
                )
            )
    return results


def _extract_usage(response: Any) -> dict[str, int]:
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


async def _call_litellm_batch(
    request: BatchRequest,
    *,
    model: str,
    run_id: str,
) -> BatchResultItem:
    """Execute one batch item via LiteLLM."""
    logger = get_logger("llm", run_id=run_id)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.prompt},
        ],
        "timeout": LLM_TIMEOUT_SECONDS,
    }
    if LLM_API_KEY:
        kwargs["api_key"] = LLM_API_KEY
    if LLM_API_BASE:
        kwargs["api_base"] = LLM_API_BASE

    try:
        response = await litellm.acompletion(**kwargs)
        content = response.choices[0].message.content
        if not content or not str(content).strip():
            return BatchResultItem(
                custom_id=request.custom_id,
                error="LLM provider returned empty content",
                success=False,
            )

        usage = _extract_usage(response)
        logger.info(
            "Batch LLM call complete for %s",
            request.agent_name,
            data={
                "agent_name": request.agent_name,
                "run_id": run_id,
                "model": model,
                "custom_id": request.custom_id,
                **usage,
            },
        )
        return BatchResultItem(
            custom_id=request.custom_id,
            content=str(content),
            success=True,
        )
    except Exception as exc:
        logger.warning(
            "Batch LLM call failed for %s: %s",
            request.agent_name,
            exc,
            data={
                "agent_name": request.agent_name,
                "run_id": run_id,
                "custom_id": request.custom_id,
            },
        )
        return BatchResultItem(
            custom_id=request.custom_id,
            error=str(exc),
            success=False,
        )


class ConcurrentBatchStrategy:
    """Fallback batch strategy — concurrent standard LiteLLM calls."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model or LLM_MODEL

    async def submit(self, requests: list[BatchRequest], run_id: str) -> str:
        if not requests:
            raise ValueError("Cannot submit empty batch")

        batch_id = str(uuid.uuid4())
        _concurrent_batches[batch_id] = _ConcurrentBatchJob(
            requests=requests,
            run_id=run_id,
        )
        logger = get_logger("llm", run_id=run_id)
        logger.info(
            "Submitted concurrent enrichment batch",
            data={"batch_id": batch_id, "request_count": len(requests)},
        )
        return batch_id

    async def poll(self, batch_id: str) -> BatchResults:
        job = _concurrent_batches.get(batch_id)
        if job is None:
            raise BatchInfrastructureError(f"Unknown batch_id: {batch_id}")

        if job.status == "completed":
            return BatchResults(
                batch_id=batch_id,
                status="completed",
                results=job.results,
            )

        tasks = [
            _call_litellm_batch(request, model=self._model, run_id=job.run_id)
            for request in job.requests
        ]
        job.results = list(await asyncio.gather(*tasks))
        job.status = "completed"
        return BatchResults(
            batch_id=batch_id,
            status="completed",
            results=job.results,
        )


class AnthropicBatchStrategy:
    """Native Anthropic Message Batches API — true async, 50% cost reduction."""

    def __init__(self, model: str | None = None) -> None:
        self._model = _anthropic_model_name(model or LLM_MODEL)

    async def submit(self, requests: list[BatchRequest], run_id: str) -> str:
        if not requests:
            raise ValueError("Cannot submit empty batch")

        payload = {
            "requests": [
                {
                    "custom_id": request.custom_id,
                    "params": {
                        "model": self._model,
                        "max_tokens": request.max_tokens,
                        "system": request.system,
                        "messages": [{"role": "user", "content": request.prompt}],
                    },
                }
                for request in requests
            ]
        }

        try:
            response = await _anthropic_post("/v1/messages/batches", payload)
        except httpx.HTTPError as exc:
            raise BatchInfrastructureError(
                f"Anthropic batch submission failed: {exc}"
            ) from exc

        batch_id = str(response["id"])
        logger = get_logger("llm", run_id=run_id)
        logger.info(
            "Submitted Anthropic enrichment batch",
            data={
                "batch_id": batch_id,
                "request_count": len(requests),
                "model": self._model,
            },
        )
        return batch_id

    async def poll(self, batch_id: str) -> BatchResults:
        try:
            batch_status = await litellm.aretrieve_batch(
                batch_id=batch_id,
                custom_llm_provider="anthropic",
                api_key=LLM_API_KEY,
                api_base=_anthropic_api_base(),
                timeout=LLM_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise BatchInfrastructureError(
                f"Anthropic batch poll failed for {batch_id}: {exc}"
            ) from exc

        status = getattr(batch_status, "status", "in_progress")
        if status in {"validating", "in_progress", "finalizing", "cancelling"}:
            return BatchResults(batch_id=batch_id, status="processing")

        if status != "completed":
            return BatchResults(batch_id=batch_id, status="failed")

        try:
            batch_meta = await _anthropic_get(
                f"{_anthropic_api_base()}/v1/messages/batches/{batch_id}"
            )
            meta = batch_meta.json()
            results_url = meta.get("results_url")
            if not results_url:
                raise BatchInfrastructureError(
                    f"Anthropic batch {batch_id} completed without results_url"
                )

            results_response = await _anthropic_get(results_url)
            results = _parse_anthropic_results_jsonl(results_response.text)
        except httpx.HTTPError as exc:
            raise BatchInfrastructureError(
                f"Anthropic batch results fetch failed for {batch_id}: {exc}"
            ) from exc

        return BatchResults(
            batch_id=batch_id,
            status="completed",
            results=results,
        )


def get_batch_strategy(model: str | None = None) -> BatchStrategy:
    """Return the batch strategy appropriate for ``model``."""
    resolved = model or LLM_MODEL
    if _is_claude_model(resolved):
        return AnthropicBatchStrategy(model=resolved)
    return ConcurrentBatchStrategy(model=resolved)
