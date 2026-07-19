"""
Tests for the Email Composer Agent and Resend service.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from scene_scout.agents import email_composer
from scene_scout.curator_config import load_curator_config
from scene_scout.email_composer_config import email_preview_path
from scene_scout.models.curated import CuratedRecommendation
from scene_scout.models.email import EmailComposerLLMOutput
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.user import UserProfile
from scene_scout.services.feedback import generate_feedback_token
from scene_scout.services.llm import LLMInfrastructureError, LLMValidationError
from tests.conftest import TEST_RUN_ID

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
EVENT_TIME = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)
TRACKING_BASE = "https://scenes.example.com"


def _profile(**overrides: object) -> UserProfile:
    payload = {
        "user_id": "user-123",
        "name": "Morgan",
        "email": "morgan@example.com",
        "home_city": "Los Angeles",
        "horizon_days": 14,
        "stated_interests": ["jazz"],
        "preferred_neighborhoods": ["Silver Lake"],
        "category_weights": {"jazz": 0.9},
        "vibe_preferences": ["intimate"],
        "excluded_categories": [],
        "created_at": NOW,
        "last_updated": NOW,
    }
    payload.update(overrides)
    return UserProfile.model_validate(payload)


def _event(**overrides: object) -> EnrichedEvent:
    payload = {
        "id": "event-jazz-1",
        "title": "Silver Lake Jazz Night",
        "start_datetime": EVENT_TIME,
        "venue": "The Sandlot",
        "city": "Los Angeles",
        "url": "https://example.com/jazz-night",
        "is_free": False,
        "price_cents": 2500,
        "description": "An intimate jazz set under the floodlights.",
        "categories": ["Jazz"],
        "source_feeds": ["sandlot-pickup-league"],
        "source_count": 1,
        "best_source_feed": "sandlot-pickup-league",
        "source_quality_score": 0.8,
        "description_quality_score": 0.9,
        "low_information": False,
        "run_id": TEST_RUN_ID,
        "normalized_at": NOW,
        "top_performer_affinity": 0.7,
        "vibe_tags": ["intimate"],
        "neighborhood_context": "Echo Park-adjacent backyard vibes.",
    }
    payload.update(overrides)
    return EnrichedEvent.model_validate(payload)


def _breakdown() -> dict[str, float]:
    return {
        "category_fit": 0.8,
        "vibe_fit": 0.7,
        "semantic_similarity": 0.0,
        "performer_affinity": 0.7,
        "location": 1.0,
        "novelty": 1.0,
        "source_quality": 0.8,
        "source_coverage": 0.33,
        "description_quality": 0.9,
    }


def _rec(**overrides: object) -> CuratedRecommendation:
    token = generate_feedback_token()
    payload = {
        "rank": 1,
        "event": _event(),
        "score": 0.85,
        "score_breakdown": _breakdown(),
        "explanation": "Strong jazz fit for your profile.",
        "neighborhood_context": "Echo Park-adjacent backyard vibes.",
        "sellout_risk": "low",
        "sellout_urgency_note": None,
        "feedback_token": token,
        "is_wildcard": False,
        "run_id": TEST_RUN_ID,
        "recommended_at": NOW,
    }
    payload.update(overrides)
    return CuratedRecommendation.model_validate(payload)


def test_format_event_datetime_renders_span_when_end_datetime_set() -> None:
    start = datetime(2026, 6, 27, 19, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 28, 23, 59, tzinfo=timezone.utc)

    formatted = email_composer._format_event_datetime(start, end)

    assert "Jun 27" in formatted
    assert "Jun 28" in formatted
    assert "–" in formatted


def test_render_html_includes_festival_date_span() -> None:
    start = datetime(2026, 6, 27, 19, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 28, 23, 59, tzinfo=timezone.utc)
    event = _event(start_datetime=start, end_datetime=end)
    recommendation = _rec(event=event)
    html_body = email_composer.render_html_email(
        recommendations=[recommendation],
        intro_paragraph="Here are your picks.",
        event_descriptions=["A two-night festival."],
        curator_name="Allegra",
        user_name="Morgan",
        tracking_base_url=TRACKING_BASE,
    )

    assert "Jun 27" in html_body
    assert "Jun 28" in html_body


def test_build_subject_prefixes_uat_run_id() -> None:
    subject = email_composer.build_subject("20260612-120000", user_name="Morgan")

    assert subject.startswith("[UAT 20260612-120000]")
    assert "Morgan" in subject


def test_build_track_url_encodes_redirect() -> None:
    url = email_composer.build_track_url(
        "11111111-1111-1111-1111-111111111111",
        "https://example.com/jazz?ref=scene",
        base_url=TRACKING_BASE,
    )

    assert url.startswith(f"{TRACKING_BASE}/track?")
    assert "token=11111111-1111-1111-1111-111111111111" in url
    assert "signal=click" in url
    assert "redirect=https%3A%2F%2Fexample.com%2Fjazz%3Fref%3Dscene" in url


def test_build_feedback_url_includes_negative_signal() -> None:
    url = email_composer.build_feedback_url(
        "22222222-2222-2222-2222-222222222222",
        base_url=TRACKING_BASE,
    )

    assert url == (
        f"{TRACKING_BASE}/feedback?"
        "token=22222222-2222-2222-2222-222222222222&signal=negative"
    )


def test_build_event_blocks_includes_pass_through_fields() -> None:
    recommendation = _rec(
        explanation="Strong jazz fit for your profile.",
        neighborhood_context="Walkable from Echo Park.",
        sellout_urgency_note="Tickets may sell out quickly.",
        is_wildcard=True,
    )

    block = email_composer.build_event_blocks([recommendation])

    assert "Silver Lake Jazz Night" in block
    assert "Strong jazz fit for your profile." in block
    assert "Walkable from Echo Park." in block
    assert "Tickets may sell out quickly." in block
    assert "Wildcard slot: yes" in block


def test_render_html_email_includes_tracking_links_and_pass_through_copy() -> None:
    recommendation = _rec()
    html_body = email_composer.render_html_email(
        [recommendation],
        intro_paragraph="This week has a strong jazz lean.",
        event_descriptions=["The quartet plays an intimate set under the lights."],
        curator_name="Allegra",
        user_name="Morgan",
        tracking_base_url=TRACKING_BASE,
    )

    assert "This week has a strong jazz lean." in html_body
    assert "The quartet plays an intimate set under the lights." in html_body
    assert "Strong jazz fit for your profile." in html_body
    assert "Echo Park-adjacent backyard vibes." in html_body
    assert f"{TRACKING_BASE}/track?" in html_body
    assert f"{TRACKING_BASE}/feedback?" in html_body
    assert recommendation.feedback_token in html_body


@pytest.mark.asyncio
async def test_run_dry_run_writes_preview_and_skips_resend(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    preview = tmp_path / "uat_run" / "email_preview.html"
    monkeypatch.setattr(
        email_composer,
        "email_preview_path",
        lambda run_id: preview,
    )
    llm_output = EmailComposerLLMOutput(
        intro_paragraph="A strong week for jazz in Silver Lake.",
        event_descriptions=["An intimate set worth catching."],
    )
    mock_complete = AsyncMock(return_value=llm_output)

    with patch("scene_scout.agents.email_composer.complete", mock_complete):
        result = await email_composer.run(
            [_rec()],
            _profile(),
            TEST_RUN_ID,
            curator_config=load_curator_config(),
            now=NOW,
        )

    assert result.sent is False
    assert result.resend_message_id is None
    assert preview.is_file()
    assert "An intimate set worth catching." in preview.read_text(encoding="utf-8")
    mock_complete.assert_awaited_once()


@pytest.mark.asyncio
@respx.mock
async def test_run_sends_via_resend_when_not_dry_run(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("USER_EMAIL", "user@example.com")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "allegra@scenes.example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    preview = tmp_path / "email_preview.html"
    monkeypatch.setattr(
        email_composer,
        "email_preview_path",
        lambda run_id: preview,
    )

    route = respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(200, json={"id": "msg_123"})
    )
    llm_output = EmailComposerLLMOutput(
        intro_paragraph="Jazz week is here.",
        event_descriptions=["The quartet plays Silver Lake."],
    )

    with patch(
        "scene_scout.agents.email_composer.complete",
        AsyncMock(return_value=llm_output),
    ):
        result = await email_composer.run(
            [_rec()],
            _profile(),
            TEST_RUN_ID,
            now=NOW,
        )

    assert result.sent is True
    assert result.resend_message_id == "msg_123"
    assert route.called
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer re_test_key"
    payload = json.loads(request.content)
    assert payload["to"] == ["user@example.com"]
    assert payload["from"] == "allegra@scenes.example.com"
    assert payload["subject"].startswith(f"[UAT {TEST_RUN_ID}]")


@pytest.mark.asyncio
@respx.mock
async def test_run_raises_infrastructure_error_on_resend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("USER_EMAIL", "user@example.com")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "allegra@scenes.example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    llm_output = EmailComposerLLMOutput(
        intro_paragraph="Jazz week is here.",
        event_descriptions=["The quartet plays Silver Lake."],
    )

    with (
        patch(
            "scene_scout.agents.email_composer.complete",
            AsyncMock(return_value=llm_output),
        ),
        patch(
            "scene_scout.agents.email_composer.email_preview_path",
            lambda run_id: email_preview_path("preview-only"),
        ),
        pytest.raises(LLMInfrastructureError, match="Resend API returned 500"),
    ):
        await email_composer.run([_rec()], _profile(), TEST_RUN_ID, now=NOW)


def test_render_html_email_rejects_mismatched_description_count() -> None:
    with pytest.raises(LLMValidationError, match="event descriptions"):
        email_composer.render_html_email(
            [_rec()],
            intro_paragraph="Intro",
            event_descriptions=[],
            curator_name="Allegra",
            user_name="Morgan",
        )


def test_align_event_descriptions_pads_with_fallbacks() -> None:
    recommendations = [_rec(rank=1), _rec(rank=2, event=_event(title="Second Show"))]

    aligned, fallback_count = email_composer.align_event_descriptions(
        recommendations,
        [],
    )

    assert len(aligned) == 2
    assert fallback_count == 2
    assert aligned[0]
    assert aligned[1]


def test_fallback_event_description_prefers_event_description() -> None:
    recommendation = _rec(
        event=_event(description="Doors at 8. Live brass ensemble."),
        explanation="Strong jazz fit for your profile.",
    )

    assert (
        email_composer.fallback_event_description(recommendation)
        == "Doors at 8. Live brass ensemble."
    )


@pytest.mark.asyncio
async def test_run_uses_fallback_descriptions_when_llm_returns_empty_list(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    preview = tmp_path / "uat_run" / "email_preview.html"
    monkeypatch.setattr(
        email_composer,
        "email_preview_path",
        lambda run_id: preview,
    )
    llm_output = EmailComposerLLMOutput(
        intro_paragraph="A strong week for jazz in Silver Lake.",
        event_descriptions=[],
    )

    with patch(
        "scene_scout.agents.email_composer.complete",
        AsyncMock(return_value=llm_output),
    ):
        result = await email_composer.run(
            [_rec()],
            _profile(),
            TEST_RUN_ID,
            curator_config=load_curator_config(),
            now=NOW,
        )

    assert result.sent is False
    assert preview.is_file()
    assert "Strong jazz fit for your profile." in preview.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_completes_when_llm_returns_invalid_json(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    preview = tmp_path / "uat_run" / "email_preview.html"
    monkeypatch.setattr(
        email_composer,
        "email_preview_path",
        lambda run_id: preview,
    )

    with patch(
        "scene_scout.agents.email_composer.complete",
        AsyncMock(side_effect=LLMValidationError("LLM response is not valid JSON")),
    ):
        result = await email_composer.run(
            [_rec()],
            _profile(),
            TEST_RUN_ID,
            curator_config=load_curator_config(),
            now=NOW,
        )

    assert result.sent is False
    assert preview.is_file()
    assert "Allegra picked 1 event" in preview.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_empty_recommendations_writes_preview_without_llm(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    preview = tmp_path / "empty_preview.html"
    monkeypatch.setattr(
        email_composer,
        "email_preview_path",
        lambda run_id: preview,
    )

    result = await email_composer.run([], _profile(), TEST_RUN_ID, now=NOW)

    assert result.sent is False
    assert preview.is_file()
    assert "did not find events" in preview.read_text(encoding="utf-8")
