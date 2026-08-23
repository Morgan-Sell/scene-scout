"""
Feedback loop integration test (Phase 8.3).

Simulates a trimmed pipeline run with mocked LLM and feed I/O, records a click
through the tracking endpoint, applies profile updates from persisted feedback,
and verifies SQLite history/feedback stores plus the Chroma liked-events index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from scene_scout.agents import user_preference
from scene_scout.db.models import feedback_events, recommendation_history
from scene_scout.db.urls import database_feedback_url, database_history_url
from scene_scout.models.curated import CuratedRecommendation, CuratorResult
from scene_scout.models.email import EmailComposerResult
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.evaluation import EvaluationReport
from scene_scout.models.event import NormalizedEvent
from scene_scout.models.feedback import FeedbackEvent
from scene_scout.models.ranking import RankingExplanationLLMOutput
from scene_scout.models.user import UserProfile
from scene_scout.orchestrator import (
    Orchestrator,
    PreEnrichmentFilterResult,
    _collect_enrichment_batch_requests,
)
from scene_scout.services import chroma as chroma_service
from scene_scout.services.batch import BatchRequest, BatchResultItem, BatchResults
from scene_scout.services.cache import CacheService
from scene_scout.services.history import get_recent
from scene_scout.user_preference_config import FEEDBACK_CLICK_CATEGORY_DELTA
from scene_scout.web.app import create_app
from tests.conftest import TEST_RUN_ID

PIPELINE_PROMPT = (
    "Find me jazz nights and comedy shows around the sandlot — "
    "live music first, outdoor when you can."
)
REFERENCE_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
JAZZ_EVENT_ID = "sandlot-jazz-feedback-loop"
REDIRECT_URL = "https://example.com/jazz-night"
JAZZ_VECTOR = [1.0, 0.0, 0.0]
NEIGHBORHOOD_CONTEXT_JSON = (
    '{"neighborhood_context": "Walkable from the sandlot.", '
    '"neighborhood_confidence": 0.82}'
)


@dataclass
class FeedbackPipelineRun:
    """Artifacts from a trimmed feedback-loop pipeline run."""

    curator_result: CuratorResult
    geocode_calls: list[tuple[str, str]]
    email_recommendations: list[CuratedRecommendation]


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
def chroma_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    chroma_path = tmp_path / "vol-chroma"
    chroma_path.mkdir()
    monkeypatch.setenv("VOL_CHROMA_DIR", str(chroma_path))
    chroma_service.reset_chroma_client()
    yield chroma_path
    chroma_service.reset_chroma_client()


@pytest.fixture
def tracking_client(migrated_databases: tuple) -> TestClient:
    return TestClient(create_app())


def _feedback_profile() -> UserProfile:
    timestamp = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    return UserProfile(
        user_id="feedback-loop-user",
        name="Smalls",
        email="smalls@example.com",
        home_city="New York",
        horizon_days=14,
        stated_interests=["jazz", "comedy"],
        category_weights={"jazz": 0.5, "comedy": 0.8},
        vibe_preferences=["social"],
        created_at=timestamp,
        last_updated=timestamp,
    )


def _normalized_jazz_event() -> NormalizedEvent:
    return NormalizedEvent.model_validate(
        {
            "id": JAZZ_EVENT_ID,
            "title": "Sandlot Jazz Night",
            "start_datetime": REFERENCE_NOW + timedelta(days=2, hours=20),
            "venue": "The Sandlot",
            "city": "New York",
            "url": REDIRECT_URL,
            "is_free": False,
            "description": "An intimate jazz set under the floodlights.",
            "categories": ["Jazz"],
            "source_feeds": ["sandlot-pickup-league"],
            "source_count": 1,
            "best_source_feed": "sandlot-pickup-league",
            "source_quality_score": 0.8,
            "description_quality_score": 0.8,
            "low_information": False,
            "run_id": TEST_RUN_ID,
            "normalized_at": REFERENCE_NOW,
        }
    )


def _enriched_jazz_event(normalized: NormalizedEvent) -> EnrichedEvent:
    return EnrichedEvent.model_validate(
        {
            **normalized.model_dump(),
            "vibe_tags": ["intimate", "high-energy"],
            "neighborhood_context": "Walkable from the sandlot.",
            "top_performer_affinity": 0.6,
        }
    )


def _feedback_events_from_db() -> list[FeedbackEvent]:
    engine = create_engine(database_feedback_url())
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(feedback_events)).all()
    finally:
        engine.dispose()

    events: list[FeedbackEvent] = []
    for row in rows:
        score_breakdown = (
            json.loads(row.score_breakdown_json) if row.score_breakdown_json else None
        )
        events.append(
            FeedbackEvent(
                token=row.token,
                signal=row.signal,
                run_id=row.run_id,
                event_id=row.event_id,
                rank=row.rank,
                categories=json.loads(row.categories_json),
                score_breakdown=score_breakdown,
                redirect_url=row.redirect_url,
                received_at=datetime.fromisoformat(
                    row.received_at.replace("Z", "+00:00"),
                ),
            )
        )
    return events


async def _run_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    cache_db: Path,
    profile: UserProfile,
) -> FeedbackPipelineRun:
    normalized = _normalized_jazz_event()
    enriched = _enriched_jazz_event(normalized)
    captured: list[CuratorResult] = []
    geocode_calls: list[tuple[str, str]] = []
    email_recommendations: list[CuratedRecommendation] = []
    from scene_scout.agents import recommendation_curator

    original_curator_run = recommendation_curator.run
    original_collect = _collect_enrichment_batch_requests

    async def capture_curator(*args: object, **kwargs: object) -> CuratorResult:
        result = await original_curator_run(*args, **kwargs)
        captured.append(result)
        return result

    async def collect_without_geocoding(
        filtered: list[NormalizedEvent],
        *,
        cache: CacheService,
        stated_interests: list[str] | None,
        run_id: str,
    ) -> list[BatchRequest]:
        assert geocode_calls == []
        return await original_collect(
            filtered,
            cache=cache,
            stated_interests=stated_interests,
            run_id=run_id,
        )

    async def counting_geocode(
        venue: str,
        city: str,
        **kwargs: object,
    ) -> tuple[float, float]:
        geocode_calls.append((venue, city))
        return (40.7128, -74.0060)

    mock_neighborhood_strategy = AsyncMock()
    mock_neighborhood_strategy.submit = AsyncMock(
        return_value="batch-feedback-neighborhood"
    )
    mock_neighborhood_strategy.poll = AsyncMock(
        return_value=BatchResults(
            batch_id="batch-feedback-neighborhood",
            status="completed",
            results=[
                BatchResultItem(
                    custom_id=JAZZ_EVENT_ID,
                    content=NEIGHBORHOOD_CONTEXT_JSON,
                    success=True,
                )
            ],
        )
    )

    async def capture_email(
        recs: list[CuratedRecommendation],
        *args: object,
        **kwargs: object,
    ) -> EmailComposerResult:
        email_recommendations.extend(recs)
        return EmailComposerResult(
            html="<html><body>Allegra</body></html>",
            subject="[UAT] Allegra",
            preview_path=None,
            sent=False,
        )

    monkeypatch.setattr(
        "scene_scout.orchestrator.feed_scout.run",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator._resolve_user_profile",
        AsyncMock(return_value=profile),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.deduplication.run",
        AsyncMock(return_value=[normalized]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.description_quality.run",
        AsyncMock(return_value=[normalized]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.apply_pre_enrichment_filter",
        lambda events, run_id, **kwargs: PreEnrichmentFilterResult(
            events=[normalized],
            discards={
                "low_information": 0,
                "outside_coming_week": 0,
                "in_exclude_window": 0,
            },
        ),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator._collect_enrichment_batch_requests",
        collect_without_geocoding,
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator._poll_enrichment_batch",
        AsyncMock(
            return_value=BatchResults(
                batch_id="batch-feedback-loop",
                status="completed",
                results=[
                    BatchResultItem(
                        custom_id=f"vibe_classifier:{normalized.id}",
                        content='{"vibe_tags": ["intimate", "high-energy"]}',
                        success=True,
                    )
                ],
            )
        ),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.get_batch_strategy",
        lambda: type(
            "Strategy",
            (),
            {"submit": AsyncMock(return_value="batch-feedback-loop")},
        )(),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator._apply_enrichment_batch",
        AsyncMock(return_value=[enriched]),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.recommendation_curator.run",
        capture_curator,
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.CacheService",
        lambda run_id, db_path=None: CacheService(run_id=run_id, db_path=cache_db),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.email_composer.run",
        AsyncMock(side_effect=capture_email),
    )
    monkeypatch.setattr(
        "scene_scout.orchestrator.evaluation.run",
        AsyncMock(
            return_value=EvaluationReport(
                run_id=TEST_RUN_ID,
                recommendation_count=1,
                overall_quality=0.85,
                flagged_recommendations=[],
                list_level_issues=[],
                summary="Strong jazz recommendation list.",
            )
        ),
    )

    ranking_complete = AsyncMock(
        return_value=RankingExplanationLLMOutput(
            explanation="Strong jazz fit for your profile.",
        ),
    )

    with (
        patch("scene_scout.agents.ranking.complete", ranking_complete),
        patch(
            "scene_scout.agents.neighborhood_scout.geocode_venue",
            AsyncMock(side_effect=counting_geocode),
        ),
        patch(
            "scene_scout.agents.neighborhood_scout.get_nearby_pois",
            AsyncMock(return_value=[]),
        ),
        patch(
            "scene_scout.agents.neighborhood_scout.get_batch_strategy",
            lambda: mock_neighborhood_strategy,
        ),
    ):
        pipeline_result = await Orchestrator().run(PIPELINE_PROMPT)

    assert pipeline_result.curated_recommendations >= 1
    assert captured, "Expected curator output to be captured during pipeline run"
    assert len(geocode_calls) <= pipeline_result.curated_recommendations
    assert len(geocode_calls) == 1
    assert email_recommendations
    assert email_recommendations[0].neighborhood_context == "Walkable from the sandlot."
    return FeedbackPipelineRun(
        curator_result=captured[-1],
        geocode_calls=geocode_calls,
        email_recommendations=email_recommendations,
    )


@pytest.mark.asyncio
async def test_feedback_loop_click_updates_profile_and_chroma(
    profiles_dir: Path,
    chroma_dir: Path,
    migrated_databases: tuple,
    tracking_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _feedback_profile()
    user_preference.write_profile(profile)
    cache_db = tmp_path / "cache.db"

    pipeline_run = await _run_pipeline(monkeypatch, cache_db, profile)
    recommendation = pipeline_run.email_recommendations[0]
    assert recommendation.neighborhood_context == "Walkable from the sandlot."

    history_entries = get_recent(days=30)
    assert len(history_entries) >= 1
    history_entry = next(
        entry
        for entry in history_entries
        if entry.feedback_token == recommendation.feedback_token
    )
    assert history_entry.neighborhood_context == "Walkable from the sandlot."
    assert recommendation.event.categories == ["Jazz"]

    response = tracking_client.get(
        "/track",
        params={
            "token": recommendation.feedback_token,
            "signal": "click",
            "redirect": REDIRECT_URL,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == REDIRECT_URL

    signals = _feedback_events_from_db()
    assert len(signals) == 1
    assert signals[0].signal == "click"
    assert signals[0].event_id == JAZZ_EVENT_ID

    collection = chroma_service.get_liked_events_collection()
    assert collection.count() == 0

    with patch(
        "scene_scout.services.chroma.embed",
        return_value=JAZZ_VECTOR,
    ):
        updated_profile = user_preference.apply_feedback_signals(
            user_preference.load_profile(),
            signals,
            events_by_id={recommendation.event.id: recommendation.event},
            chroma_collection=collection,
            reference_time=REFERENCE_NOW,
        )

    assert updated_profile.category_weights["jazz"] == pytest.approx(
        0.5 + FEEDBACK_CLICK_CATEGORY_DELTA,
    )
    assert "intimate" in updated_profile.vibe_preferences
    assert "high-energy" in updated_profile.vibe_preferences

    assert collection.count() == 1
    stored = collection.get(ids=[JAZZ_EVENT_ID], include=["metadatas"])
    assert stored["ids"] == [JAZZ_EVENT_ID]
    assert stored["metadatas"][0]["title"] == "Sandlot Jazz Night"

    history_engine = create_engine(database_history_url())
    try:
        with history_engine.connect() as conn:
            history_row = conn.execute(
                select(recommendation_history).where(
                    recommendation_history.c.feedback_token
                    == recommendation.feedback_token,
                ),
            ).one()
    finally:
        history_engine.dispose()

    assert history_row.feedback_signal == "click"

    reloaded = user_preference.load_profile()
    assert reloaded.category_weights["jazz"] == updated_profile.category_weights["jazz"]
