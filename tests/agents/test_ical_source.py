"""
Tests for the iCal/ICS source adapter.

Covers successful parse, field mapping, webcal URL normalization,
and health reporting for unreachable, malformed, and empty calendars.
"""

from datetime import datetime
from pathlib import Path

import respx
from httpx import Response

from scene_scout.agents.sources.ical import IcalSourceAdapter
from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.models.feed import FeedConfig, FeedStatus
from tests.conftest import TEST_RUN_ID

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ical"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _make_config(url: str = "https://example.com/events.ics") -> FeedConfig:
    return FeedConfig(
        id="ical_test",
        name="Library Calendar",
        url=url,
        city="New York",
        source_quality_score=0.8,
        source_type="ical",
    )


@respx.mock
async def test_successful_ics_returns_raw_feed_entries():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("valid_library_events.ics"))
    )

    adapter = IcalSourceAdapter()
    entries, report = await adapter.fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert report.status == FeedStatus.OK
    assert report.succeeded is True
    assert report.entries_fetched == 2
    assert len(entries) == 2


@respx.mock
async def test_vevent_fields_mapped_to_raw_feed_entry():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("valid_library_events.ics"))
    )

    entries, _ = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )
    first = entries[0]

    assert first.title == "Sandlot Storytime"
    assert first.link == "https://example.com/events/sandlot-storytime"
    assert "read-aloud" in first.description.lower()
    assert first.published_raw == "2026-07-15T15:00:00+00:00"
    assert first.author == "Library Programs"
    assert "Storytime" in first.categories
    assert first.feed_id == "ical_test"
    assert first.run_id == TEST_RUN_ID
    assert isinstance(first.fetched_at, datetime)


@respx.mock
async def test_all_day_event_published_raw_is_date_only():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("valid_library_events.ics"))
    )

    entries, _ = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries[1].published_raw == "2026-07-20"


@respx.mock
async def test_webcal_url_is_fetched_as_https():
    config = _make_config("webcal://example.com/events.ics")
    respx.get("https://example.com/events.ics").mock(
        return_value=Response(200, content=_fixture("valid_library_events.ics"))
    )

    entries, report = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert report.succeeded is True
    assert len(entries) == 2


@respx.mock
async def test_unreachable_ics_produces_failure_report():
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(503))

    entries, report = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.UNREACHABLE
    assert report.succeeded is False


@respx.mock
async def test_malformed_ics_produces_malformed_report():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, text=_fixture("malformed.ics"))
    )

    entries, report = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.MALFORMED


@respx.mock
async def test_empty_calendar_produces_empty_report():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("empty_calendar.ics"))
    )

    entries, report = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert entries == []
    assert report.status == FeedStatus.EMPTY
