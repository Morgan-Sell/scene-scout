"""
Tests for the SceneScout web application (FastAPI onboarding and profile API).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from scene_scout.models.user import (
    DEFAULT_HORIZON_DAYS,
    HORIZON_DAYS_MAX,
    HORIZON_DAYS_MIN,
    UserProfile,
)
from scene_scout.services.llm import LLMInfrastructureError, LLMValidationError
from scene_scout.web.app import (
    create_app,
    validate_onboarding_inputs,
    validate_onboarding_location,
)


def _onboarding_payload(**overrides: object) -> dict[str, object]:
    payload = {
        "home_city": "New York",
        "horizon_days": DEFAULT_HORIZON_DAYS,
        "name": "Smalls",
        "email": "smalls@example.com",
        "prompt": "I love jazz in Silver Lake.",
    }
    payload.update(overrides)
    return payload


def _sample_profile() -> UserProfile:
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    return UserProfile(
        user_id="smalls-001",
        name="Smalls",
        email="smalls@example.com",
        home_city="New York",
        horizon_days=DEFAULT_HORIZON_DAYS,
        stated_interests=["jazz"],
        stated_dislikes=["EDM festivals"],
        preferred_neighborhoods=["Silver Lake"],
        max_travel_minutes=45,
        budget_ceiling_cents=5000,
        excluded_categories=["nightclub"],
        category_weights={"jazz": 0.9},
        vibe_preferences=["intimate"],
        created_at=now,
        last_updated=now,
        profile_version=1,
    )


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profiles_path = tmp_path / "vol-profiles"
    profiles_path.mkdir()
    monkeypatch.setattr(
        "scene_scout.agents.user_preference.vol_profiles_dir",
        lambda: profiles_path,
    )
    return profiles_path


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_validate_onboarding_location_rejects_empty_city() -> None:
    assert validate_onboarding_location("  ", 14) == "Home city is required."


def test_validate_onboarding_location_rejects_horizon_out_of_range() -> None:
    assert (
        validate_onboarding_location("New York", HORIZON_DAYS_MAX + 1)
        == f"Horizon must be between {HORIZON_DAYS_MIN} and {HORIZON_DAYS_MAX} days."
    )


def test_validate_onboarding_inputs_rejects_blank_name() -> None:
    assert (
        validate_onboarding_inputs(
            "  ",
            "a@b.com",
            "prompt",
            home_city="New York",
            horizon_days=14,
        )
        == "Name is required."
    )


def test_validate_onboarding_inputs_rejects_invalid_email() -> None:
    assert (
        validate_onboarding_inputs(
            "Smalls",
            "not-an-email",
            "prompt",
            home_city="New York",
            horizon_days=14,
        )
        == "Email must look like a valid address."
    )


def test_validate_onboarding_inputs_accepts_valid() -> None:
    assert (
        validate_onboarding_inputs(
            "Smalls",
            "smalls@example.com",
            "jazz",
            home_city="New York",
            horizon_days=14,
        )
        is None
    )


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_profile_returns_404_when_missing(
    client: TestClient,
    profiles_dir: Path,
) -> None:
    response = client.get("/api/profile")
    assert response.status_code == 404
    assert response.json()["error"] == "No profile saved yet."


def test_profile_returns_saved_profile(
    client: TestClient,
    profiles_dir: Path,
) -> None:
    profile = _sample_profile()
    (profiles_dir / "profile.json").write_text(
        profile.model_dump_json(),
        encoding="utf-8",
    )

    response = client.get("/api/profile")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Smalls"
    assert data["home_city"] == "New York"
    assert data["horizon_days"] == DEFAULT_HORIZON_DAYS
    assert data["stated_interests"] == ["jazz"]


def test_onboarding_rejects_empty_city(client: TestClient) -> None:
    response = client.post(
        "/api/onboarding",
        json=_onboarding_payload(home_city=" "),
    )
    assert response.status_code == 422
    assert response.json()["error"] == "Home city is required."


def test_onboarding_rejects_invalid_horizon(client: TestClient) -> None:
    response = client.post(
        "/api/onboarding",
        json=_onboarding_payload(horizon_days=HORIZON_DAYS_MAX + 1),
    )
    assert response.status_code == 422
    assert "Horizon must be between" in response.json()["error"]


def test_onboarding_rejects_missing_name(client: TestClient) -> None:
    response = client.post(
        "/api/onboarding",
        json=_onboarding_payload(name=" "),
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Name is required."


def test_onboarding_success(
    client: TestClient,
    profiles_dir: Path,
) -> None:
    profile = _sample_profile()
    with patch(
        "scene_scout.web.app.user_preference.parse_cold_start",
        AsyncMock(return_value=profile),
    ) as mock_parse:
        response = client.post(
            "/api/onboarding",
            json=_onboarding_payload(),
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Smalls"
    assert response.json()["home_city"] == "New York"
    mock_parse.assert_awaited_once_with(
        name="Smalls",
        email="smalls@example.com",
        prompt="I love jazz in Silver Lake.",
        run_id=mock_parse.await_args.kwargs["run_id"],
        home_city="New York",
        horizon_days=DEFAULT_HORIZON_DAYS,
    )


def test_onboarding_llm_infrastructure_error_returns_502(client: TestClient) -> None:
    with patch(
        "scene_scout.web.app.user_preference.parse_cold_start",
        AsyncMock(side_effect=LLMInfrastructureError("provider down")),
    ):
        response = client.post(
            "/api/onboarding",
            json=_onboarding_payload(prompt="jazz"),
        )

    assert response.status_code == 502
    assert "provider down" in response.json()["error"]


def test_onboarding_llm_validation_error_returns_422(client: TestClient) -> None:
    with patch(
        "scene_scout.web.app.user_preference.parse_cold_start",
        AsyncMock(side_effect=LLMValidationError("bad llm json")),
    ):
        response = client.post(
            "/api/onboarding",
            json=_onboarding_payload(prompt="jazz"),
        )

    assert response.status_code == 422
    assert response.json()["error"] == "bad llm json"


def test_static_index_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "SceneScout" in response.text
    assert "Home city" in response.text
    assert "Horizon (days out)" in response.text
    assert "Let Allegra in" in response.text


def test_basic_auth_required_when_password_set(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_PASSWORD", "secret-pass")

    unauthenticated = client.get("/")
    assert unauthenticated.status_code == 401

    authenticated = client.get(
        "/",
        auth=("scenescout", "secret-pass"),
    )
    assert authenticated.status_code == 200


def test_health_exempt_from_basic_auth(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_PASSWORD", "secret-pass")
    response = client.get("/health")
    assert response.status_code == 200
