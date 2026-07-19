"""Tests for email tracking and feedback web endpoints (Phase 8.1)."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from scene_scout.db.models import feedback_events, recommendation_history
from scene_scout.db.urls import database_feedback_url, database_history_url
from scene_scout.models.history import RecommendationRecord
from scene_scout.services.feedback import generate_feedback_token
from scene_scout.services.history import write_recommendations
from scene_scout.web.app import create_app
from tests.conftest import TEST_RUN_ID

RECOMMENDED_AT = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
REDIRECT_URL = "https://donyc.com/events/jazz-night"


@pytest.fixture
def tracking_client(migrated_databases: tuple) -> TestClient:
    return TestClient(create_app())


def _history_record(*, feedback_token: str) -> RecommendationRecord:
    return RecommendationRecord(
        feedback_token=feedback_token,
        event_id="sandlot-jazz-night",
        run_id=TEST_RUN_ID,
        rank=2,
        score=0.82,
        score_breakdown={"category_fit": 0.9, "novelty": 0.4},
        event_title="Jazz Night at the Sandlot",
        categories=["Jazz", "Music"],
        explanation="Strong jazz fit.",
        neighborhood_context="Silver Lake after dark.",
        sellout_risk="low",
        sellout_urgency_note=None,
        is_wildcard=False,
        recommended_at=RECOMMENDED_AT,
        feedback_signal=None,
    )


def test_track_logs_click_and_redirects(tracking_client: TestClient) -> None:
    token = generate_feedback_token()
    write_recommendations([_history_record(feedback_token=token)])

    response = tracking_client.get(
        "/track",
        params={
            "token": token,
            "signal": "click",
            "redirect": REDIRECT_URL,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == REDIRECT_URL

    engine = create_engine(database_feedback_url())
    history_engine = create_engine(database_history_url())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(feedback_events).where(feedback_events.c.token == token),
            ).one()
        with history_engine.connect() as conn:
            history_row = conn.execute(
                select(recommendation_history).where(
                    recommendation_history.c.feedback_token == token,
                ),
            ).one()
    finally:
        engine.dispose()
        history_engine.dispose()

    assert row.signal == "click"
    assert row.event_id == "sandlot-jazz-night"
    assert row.run_id == TEST_RUN_ID
    assert row.redirect_url == REDIRECT_URL
    assert history_row.feedback_signal == "click"


def test_feedback_logs_negative_and_returns_confirmation(
    tracking_client: TestClient,
) -> None:
    token = generate_feedback_token()
    write_recommendations([_history_record(feedback_token=token)])

    response = tracking_client.get(
        "/feedback",
        params={"token": token, "signal": "negative"},
    )

    assert response.status_code == 200
    assert "fewer events like this" in response.text

    engine = create_engine(database_feedback_url())
    history_engine = create_engine(database_history_url())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(feedback_events).where(feedback_events.c.token == token),
            ).one()
        with history_engine.connect() as conn:
            history_row = conn.execute(
                select(recommendation_history).where(
                    recommendation_history.c.feedback_token == token,
                ),
            ).one()
    finally:
        engine.dispose()
        history_engine.dispose()

    assert row.signal == "negative"
    assert history_row.feedback_signal == "negative"


def test_unknown_token_still_logs_feedback_and_returns_confirmation(
    tracking_client: TestClient,
) -> None:
    token = generate_feedback_token()

    response = tracking_client.get(
        "/feedback",
        params={"token": token, "signal": "negative"},
    )

    assert response.status_code == 200
    assert "match this link to a recent recommendation" in response.text

    engine = create_engine(database_feedback_url())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(feedback_events).where(feedback_events.c.token == token),
            ).one()
    finally:
        engine.dispose()

    assert row.signal == "negative"
    assert row.run_id == "unknown"


def test_unknown_token_click_redirects_when_url_valid(
    tracking_client: TestClient,
) -> None:
    token = generate_feedback_token()

    response = tracking_client.get(
        "/track",
        params={
            "token": token,
            "signal": "click",
            "redirect": REDIRECT_URL,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == REDIRECT_URL


def test_invalid_token_returns_confirmation_without_logging(
    tracking_client: TestClient,
) -> None:
    response = tracking_client.get(
        "/feedback",
        params={"token": "not-a-uuid", "signal": "negative"},
    )

    assert response.status_code == 400

    engine = create_engine(database_feedback_url())
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(feedback_events)).all()
    finally:
        engine.dispose()

    assert rows == []


def test_track_rejects_invalid_redirect(tracking_client: TestClient) -> None:
    token = generate_feedback_token()

    response = tracking_client.get(
        "/track",
        params={
            "token": token,
            "signal": "click",
            "redirect": "javascript:alert(1)",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_tracking_endpoints_exempt_from_basic_auth(
    tracking_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_PASSWORD", "secret-pass")
    token = generate_feedback_token()

    response = tracking_client.get(
        "/feedback",
        params={"token": token, "signal": "negative"},
    )

    assert response.status_code == 200


def test_track_accepts_encoded_redirect(tracking_client: TestClient) -> None:
    token = generate_feedback_token()
    encoded = f"https://example.com/event?ref={quote('jazz night')}"

    response = tracking_client.get(
        "/track",
        params={
            "token": token,
            "signal": "click",
            "redirect": encoded,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == encoded
