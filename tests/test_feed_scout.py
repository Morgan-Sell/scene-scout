"""
Tests for the Feed Scout Agent.

Covers: successful fetch, ETag/304 change detection, all failure modes,
multi-feed isolation, run_id propagation, validate_feed(), and the
FeedStatus.UNCHANGED path.

All HTTP responses are mocked via respx so tests run offline and
deterministically. We test our logic, not feedparser or httpx.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import respx
from httpx import Response

from scene_scout.agents import feed_scout
from scene_scout.config import load_feed_configs
from scene_scout.models.feed import FeedConfig, FeedStatus

TEST_RUN_ID = "20250606-120000"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Events Feed</title>
    <link>https://example.com</link>
    <description>A test feed</description>
    <item>
      <title>Jazz Night at the Echo</title>
      <link>https://example.com/jazz-night</link>
      <description>An intimate jazz evening featuring local quartet.</description>
      <pubDate>Fri, 06 Jun 2025 20:00:00 +0000</pubDate>
      <category>Music</category>
      <category>Jazz</category>
    </item>
    <item>
      <title>Sunday Film Screening</title>
      <link>https://example.com/film-screening</link>
      <description>Independent short films from LA filmmakers.</description>
      <pubDate>Sun, 08 Jun 2025 18:00:00 +0000</pubDate>
      <category>Film</category>
    </item>
  </channel>
</rss>"""

MALFORMED_RSS = "This is not XML or RSS at all."

EMPTY_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Empty Feed</title>
  </channel>
</rss>"""


def _make_config(
    url: str = "https://example.com/feed",
    feed_id: str = "test_feed",
) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        name="Test Feed",
        url=url,
        city="Los Angeles",
        source_quality_score=0.8,
        active=True,
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def test_load_feed_configs_returns_active_feeds():
    configs = load_feed_configs()
    assert len(configs) > 0
    assert all(c.active for c in configs)


def test_load_feed_configs_validates_schema():
    configs = load_feed_configs()
    for config in configs:
        assert isinstance(config.id, str) and config.id
        assert isinstance(config.url, str) and config.url
        assert 0.0 <= config.source_quality_score <= 1.0
        assert config.cursor is None  # Always None for RSS feeds


def test_load_feed_configs_raises_on_missing_file():
    with pytest.raises(FileNotFoundError):
        load_feed_configs(Path("/nonexistent/feeds.yaml"))


# ---------------------------------------------------------------------------
# Successful fetch
# ---------------------------------------------------------------------------

@respx.mock
async def test_successful_feed_returns_entries():
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=MINIMAL_RSS))

    entries, reports = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert len(reports) == 1
    assert reports[0].status == FeedStatus.OK
    assert reports[0].succeeded is True
    assert reports[0].entries_fetched == 2
    assert len(entries) == 2


@respx.mock
async def test_raw_entry_fields_preserved_faithfully():
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=MINIMAL_RSS))

    entries, _ = await feed_scout.run([config], run_id=TEST_RUN_ID)
    first = entries[0]

    assert first.title == "Jazz Night at the Echo"
    assert first.link == "https://example.com/jazz-night"
    assert "jazz evening" in first.description.lower()
    assert "Music" in first.categories or "Jazz" in first.categories
    assert isinstance(first.published_raw, str)  # Not parsed — preserved as string
    assert first.feed_id == "test_feed"
    assert first.run_id == TEST_RUN_ID
    assert isinstance(first.fetched_at, datetime)


@respx.mock
async def test_run_id_attached_to_every_entry():
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=MINIMAL_RSS))

    entries, _ = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert all(e.run_id == TEST_RUN_ID for e in entries)


# ---------------------------------------------------------------------------
# ETag / 304 change detection
# ---------------------------------------------------------------------------

@respx.mock
async def test_etag_stored_after_successful_fetch():
    """ETag and Last-Modified values from a successful response are stored."""
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(
        200,
        text=MINIMAL_RSS,
        headers={"ETag": '"abc123"', "Last-Modified": "Mon, 01 Jan 2025 00:00:00 GMT"},
    ))

    stored = {}

    def store_etag(feed_id, etag, last_modified):
        stored[feed_id] = (etag, last_modified)

    await feed_scout.run([config], run_id=TEST_RUN_ID, store_feed_etag=store_etag)

    assert config.id in stored
    assert stored[config.id] == ('"abc123"', "Mon, 01 Jan 2025 00:00:00 GMT")


@respx.mock
async def test_conditional_headers_sent_when_etag_cached():
    """If-None-Match and If-Modified-Since headers are sent when cached values exist."""
    config = _make_config()
    captured_headers: dict = {}

    def capture_request(request):
        captured_headers.update(dict(request.headers))
        return Response(200, text=MINIMAL_RSS)

    respx.get(config.url).mock(side_effect=capture_request)

    def get_etag(feed_id):
        return ('"cached-etag"', "Mon, 01 Jan 2025 00:00:00 GMT")

    await feed_scout.run([config], run_id=TEST_RUN_ID, get_feed_etag=get_etag)

    assert captured_headers.get("if-none-match") == '"cached-etag"'
    assert captured_headers.get("if-modified-since") == "Mon, 01 Jan 2025 00:00:00 GMT"


@respx.mock
async def test_304_response_produces_unchanged_status_no_entries():
    """A 304 Not Modified response results in UNCHANGED status and no entries."""
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(304))

    entries, reports = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert entries == []
    assert reports[0].status == FeedStatus.UNCHANGED
    assert reports[0].skipped is True
    assert reports[0].succeeded is False
    assert reports[0].entries_fetched == 0
    assert reports[0].etag_supported is True


@respx.mock
async def test_feed_with_etag_support_flagged_in_report():
    """Feeds that return ETag headers have etag_supported=True in their report."""
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(
        200,
        text=MINIMAL_RSS,
        headers={"ETag": '"version-1"'},
    ))

    _, reports = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert reports[0].etag_supported is True
    assert reports[0].etag == '"version-1"'


@respx.mock
async def test_feed_without_etag_flagged_as_not_supported():
    """Feeds that do not return ETag or Last-Modified have etag_supported=False."""
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=MINIMAL_RSS))

    _, reports = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert reports[0].etag_supported is False


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

@respx.mock
async def test_unreachable_feed_produces_failure_report():
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(503))

    entries, reports = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert entries == []
    assert reports[0].status == FeedStatus.UNREACHABLE
    assert reports[0].succeeded is False
    assert reports[0].entries_fetched == 0


@respx.mock
async def test_malformed_feed_produces_failure_report():
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=MALFORMED_RSS))

    entries, reports = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert entries == []
    assert reports[0].status == FeedStatus.MALFORMED


@respx.mock
async def test_empty_feed_produces_empty_report():
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=EMPTY_RSS))

    entries, reports = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert entries == []
    assert reports[0].status == FeedStatus.EMPTY


# ---------------------------------------------------------------------------
# Multi-feed concurrent behavior
# ---------------------------------------------------------------------------

@respx.mock
async def test_one_failed_feed_does_not_stop_others():
    """Failure on one feed does not prevent other feeds from being processed."""
    good = _make_config("https://good.example.com/feed", "good_feed")
    bad = _make_config("https://bad.example.com/feed", "bad_feed")

    respx.get(good.url).mock(return_value=Response(200, text=MINIMAL_RSS))
    respx.get(bad.url).mock(return_value=Response(503))

    entries, reports = await feed_scout.run([good, bad], run_id=TEST_RUN_ID)

    assert len(reports) == 2
    assert reports[0].succeeded is True
    assert reports[1].succeeded is False
    assert len(entries) == 2
    assert all(e.feed_id == "good_feed" for e in entries)


@respx.mock
async def test_unchanged_feed_does_not_affect_other_feeds():
    """A 304 UNCHANGED feed does not prevent other feeds from returning entries."""
    ok_feed = _make_config("https://ok.example.com/feed", "ok_feed")
    unchanged_feed = _make_config("https://unchanged.example.com/feed", "unchanged_feed")

    respx.get(ok_feed.url).mock(return_value=Response(200, text=MINIMAL_RSS))
    respx.get(unchanged_feed.url).mock(return_value=Response(304))

    entries, reports = await feed_scout.run(
        [ok_feed, unchanged_feed], run_id=TEST_RUN_ID
    )

    statuses = {r.feed_id: r.status for r in reports}
    assert statuses["ok_feed"] == FeedStatus.OK
    assert statuses["unchanged_feed"] == FeedStatus.UNCHANGED
    assert len(entries) == 2  # Only from ok_feed


@respx.mock
async def test_all_feeds_fetched_concurrently():
    config_a = _make_config("https://feed-a.example.com/feed", "feed_a")
    config_b = _make_config("https://feed-b.example.com/feed", "feed_b")

    respx.get(config_a.url).mock(return_value=Response(200, text=MINIMAL_RSS))
    respx.get(config_b.url).mock(return_value=Response(200, text=MINIMAL_RSS))

    entries, reports = await feed_scout.run(
        [config_a, config_b], run_id=TEST_RUN_ID
    )

    assert len(reports) == 2
    assert all(r.succeeded for r in reports)
    assert len(entries) == 4  # 2 entries per feed


# ---------------------------------------------------------------------------
# Feed validation (for user-submitted URLs in Gradio)
# ---------------------------------------------------------------------------

@respx.mock
async def test_validate_feed_returns_ok_for_valid_feed():
    url = "https://valid.example.com/feed"
    respx.get(url).mock(return_value=Response(200, text=MINIMAL_RSS))

    report = await feed_scout.validate_feed(url)

    assert report.status == FeedStatus.OK
    assert report.entries_fetched == 2


@respx.mock
async def test_validate_feed_returns_failure_for_dead_url():
    url = "https://dead.example.com/feed"
    respx.get(url).mock(return_value=Response(404))

    report = await feed_scout.validate_feed(url)

    assert report.status == FeedStatus.UNREACHABLE
    assert report.succeeded is False
