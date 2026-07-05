"""
Tests for the Event API source adapter (Eventbrite).

All HTTP is mocked via respx; no live API token required.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
import respx
from httpx import Response

from scene_scout.agents.sources.event_api import EventApiSourceAdapter
from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.models.feed import FeedConfig, FeedStatus
from tests.conftest import TEST_RUN_ID

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "event_api"
_SEARCH_URL = "https://www.eventbriteapi.com/v3/events/search/"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def _make_config(
    url: str = "eventbrite://events/search",
    *,
    city: str = "New York",
    cursor: str | None = None,
) -> FeedConfig:
    return FeedConfig(
        id="eventbrite_test",
        name="Eventbrite NYC",
        url=url,
        city=city,
        source_quality_score=0.7,
        source_type="api",
        cursor=cursor,
    )


@pytest.fixture
def eventbrite_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a fake Eventbrite token for adapter tests."""
    monkeypatch.setenv("EVENTBRITE_API_TOKEN", "test-eventbrite-token")


@respx.mock
async def test_successful_eventbrite_search_returns_entries(eventbrite_token):
    config = _make_config()
    route = respx.get(_SEARCH_URL).mock(
        return_value=Response(200, json=_fixture("eventbrite_search_page1.json"))
    )

    entries, report = await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert report.status == FeedStatus.OK
    assert report.succeeded is True
    assert report.entries_fetched == 2
    assert len(entries) == 2
    assert route.called
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer test-eventbrite-token"


@respx.mock
async def test_eventbrite_fields_mapped_to_raw_feed_entry(eventbrite_token):
    config = _make_config()
    respx.get(_SEARCH_URL).mock(
        return_value=Response(200, json=_fixture("eventbrite_search_page1.json"))
    )

    entries, _ = await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )
    first = entries[0]

    assert first.title == "Sandlot Summer Concert"
    assert (
        first.link == "https://www.eventbrite.com/e/sandlot-summer-concert-123456789012"
    )
    assert "live music" in first.description.lower()
    assert first.description.startswith("Venue: Neighborhood Ball Field")
    assert first.published_raw == "2026-07-18T19:00:00"
    assert first.author == "Neighborhood Events Co"
    assert first.categories == ["Music"]
    assert first.feed_id == "eventbrite_test"
    assert first.run_id == TEST_RUN_ID
    assert isinstance(first.fetched_at, datetime)


@respx.mock
async def test_eventbrite_geo_filter_uses_new_york_params(eventbrite_token):
    config = _make_config(city="New York")
    route = respx.get(_SEARCH_URL).mock(
        return_value=Response(200, json=_fixture("eventbrite_search_page1.json"))
    )

    await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    params = dict(route.calls.last.request.url.params)
    assert params["location.address"] == "New York, NY"
    assert params["location.within"] == "50km"
    assert params["page"] == "1"
    assert params["expand"] == "organizer,category,venue"


@respx.mock
async def test_eventbrite_cursor_starts_at_configured_page(eventbrite_token):
    config = _make_config(cursor="2")
    route = respx.get(_SEARCH_URL).mock(
        return_value=Response(200, json=_fixture("eventbrite_search_page2.json"))
    )

    entries, report = await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert report.succeeded is True
    assert len(entries) == 1
    assert entries[0].title == "Page Two Showcase"
    assert route.calls.last.request.url.params.get("page") == "2"


@respx.mock
async def test_missing_eventbrite_token_returns_unreachable(monkeypatch):
    monkeypatch.delenv("EVENTBRITE_API_TOKEN", raising=False)
    config = _make_config()

    entries, report = await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.UNREACHABLE
    assert "EVENTBRITE_API_TOKEN" in report.error_message


@respx.mock
async def test_eventbrite_http_error_returns_unreachable(eventbrite_token):
    config = _make_config()
    respx.get(_SEARCH_URL).mock(return_value=Response(401))

    entries, report = await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.UNREACHABLE
    assert "401" in report.error_message


@respx.mock
async def test_eventbrite_empty_results_return_empty_status(eventbrite_token):
    config = _make_config()
    respx.get(_SEARCH_URL).mock(
        return_value=Response(200, json=_fixture("eventbrite_search_empty.json"))
    )

    entries, report = await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.EMPTY


@respx.mock
async def test_unsupported_api_platform_returns_unreachable(eventbrite_token):
    config = _make_config(url="songkick://events/search")

    entries, report = await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.UNREACHABLE
    assert "Unsupported API platform" in report.error_message


@respx.mock
async def test_eventbrite_paginates_when_has_more_items(eventbrite_token):
    config = _make_config()
    page1 = _fixture("eventbrite_search_page1.json")
    page1["pagination"]["has_more_items"] = True
    page1["pagination"]["page_count"] = 2

    route = respx.get(_SEARCH_URL).mock(
        side_effect=[
            Response(200, json=page1),
            Response(200, json=_fixture("eventbrite_search_page2.json")),
        ]
    )

    entries, report = await EventApiSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert report.succeeded is True
    assert len(entries) == 3
    assert len(route.calls) == 2
    assert route.calls[0].request.url.params.get("page") == "1"
    assert route.calls[1].request.url.params.get("page") == "2"
