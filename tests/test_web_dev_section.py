"""
Tests for the web Dev Section API and service helpers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from scene_scout.models.feed import FeedConfig
from scene_scout.models.history import RecommendationHistoryEntry
from scene_scout.orchestrator import PipelineResult
from scene_scout.web import dev_service
from scene_scout.web.app import create_app

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def reset_dry_run_job() -> None:
    dev_service.reset_dry_run_job()
    yield
    dev_service.reset_dry_run_job()


@pytest.fixture
def cache_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "cache.db"
    from scene_scout.services.cache import CacheService

    CacheService(run_id="dev-section-test", db_path=db_path)
    return db_path


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def dev_logs(logs_dir: Path) -> Path:
    recent_run = "20260628-120000"
    older_run = "20260627-120000"
    recent_path = logs_dir / f"{recent_run}.jsonl"
    older_path = logs_dir / f"{older_run}.jsonl"
    recent_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": NOW.isoformat(),
                        "run_id": recent_run,
                        "agent": "feed_scout",
                        "level": "INFO",
                        "message": "Feed OK",
                        "data": {
                            "feed_id": "sandlot-pickup-league",
                            "feed_name": "Sandlot RSS",
                            "entries_fetched": 2,
                            "etag_supported": True,
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": NOW.isoformat(),
                        "run_id": recent_run,
                        "agent": "ranking",
                        "level": "WARNING",
                        "message": "Low score event",
                        "data": {"event_id": "evt-1"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    older_path.write_text(
        json.dumps(
            {
                "timestamp": (NOW - timedelta(days=1)).isoformat(),
                "run_id": older_run,
                "agent": "orchestrator",
                "level": "INFO",
                "message": "Pipeline started",
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return logs_dir


@pytest.fixture
def uat_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    output_dir = tmp_path / "output" / "uat_20260628-120000"
    output_dir.mkdir(parents=True)
    summary = {
        "run_id": "20260628-120000",
        "status": "completed",
        "dry_run": True,
        "after_description_quality": 10,
        "after_pre_enrichment_filter": 7,
        "seen_entries_hit_rate_pct": 42.5,
        "enrichment_cache_hit_rates_pct": {
            "performer": 80.0,
            "venue": 65.0,
            "vibe": 50.0,
        },
        "feed_health": [
            {
                "feed_id": "sandlot-pickup-league",
                "feed_name": "Sandlot RSS",
                "status": "ok",
                "entries_fetched": 2,
                "error_message": None,
            }
        ],
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    monkeypatch.setattr(dev_service, "_OUTPUT_DIR", tmp_path / "output")
    return summary_path


def test_dev_runs_lists_recent_logs(client: TestClient, dev_logs: Path) -> None:
    response = client.get("/api/dev/runs?limit=5")
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert len(runs) == 2
    assert runs[0]["run_id"] == "20260628-120000"
    assert runs[0]["entry_count"] == 2


def test_dev_logs_filters_by_agent_and_level(
    client: TestClient,
    dev_logs: Path,
) -> None:
    response = client.get(
        "/api/dev/logs",
        params={
            "run_id": "20260628-120000",
            "agent": "ranking",
            "level": "WARNING",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["agent"] == "ranking"
    assert data["entries"][0]["level"] == "WARNING"


def test_dev_feed_health_dashboard(
    client: TestClient,
    dev_logs: Path,
    uat_summary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dev_service,
        "load_feed_configs",
        lambda **kwargs: [
            FeedConfig(
                id="sandlot-pickup-league",
                name="Sandlot RSS",
                url="https://example.com/rss",
                city="Los Angeles",
                source_quality_score=0.8,
                active=True,
                source_type="rss",
            )
        ],
    )
    response = client.get("/api/dev/feed-health")
    assert response.status_code == 200
    data = response.json()
    assert data["latest_run_id"] == "20260628-120000"
    assert data["seen_entries_hit_rate_pct"] == 42.5
    assert data["post_date_filter_yield_pct"] == 70.0
    feed = next(
        row for row in data["feeds"] if row["feed_id"] == "sandlot-pickup-league"
    )
    assert feed["etag_supported"] is True
    assert feed["last_fetch_at"] is not None


def test_dev_cache_inspection(
    client: TestClient,
    uat_summary: Path,
    cache_db: Path,
) -> None:
    response = client.get("/api/dev/cache")
    assert response.status_code == 200
    data = response.json()
    assert data["latest_run_id"] == "20260628-120000"
    assert data["seen_entries_hit_rate_pct"] == 42.5
    cache_types = {row["cache_type"] for row in data["tables"]}
    assert "seen_entries" in cache_types
    assert "venue_cache" in cache_types


def test_dev_history_returns_recent_entries(
    client: TestClient,
    migration_dirs: tuple[Path, Path],
    migrated_databases: tuple[Path, Path],
) -> None:
    entry = RecommendationHistoryEntry(
        id=1,
        feedback_token="11111111-1111-1111-1111-111111111111",
        event_id="evt-jazz-1",
        run_id="20260628-120000",
        rank=1,
        score=0.82,
        score_breakdown={"category_fit": 0.8},
        event_title="Silver Lake Jazz Night",
        categories=["Jazz"],
        explanation="Strong jazz fit.",
        recommended_at=NOW,
        feedback_signal=None,
        is_wildcard=False,
    )
    with patch(
        "scene_scout.web.dev_service.history_service.get_recent",
        return_value=[entry],
    ):
        response = client.get("/api/dev/history?days=30")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["entries"][0]["event_title"] == "Silver Lake Jazz Night"


def test_dev_dry_run_starts_background_job(client: TestClient) -> None:
    result = PipelineResult(
        run_id="20260628-130000",
        user_prompt="jazz nights",
        email_preview_path="output/uat_20260628-130000/email_preview.html",
    )

    with patch(
        "scene_scout.web.dev_service.run_uat",
        AsyncMock(return_value=result),
    ):
        response = client.post("/api/dev/dry-run", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "running"


@pytest.mark.asyncio
async def test_start_dry_run_completes_and_records_preview(tmp_path: Path) -> None:
    preview_dir = tmp_path / "output" / "uat_20260628-130000"
    preview_dir.mkdir(parents=True)
    preview_path = preview_dir / "email_preview.html"
    preview_path.write_text("<html><body>Preview</body></html>", encoding="utf-8")

    result = PipelineResult(
        run_id="20260628-130000",
        user_prompt="jazz nights",
        email_preview_path=str(preview_path),
        curated_recommendations=3,
    )

    with patch(
        "scene_scout.web.dev_service.run_uat",
        AsyncMock(return_value=result),
    ):
        await dev_service.start_dry_run("Find jazz.")
        await asyncio_sleep_short()

    status = dev_service.get_dry_run_status()
    assert status["status"] == "completed"
    assert status["run_id"] == "20260628-130000"
    assert status["email_preview_path"] == str(preview_path)


async def asyncio_sleep_short() -> None:
    import asyncio

    for _ in range(20):
        if dev_service.get_dry_run_status()["status"] != "running":
            return
        await asyncio.sleep(0.05)


def test_dev_dry_run_preview_serves_html(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "20260628-130000"
    preview_dir = tmp_path / "output" / f"uat_{run_id}"
    preview_dir.mkdir(parents=True)
    preview_path = preview_dir / "email_preview.html"
    preview_path.write_text(
        "<html><body>Allegra preview</body></html>", encoding="utf-8"
    )
    monkeypatch.setattr(
        "scene_scout.web.dev_service.uat_output_dir",
        lambda rid: tmp_path / "output" / f"uat_{rid}",
    )

    response = client.get(f"/api/dev/dry-run/preview?run_id={run_id}")
    assert response.status_code == 200
    assert "Allegra preview" in response.text


def test_static_index_includes_dev_tab(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'data-tab="dev"' in response.text
    assert "Run logs" in response.text
    assert "/dev.js" in response.text
