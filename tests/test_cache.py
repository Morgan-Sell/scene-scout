"""
Tests for the SQLite cache service.

Covers schema initialization, all cache interfaces, TTL expiry, feed provenance
keys, venue dual-TTL partial reads, and per-run hit/miss logging.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from scene_scout.cache_config import (
    PERFORMER_TTL_DAYS,
    SEEN_ENTRIES_TTL_DAYS,
    VENUE_CONTEXT_TTL_DAYS,
    VENUE_GEO_TTL_DAYS,
    VIBE_TTL_DAYS,
)
from scene_scout.models.enrichment import PerformerInfo
from scene_scout.models.event import NormalizedEvent, VenueCacheEntry
from scene_scout.services.cache import CacheService
from tests.conftest import TEST_RUN_ID

SANDLOT_FEED = "sandlot-pickup-league"
RIVAL_FEED = "rival-neighborhood-league"
ENTRY_HASH = "hash-of-homerun-link"
VENUE_KEY = "sandlot field|los angeles"
PERFORMER_KEY = "benny the jet"
VIBE_HASH = "sha256-sandlot-sunset-game"


def _sample_event(*, feed_id: str = SANDLOT_FEED) -> NormalizedEvent:
    return NormalizedEvent(
        id="sandlot-game-1993",
        title="Pick-up baseball at the sandlot",
        start_datetime=datetime(1993, 7, 4, 18, 0, tzinfo=timezone.utc),
        venue="The Sandlot",
        city="Los Angeles",
        url="https://example.com/sandlot-game",
        is_free=True,
        description="You're killing me, Smalls!",
        source_feeds=[feed_id],
        best_source_feed=feed_id,
        run_id=TEST_RUN_ID,
        normalized_at=datetime(1993, 7, 4, 12, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def cache_db(tmp_path: Path) -> Path:
    return tmp_path / "wendy-peffercorn-locker" / "cache.db"


@pytest.fixture
def cache(cache_db: Path) -> CacheService:
    return CacheService(run_id=TEST_RUN_ID, db_path=cache_db)


def test_initializes_all_five_tables_on_first_use(cache: CacheService) -> None:
    cache.set_feed_etag(SANDLOT_FEED, '"etag-1"', "Mon, 01 Jan 2024 00:00:00 GMT")

    with sqlite3.connect(cache._db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert tables >= {
        "feed_etags",
        "seen_entries",
        "performer_cache",
        "venue_cache",
        "vibe_cache",
    }


def test_feed_etag_get_and_set(cache: CacheService) -> None:
    assert cache.get_feed_etag(SANDLOT_FEED) is None

    cache.set_feed_etag(SANDLOT_FEED, '"sandlot-etag"', "Tue, 02 Jan 2024 00:00:00 GMT")
    result = cache.get_feed_etag(SANDLOT_FEED)

    assert result == ('"sandlot-etag"', "Tue, 02 Jan 2024 00:00:00 GMT")


def test_seen_entry_round_trip(cache: CacheService) -> None:
    event = _sample_event()
    cache.set_seen_entry(SANDLOT_FEED, ENTRY_HASH, event)

    cached = cache.get_seen_entry(SANDLOT_FEED, ENTRY_HASH)

    assert cached == event


def test_seen_entry_isolated_by_feed_id(cache: CacheService) -> None:
    sandlot_event = _sample_event(feed_id=SANDLOT_FEED)
    rival_event = _sample_event(feed_id=RIVAL_FEED)

    cache.set_seen_entry(SANDLOT_FEED, ENTRY_HASH, sandlot_event)
    cache.set_seen_entry(RIVAL_FEED, ENTRY_HASH, rival_event)

    assert cache.get_seen_entry(SANDLOT_FEED, ENTRY_HASH) == sandlot_event
    assert cache.get_seen_entry(RIVAL_FEED, ENTRY_HASH) == rival_event


def test_seen_entry_expires_after_ttl(cache: CacheService) -> None:
    event = _sample_event()
    base = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

    with patch("scene_scout.services.cache._utc_now", return_value=base):
        cache.set_seen_entry(SANDLOT_FEED, ENTRY_HASH, event)

    with patch(
        "scene_scout.services.cache._utc_now",
        return_value=base + timedelta(days=SEEN_ENTRIES_TTL_DAYS, seconds=1),
    ):
        assert cache.get_seen_entry(SANDLOT_FEED, ENTRY_HASH) is None


def test_performer_round_trip_and_expiry(cache: CacheService) -> None:
    info = PerformerInfo(
        name="Benny Rodriguez",
        entity_type="athlete",
        genre_tags=["base-stealing"],
        one_line_summary="The Jet",
        confidence=0.95,
        affinity_score=0.88,
    )
    base = datetime(2024, 6, 1, tzinfo=timezone.utc)

    with patch("scene_scout.services.cache._utc_now", return_value=base):
        cache.set_performer(PERFORMER_KEY, info)

    with patch("scene_scout.services.cache._utc_now", return_value=base):
        assert cache.get_performer(PERFORMER_KEY) == info

    with patch(
        "scene_scout.services.cache._utc_now",
        return_value=base + timedelta(days=PERFORMER_TTL_DAYS, seconds=1),
    ):
        assert cache.get_performer(PERFORMER_KEY) is None


def test_venue_geo_and_context_dual_ttl(cache: CacheService) -> None:
    base = datetime(2024, 3, 1, tzinfo=timezone.utc)
    poi = [{"name": "Treehouse", "type": "landmark"}]

    with patch("scene_scout.services.cache._utc_now", return_value=base):
        cache.set_venue(
            VENUE_KEY,
            coordinates=(34.05, -118.25),
            poi_list=poi,
            neighborhood_context="Classic suburban sandlot vibes.",
            neighborhood_confidence=0.82,
        )

    context_expired = base + timedelta(days=VENUE_CONTEXT_TTL_DAYS, seconds=1)
    with patch("scene_scout.services.cache._utc_now", return_value=context_expired):
        partial = cache.get_venue(VENUE_KEY)

    assert partial == VenueCacheEntry(
        coordinates=(34.05, -118.25),
        poi_list=poi,
        neighborhood_context=None,
        neighborhood_confidence=None,
    )

    geo_expired = base + timedelta(days=VENUE_GEO_TTL_DAYS, seconds=1)
    with patch("scene_scout.services.cache._utc_now", return_value=geo_expired):
        assert cache.get_venue(VENUE_KEY) is None


def test_vibe_round_trip_and_expiry(cache: CacheService) -> None:
    tags = ["outdoor", "social", "high-energy"]
    base = datetime(2024, 5, 1, tzinfo=timezone.utc)

    with patch("scene_scout.services.cache._utc_now", return_value=base):
        cache.set_vibe(VIBE_HASH, tags)

    with patch("scene_scout.services.cache._utc_now", return_value=base):
        assert cache.get_vibe(VIBE_HASH) == tags

    with patch(
        "scene_scout.services.cache._utc_now",
        return_value=base + timedelta(days=VIBE_TTL_DAYS, seconds=1),
    ):
        assert cache.get_vibe(VIBE_HASH) is None


def test_log_run_stats_records_hits_and_misses(
    cache: CacheService,
    logs_dir: Path,
) -> None:
    cache.get_feed_etag("missing-feed")
    cache.set_feed_etag(SANDLOT_FEED, '"etag"', "Wed, 03 Jan 2024 00:00:00 GMT")
    cache.get_feed_etag(SANDLOT_FEED)
    cache.get_seen_entry(SANDLOT_FEED, "missing-hash")

    cache.log_run_stats()

    log_file = logs_dir / f"{TEST_RUN_ID}.jsonl"
    entries = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").strip().splitlines()
    ]
    stats_entry = next(
        entry for entry in entries if entry["message"] == "Cache run stats"
    )

    assert stats_entry["data"]["hits"]["feed_etags"] == 1
    assert stats_entry["data"]["misses"]["feed_etags"] == 1
    assert stats_entry["data"]["misses"]["seen_entries"] == 1
