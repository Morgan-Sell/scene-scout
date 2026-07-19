"""
Feedback and click tracking endpoints for SceneScout email links.

Served by the FastAPI web app locally and on Modal in production.
"""

from __future__ import annotations

import html
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from scene_scout.logging import get_logger
from scene_scout.models.feedback import FeedbackEvent, FeedbackSignal
from scene_scout.services import feedback as feedback_service
from scene_scout.services import history as history_service

router = APIRouter(tags=["tracking"])

_UNKNOWN_RUN_ID = "unknown"
_TRACKING_LOGGER = "tracking"


def _is_http_url(url: str | None) -> bool:
    if not url or not url.strip():
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_token(token: str | None) -> str | None:
    if not token or not token.strip():
        return None
    try:
        return str(uuid.UUID(token.strip()))
    except ValueError:
        return None


def _parse_signal(raw: str | None, expected: FeedbackSignal) -> FeedbackSignal | None:
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized == expected:
        return expected
    return None


def _feedback_confirmation_html(*, known: bool) -> str:
    if known:
        message = "Got it — we'll show you fewer events like this."
    else:
        message = (
            "Got it — we couldn't match this link to a recent recommendation, "
            "but your preference was recorded."
        )
    safe_message = html.escape(message)
    body_style = (
        "font-family: Georgia, serif; color: #1a1a1a; line-height: 1.6; "
        "max-width: 640px; margin: 48px auto; padding: 24px;"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SceneScout</title>
</head>
<body style="{body_style}">
<p>{safe_message}</p>
<p style="color: #A09080;">— Allegra</p>
</body>
</html>"""


def record_feedback_signal(
    *,
    token: str,
    signal: FeedbackSignal,
    redirect_url: str | None = None,
) -> bool:
    """Log a feedback event and update history when the token is known.

    Returns
    -------
    bool
        ``True`` when the token matched a recommendation history row.
    """
    entry = history_service.get_entry_by_feedback_token(token)
    if entry is not None:
        feedback_service.log_signal(
            FeedbackEvent(
                token=token,
                signal=signal,
                run_id=entry.run_id,
                event_id=entry.event_id,
                rank=entry.rank,
                categories=list(entry.categories),
                score_breakdown=dict(entry.score_breakdown),
                redirect_url=redirect_url,
            ),
            run_id=entry.run_id,
        )
        history_service.update_feedback(token, signal, run_id=entry.run_id)
        return True

    feedback_service.log_signal(
        FeedbackEvent(
            token=token,
            signal=signal,
            run_id=_UNKNOWN_RUN_ID,
            redirect_url=redirect_url,
        ),
    )
    logger = get_logger(_TRACKING_LOGGER)
    logger.warning(
        "Feedback token not found in recommendation history",
        data={"token": token, "signal": signal},
    )
    return False


@router.get("/track")
async def track_click(
    token: str | None = Query(default=None),
    signal: str | None = Query(default=None),
    redirect: str | None = Query(default=None),
) -> Response:
    """Log a click signal and redirect to the event URL."""
    parsed_token = _parse_token(token)
    if parsed_token is None:
        return HTMLResponse(
            _feedback_confirmation_html(known=False),
            status_code=400,
        )

    parsed_signal = _parse_signal(signal, "click")
    if parsed_signal is None:
        return HTMLResponse(
            _feedback_confirmation_html(known=False),
            status_code=400,
        )

    if not _is_http_url(redirect):
        return HTMLResponse(
            _feedback_confirmation_html(known=False),
            status_code=400,
        )

    record_feedback_signal(
        token=parsed_token,
        signal=parsed_signal,
        redirect_url=redirect,
    )
    return RedirectResponse(url=redirect.strip(), status_code=302)


@router.get("/feedback")
async def feedback_negative(
    token: str | None = Query(default=None),
    signal: str | None = Query(default=None),
) -> HTMLResponse:
    """Log a negative feedback signal and return a confirmation page."""
    parsed_token = _parse_token(token)
    if parsed_token is None:
        return HTMLResponse(
            _feedback_confirmation_html(known=False),
            status_code=400,
        )

    parsed_signal = _parse_signal(signal, "negative")
    if parsed_signal is None:
        return HTMLResponse(
            _feedback_confirmation_html(known=False),
            status_code=400,
        )

    known = record_feedback_signal(token=parsed_token, signal=parsed_signal)
    return HTMLResponse(_feedback_confirmation_html(known=known))
