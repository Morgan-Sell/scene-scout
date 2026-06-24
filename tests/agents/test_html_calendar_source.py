"""
Tests for the HTML calendar scraper source adapter.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
import respx
from httpx import Response

from scene_scout.agents.sources.html_calendar import HtmlCalendarSourceAdapter
from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.models.feed import FeedConfig, FeedStatus, ScrapeConfig
from tests.conftest import TEST_RUN_ID

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "html_calendar"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _make_config(
    *,
    scrape: ScrapeConfig,
    url: str = "https://www.ohmyrockness.com/shows",
    feed_id: str = "ohmyrockness_nyc",
) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        name="Oh My Rockness NYC",
        url=url,
        city="New York",
        source_quality_score=0.85,
        source_type="scrape",
        scrape=scrape,
    )


@respx.mock
async def test_omr_json_api_discover_and_map_entries():
    config = _make_config(
        scrape=ScrapeConfig(
            strategy="json_api",
            discover_json_url=True,
            require_csrf=True,
            json_items_path="shows",
            field_map={
                "title": "bands",
                "link": "url",
                "published_raw": "starts_at",
                "description": "venue.name",
            },
        )
    )
    landing = respx.get(config.url).mock(
        return_value=Response(200, text=_fixture("omr_landing.html"))
    )
    api = respx.get(
        "https://www.ohmyrockness.com/api/shows.json?index=true&page=1&per=50&regioned=1"
    ).mock(return_value=Response(200, json=json.loads(_fixture("omr_shows.json"))))

    entries, report = await HtmlCalendarSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert report.status == FeedStatus.OK
    assert report.entries_fetched == 2
    assert landing.called
    assert api.called
    assert api.calls.last.request.headers["x-csrf-token"] == "test-csrf-token-value"

    first = entries[0]
    assert first.title == "The Breeders"
    assert first.link == (
        "https://www.ohmyrockness.com/shows/2026/6/24/the-breeders-brooklyn-paramount"
    )
    assert first.published_raw == "2026-06-24T20:00:00-04:00"
    assert first.description == "Brooklyn Paramount"
    assert first.categories == ["music"]
    assert isinstance(first.fetched_at, datetime)


@respx.mock
async def test_css_scrape_maps_event_blocks():
    config = _make_config(
        url="https://www.dance.nyc/calendar/",
        feed_id="dance_nyc",
        scrape=ScrapeConfig(
            strategy="css",
            item_selector="div.event",
            title_selector="h3",
            link_selector="a",
            description_selector="p",
            date_selector="div.event_time",
        ),
    )
    respx.get(config.url).mock(
        return_value=Response(200, text=_fixture("dance_nyc_calendar.html"))
    )

    entries, report = await HtmlCalendarSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert report.succeeded is True
    assert len(entries) == 2
    assert entries[0].title == "Community Kundalini Yoga"
    assert entries[0].link.startswith(
        "https://www.dance.nyc/for-audiences/community-calendar/view/"
    )
    assert entries[0].published_raw == "6:00pm"
    assert entries[0].categories == []


@respx.mock
async def test_missing_scrape_config_returns_unreachable():
    config = FeedConfig(
        id="scrape_test",
        name="HTML Calendar",
        url="https://example.com/calendar",
        city="New York",
        source_quality_score=0.8,
        source_type="scrape",
    )

    entries, report = await HtmlCalendarSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.UNREACHABLE
    assert "scrape configuration is required" in report.error_message


@respx.mock
async def test_http_error_returns_unreachable_without_raising():
    config = _make_config(
        scrape=ScrapeConfig(
            strategy="json_api",
            json_url="/api/shows.json",
        )
    )
    respx.get(config.url).mock(return_value=Response(200, text="<html></html>"))
    respx.get("https://www.ohmyrockness.com/api/shows.json").mock(
        return_value=Response(503)
    )

    entries, report = await HtmlCalendarSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.UNREACHABLE
    assert "503" in report.error_message


@respx.mock
async def test_empty_json_results_return_empty_status():
    config = _make_config(
        scrape=ScrapeConfig(
            strategy="json_api",
            json_url="/api/shows.json",
            json_items_path="shows",
        )
    )
    respx.get(config.url).mock(return_value=Response(200, text="<html></html>"))
    respx.get("https://www.ohmyrockness.com/api/shows.json").mock(
        return_value=Response(200, json={"shows": []})
    )

    entries, report = await HtmlCalendarSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.EMPTY
