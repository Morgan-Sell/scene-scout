"""
Resend email delivery for SceneScout.

Sends HTML email via the Resend REST API. Failures raise
:class:`LLMInfrastructureError` so the orchestrator fail-fast path matches
LLM outages.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from scene_scout.services.llm import LLMInfrastructureError

_RESEND_API_URL = "https://api.resend.com/emails"


def _resolve_recipient(to_email: str | None) -> str | None:
    return to_email or os.getenv("USER_EMAIL")


def _resolve_sender(from_email: str | None) -> str | None:
    return from_email or os.getenv("RESEND_FROM_EMAIL") or os.getenv("FROM_EMAIL")


def _resolve_api_key(api_key: str | None) -> str | None:
    return api_key or os.getenv("RESEND_API_KEY")


async def send_html_email(
    *,
    subject: str,
    html: str,
    to_email: str | None = None,
    from_email: str | None = None,
    api_key: str | None = None,
) -> str:
    """Send an HTML email through Resend.

    Parameters
    ----------
    subject : str
        Email subject line.
    html : str
        Rendered HTML body.
    to_email : str, optional
        Recipient address. Defaults to ``USER_EMAIL`` env var.
    from_email : str, optional
        Sender address. Defaults to ``RESEND_FROM_EMAIL`` env var.
    api_key : str, optional
        Resend API key. Defaults to ``RESEND_API_KEY`` env var.

    Returns
    -------
    str
        Resend message ID.

    Raises
    ------
    LLMInfrastructureError
        On missing configuration or Resend API failure.
    """
    resolved_to = _resolve_recipient(to_email)
    resolved_from = _resolve_sender(from_email)
    resolved_key = _resolve_api_key(api_key)

    if not resolved_to:
        raise LLMInfrastructureError("USER_EMAIL is not configured")
    if not resolved_from:
        raise LLMInfrastructureError("RESEND_FROM_EMAIL is not configured")
    if not resolved_key:
        raise LLMInfrastructureError("RESEND_API_KEY is not configured")

    payload = {
        "from": resolved_from,
        "to": [resolved_to],
        "subject": subject,
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                _RESEND_API_URL,
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise LLMInfrastructureError(f"Resend request failed: {exc}") from exc

    if response.status_code >= 400:
        raise LLMInfrastructureError(
            f"Resend API returned {response.status_code}: {response.text}"
        )

    data: dict[str, Any] = response.json()
    message_id = data.get("id")
    if not message_id:
        raise LLMInfrastructureError("Resend API response missing message id")

    return str(message_id)
