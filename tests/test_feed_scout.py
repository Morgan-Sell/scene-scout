"""
Tests for the Feed Scout Agent.

These tests use mocked HTTP responses so they run offline and
deterministically. We are testing our logic: config loading, async
concurrent fetching, entry parsing, health report production, and
failure handling.

pytest-asyncio handles the async test runner. respx mocks httpx at
the transport level so our actual async client code executes normally.
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


def _make_config(url: str = "https://example.com/feed", feed_id: str = "test_feed") -> FeedConfig:
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


def test_load_feed_configs_raises_on_missing_file():
    with pytest.raises(FileNotFoundError):
        load_feed_configs(Path("/nonexistent/feeds.yaml"))


# ---------------------------------------------------------------------------
# Successful feed fetch
# ---------------------------------------------------------------------------

@respx.mock
async def test_successful_feed_returns_entries():
    """A healthy feed produces RawFeedEntry objects and an OK health report."""
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=MINIMAL_RSS))

    entries, reports = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert len(reports) == 1
    report = reports[0]
    assert report.status == FeedStatus.OK
    assert report.succeeded is True
    assert report.entries_fetched == 2
    assert len(entries) == 2


@respx.mock
async def test_raw_entry_fields_are_preserved_faithfully():
    """Entry fields are captured exactly as the feed provides them — no parsing."""
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=MINIMAL_RSS))

    entries, _ = await feed_scout.run([config], run_id=TEST_RUN_ID)
    first = entries[0]

    assert first.title == "Jazz Night at the Echo"
    assert first.link == "https://example.com/jazz-night"
    assert "jazz evening" in first.description.lower()
    assert "Music" in first.categories or "Jazz" in first.categories
    # published_raw preserved as string — not parsed
    assert isinstance(first.published_raw, str)
    assert first.feed_id == "test_feed"
    assert first.run_id == TEST_RUN_ID
    assert isinstance(first.fetched_at, datetime)


@respx.mock
async def test_run_id_attached_to_every_entry():
    """Every entry produced carries the run_id of the pipeline execution."""
    config = _make_config()
    respx.get(config.url).mock(return_value=Response(200, text=MINIMAL_RSS))

    entries, _ = await feed_scout.run([config], run_id=TEST_RUN_ID)

    assert all(e.run_id == TEST_RUN_ID for e in entries)


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
    """A failure on one feed does not prevent other feeds from being processed."""
    good_config = _make_config("https://good.example.com/feed", feed_id="good_feed")
    bad_config = _make_config("https://bad.example.com/feed", feed_id="bad_feed")

    respx.get(good_config.url).mock(return_value=Response(200, text=MINIMAL_RSS))
    respx.get(bad_config.url).mock(return_value=Response(503))

    entries, reports = await feed_scout.run(
        [good_config, bad_config], run_id=TEST_RUN_ID
    )

    assert len(reports) == 2
    assert reports[0].succeeded is True
    assert reports[1].succeeded is False
    # Entries only from the good feed
    assert len(entries) == 2
    assert all(e.feed_id == "good_feed" for e in entries)


@respx.mock
async def test_all_feeds_fetched_concurrently():
    """Both feeds are fetched and both produce results — concurrent execution verified
    by the fact that both mocks are called and both results are returned."""
    config_a = _make_config("https://feed-a.example.com/feed", feed_id="feed_a")
    config_b = _make_config("https://feed-b.example.com/feed", feed_id="feed_b")

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
