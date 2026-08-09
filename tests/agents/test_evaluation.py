"""
Tests for the Evaluation Agent.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scene_scout.agents import evaluation
from scene_scout.email_composer_config import evaluation_report_path
from scene_scout.models.curated import CuratedRecommendation
from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.evaluation import EvaluationLLMOutput, FlaggedRecommendation
from scene_scout.models.user import UserProfile
from scene_scout.services.feedback import generate_feedback_token
from scene_scout.services.llm import LLMValidationError
from tests.conftest import TEST_RUN_ID

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
EVENT_TIME = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)


def _profile() -> UserProfile:
    return UserProfile.model_validate(
        {
            "user_id": "user-123",
            "name": "Morgan",
            "email": "morgan@example.com",
            "home_city": "Los Angeles",
            "horizon_days": 14,
            "stated_interests": ["jazz"],
            "created_at": NOW,
            "last_updated": NOW,
        }
    )


def _rec(**overrides: object) -> CuratedRecommendation:
    event = EnrichedEvent.model_validate(
        {
            "id": "event-jazz-1",
            "title": "Silver Lake Jazz Night",
            "start_datetime": EVENT_TIME,
            "venue": "The Sandlot",
            "city": "Los Angeles",
            "url": "https://example.com/jazz-night",
            "is_free": False,
            "description": "An intimate jazz set under the floodlights.",
            "categories": ["Jazz"],
            "source_feeds": ["sandlot-pickup-league"],
            "source_count": 1,
            "best_source_feed": "sandlot-pickup-league",
            "source_quality_score": 0.8,
            "description_quality_score": 0.85,
            "low_information": False,
            "run_id": TEST_RUN_ID,
            "normalized_at": NOW,
            "vibe_tags": ["intimate"],
            "neighborhood_context": "Walkable from Echo Park.",
        }
    )
    payload = {
        "rank": 1,
        "event": event,
        "score": 0.82,
        "score_breakdown": {
            "category_fit": 0.8,
            "vibe_fit": 0.7,
            "semantic_similarity": 0.0,
            "performer_affinity": 0.5,
            "location": 1.0,
            "novelty": 1.0,
            "source_quality": 0.8,
            "source_coverage": 0.33,
            "description_quality": 0.85,
        },
        "explanation": "Strong jazz fit for your profile.",
        "neighborhood_context": "Walkable from Echo Park.",
        "sellout_risk": "low",
        "feedback_token": generate_feedback_token(),
        "is_wildcard": False,
        "run_id": TEST_RUN_ID,
        "recommended_at": NOW,
    }
    payload.update(overrides)
    return CuratedRecommendation.model_validate(payload)


@pytest.mark.asyncio
async def test_run_writes_evaluation_report_with_llm_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scene_scout.email_composer_config.PROJECT_ROOT",
        tmp_path,
    )
    llm_output = EvaluationLLMOutput(
        overall_quality=0.78,
        flagged_recommendations=[
            FlaggedRecommendation(
                rank=1,
                issue_type="generic_explanation",
                description="Explanation could apply to any jazz event.",
            )
        ],
        list_level_issues=["Limited category diversity."],
        summary="Mostly strong picks with one generic explanation.",
    )

    with patch(
        "scene_scout.agents.evaluation.complete",
        AsyncMock(return_value=llm_output),
    ):
        report = await evaluation.run([_rec()], _profile(), TEST_RUN_ID)

    assert report.overall_quality == pytest.approx(0.78)
    assert len(report.flagged_recommendations) == 1
    assert report.list_level_issues == ["Limited category diversity."]
    assert report.report_path == evaluation_report_path(TEST_RUN_ID)

    written = json.loads(report.report_path.read_text(encoding="utf-8"))
    assert written["run_id"] == TEST_RUN_ID
    assert written["flagged_recommendations"][0]["issue_type"] == "generic_explanation"


@pytest.mark.asyncio
async def test_run_empty_recommendations_writes_zero_quality_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scene_scout.email_composer_config.PROJECT_ROOT",
        tmp_path,
    )

    report = await evaluation.run([], _profile(), TEST_RUN_ID)

    assert report.recommendation_count == 0
    assert report.overall_quality == 0.0
    assert report.list_level_issues == ["No recommendations to evaluate."]
    assert report.report_path is not None
    assert report.report_path.is_file()


@pytest.mark.asyncio
async def test_run_uses_fallback_when_llm_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scene_scout.email_composer_config.PROJECT_ROOT",
        tmp_path,
    )

    with patch(
        "scene_scout.agents.evaluation.complete",
        AsyncMock(side_effect=LLMValidationError("invalid json")),
    ):
        report = await evaluation.run([_rec()], _profile(), TEST_RUN_ID)

    assert report.overall_quality == pytest.approx(0.5)
    assert "Evaluation LLM response could not be validated." in report.list_level_issues
    assert report.report_path is not None
