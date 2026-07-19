"""
Tests for the iCal/ICS source adapter.

Covers successful parse, field mapping, webcal URL normalization,
window pre-filtering, and health reporting for unreachable, malformed,
and empty calendars.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import respx
from httpx import Response
from icalendar import Calendar

from scene_scout.agents.sources.ical import (
    IcalSourceAdapter,
    _filter_vevents_by_window,
)
from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.models.feed import FeedConfig, FeedStatus
from tests.conftest import TEST_RUN_ID

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ical"
_ICAL_REFERENCE_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
_ICAL_TEST_HORIZON_DAYS = 7


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


@pytest.fixture(autouse=True)
def ical_reference_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Anchor iCal window filtering so fixture dates stay in-window."""
    monkeypatch.setattr(
        "scene_scout.agents.sources.ical._utc_now",
        lambda: _ICAL_REFERENCE_NOW,
    )
    return _ICAL_REFERENCE_NOW


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
    assert first.published_raw == "2026-07-13T15:00:00+00:00"
    assert first.author == "Library Programs"
    assert "Storytime" in first.categories
    assert first.source_type == "ical"
    assert first.event_venue == "Brooklyn Public Library - Brower Park Library"
    assert first.event_city == "New York"
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

    assert entries[1].published_raw == "2026-07-15"


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
    assert report.error_message == "Calendar returned no VEVENT entries"


@respx.mock
async def test_ical_prefilter_keeps_in_window_vevents():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("mixed_window_events.ics"))
    )

    entries, report = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    titles = {entry.title for entry in entries}
    assert titles == {"Near Term Concert", "All Day Block Party"}
    assert report.entries_fetched == 2
    assert report.status == FeedStatus.OK


@respx.mock
async def test_ical_prefilter_drops_out_of_window_vevents():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("mixed_window_events.ics"))
    )

    entries, _ = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert all(entry.title != "Far Future Festival" for entry in entries)


@respx.mock
async def test_ical_prefilter_all_day_event_respects_window():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("past_and_future.ics"))
    )

    entries, report = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert len(entries) == 1
    assert entries[0].title == "Tomorrow Show"
    assert report.entries_fetched == 1


@respx.mock
async def test_ical_prefilter_empty_after_filter_returns_empty_status():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("all_out_of_window.ics"))
    )

    entries, report = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(horizon_days=_ICAL_TEST_HORIZON_DAYS),
    )

    assert entries == []
    assert report.status == FeedStatus.EMPTY
    assert report.entries_fetched == 0
    assert (
        report.error_message
        == f"No VEVENT entries within {_ICAL_TEST_HORIZON_DAYS}-day window"
    )


@respx.mock
async def test_ical_prefilter_report_entries_fetched_is_post_filter_count():
    config = _make_config()
    respx.get(config.url).mock(
        return_value=Response(200, content=_fixture("mixed_window_events.ics"))
    )

    _, report = await IcalSourceAdapter().fetch(
        config,
        TEST_RUN_ID,
        cache_hooks=CacheHooks(),
    )

    assert report.entries_fetched == 2


def test_filter_vevents_by_window_unit() -> None:
    calendar = Calendar.from_ical(_fixture("mixed_window_events.ics"))
    vevents = list(calendar.walk("VEVENT"))

    kept = _filter_vevents_by_window(
        vevents,
        now=_ICAL_REFERENCE_NOW,
        window_days=_ICAL_TEST_HORIZON_DAYS,
        feed_id="ical_test",
    )

    kept_titles = {str(component.get("summary")) for component in kept}
    assert kept_titles == {"Near Term Concert", "All Day Block Party"}
