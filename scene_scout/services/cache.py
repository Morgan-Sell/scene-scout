"""
SQLite cache service for SceneScout.

All caching lives in a single database under ``vol-cache/``. Tables are created on
first use. TTL is enforced on read; expired enrichment entries return ``None`` (or
partial ``VenueCacheEntry`` fields for dual-TTL venue rows).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scene_scout.cache_config import (
    CACHE_DB_FILENAME,
    CACHE_TYPES,
    PERFORMER_TTL_DAYS,
    SEEN_ENTRIES_TTL_DAYS,
    VENUE_CONTEXT_TTL_DAYS,
    VENUE_GEO_TTL_DAYS,
    VIBE_TTL_DAYS,
)
from scene_scout.config import vol_cache_dir
from scene_scout.logging import get_logger
from scene_scout.models.event import NormalizedEvent, PerformerInfo, VenueCacheEntry
from scene_scout.services.cache_schema import SCHEMA_STATEMENTS


def _cache_db_path() -> Path:
    return vol_cache_dir() / CACHE_DB_FILENAME


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_expired(expires_at: str, *, now: datetime | None = None) -> bool:
    current = now or _utc_now()
    return _parse_dt(expires_at) <= current


class CacheService:
    """SQLite-backed cache for feeds, seen entries, and enrichment results."""

    def __init__(self, run_id: str, db_path: Path | None = None) -> None:
        self._run_id = run_id
        self._db_path = db_path or _cache_db_path()
        self._logger = get_logger("cache", run_id=run_id)
        self._hits = dict.fromkeys(CACHE_TYPES, 0)
        self._misses = dict.fromkeys(CACHE_TYPES, 0)
        self._schema_initialized = False

    def _connect(self) -> sqlite3.Connection:
        self._ensure_schema()
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        if self._schema_initialized:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.commit()
        self._schema_initialized = True

    def _record_hit(self, cache_type: str) -> None:
        self._hits[cache_type] += 1

    def _record_miss(self, cache_type: str) -> None:
        self._misses[cache_type] += 1

    def log_run_stats(self) -> None:
        """Log aggregate hit/miss counts for this run."""
        self._logger.info(
            "Cache run stats",
            data={
                "run_id": self._run_id,
                "hits": dict(self._hits),
                "misses": dict(self._misses),
            },
        )

    def get_feed_etag(self, feed_id: str) -> tuple[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT etag, last_modified FROM feed_etags WHERE feed_id = ?",
                (feed_id,),
            ).fetchone()

        if row is None:
            self._record_miss("feed_etags")
            return None

        self._record_hit("feed_etags")
        return str(row["etag"] or ""), str(row["last_modified"] or "")

    def set_feed_etag(
        self,
        feed_id: str,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        now = _format_dt(_utc_now())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feed_etags (feed_id, etag, last_modified, stored_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(feed_id) DO UPDATE SET
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    stored_at = excluded.stored_at
                """,
                (feed_id, etag, last_modified, now),
            )
            conn.commit()

    def get_seen_entry(
        self,
        feed_id: str,
        entry_hash: str,
    ) -> NormalizedEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT normalized_event_json, expires_at
                FROM seen_entries
                WHERE feed_id = ? AND entry_hash = ?
                """,
                (feed_id, entry_hash),
            ).fetchone()

        if row is None or _is_expired(str(row["expires_at"])):
            self._record_miss("seen_entries")
            return None

        self._record_hit("seen_entries")
        return NormalizedEvent.model_validate_json(str(row["normalized_event_json"]))

    def set_seen_entry(
        self,
        feed_id: str,
        entry_hash: str,
        normalized_event: NormalizedEvent,
    ) -> None:
        now = _utc_now()
        expires_at = now + timedelta(days=SEEN_ENTRIES_TTL_DAYS)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO seen_entries (
                    feed_id,
                    entry_hash,
                    normalized_event_json,
                    first_seen_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(feed_id, entry_hash) DO UPDATE SET
                    normalized_event_json = excluded.normalized_event_json,
                    first_seen_at = excluded.first_seen_at,
                    expires_at = excluded.expires_at
                """,
                (
                    feed_id,
                    entry_hash,
                    normalized_event.model_dump_json(),
                    _format_dt(now),
                    _format_dt(expires_at),
                ),
            )
            conn.commit()

    def get_performer(self, name_key: str) -> PerformerInfo | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT performer_info_json, expires_at
                FROM performer_cache
                WHERE performer_name_key = ?
                """,
                (name_key,),
            ).fetchone()

        if row is None or _is_expired(str(row["expires_at"])):
            self._record_miss("performer_cache")
            return None

        self._record_hit("performer_cache")
        return PerformerInfo.model_validate_json(str(row["performer_info_json"]))

    def set_performer(self, name_key: str, info: PerformerInfo) -> None:
        now = _utc_now()
        expires_at = now + timedelta(days=PERFORMER_TTL_DAYS)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO performer_cache (
                    performer_name_key,
                    performer_info_json,
                    cached_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(performer_name_key) DO UPDATE SET
                    performer_info_json = excluded.performer_info_json,
                    cached_at = excluded.cached_at,
                    expires_at = excluded.expires_at
                """,
                (
                    name_key,
                    info.model_dump_json(),
                    _format_dt(now),
                    _format_dt(expires_at),
                ),
            )
            conn.commit()

    def get_venue(self, venue_key: str) -> VenueCacheEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    coordinates_json,
                    poi_list_json,
                    neighborhood_context,
                    neighborhood_confidence,
                    geo_expires_at,
                    context_expires_at
                FROM venue_cache
                WHERE venue_key = ?
                """,
                (venue_key,),
            ).fetchone()

        if row is None:
            self._record_miss("venue_cache")
            return None

        geo_valid = not _is_expired(str(row["geo_expires_at"]))
        context_valid = not _is_expired(str(row["context_expires_at"]))

        if not geo_valid and not context_valid:
            self._record_miss("venue_cache")
            return None

        self._record_hit("venue_cache")
        coordinates: tuple[float, float] | None = None
        poi_list: list[dict[str, Any]] | None = None
        neighborhood_context: str | None = None
        neighborhood_confidence: float | None = None

        if geo_valid:
            if row["coordinates_json"]:
                coords = json.loads(str(row["coordinates_json"]))
                coordinates = (float(coords["lat"]), float(coords["lon"]))
            if row["poi_list_json"]:
                poi_list = json.loads(str(row["poi_list_json"]))

        if context_valid:
            neighborhood_context = row["neighborhood_context"]
            if row["neighborhood_confidence"] is not None:
                neighborhood_confidence = float(row["neighborhood_confidence"])

        return VenueCacheEntry(
            coordinates=coordinates,
            poi_list=poi_list,
            neighborhood_context=neighborhood_context,
            neighborhood_confidence=neighborhood_confidence,
        )

    def set_venue(
        self,
        venue_key: str,
        *,
        coordinates: tuple[float, float] | None = None,
        poi_list: list[dict[str, Any]] | None = None,
        neighborhood_context: str | None = None,
        neighborhood_confidence: float | None = None,
    ) -> None:
        now = _utc_now()
        now_text = _format_dt(now)

        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT
                    coordinates_json,
                    poi_list_json,
                    neighborhood_context,
                    neighborhood_confidence,
                    geo_expires_at,
                    context_expires_at
                FROM venue_cache
                WHERE venue_key = ?
                """,
                (venue_key,),
            ).fetchone()

            if existing is None:
                geo_expires_at = (
                    _format_dt(now + timedelta(days=VENUE_GEO_TTL_DAYS))
                    if coordinates is not None or poi_list is not None
                    else now_text
                )
                context_expires_at = (
                    _format_dt(now + timedelta(days=VENUE_CONTEXT_TTL_DAYS))
                    if neighborhood_context is not None
                    or neighborhood_confidence is not None
                    else now_text
                )
                coordinates_json = (
                    json.dumps({"lat": coordinates[0], "lon": coordinates[1]})
                    if coordinates is not None
                    else None
                )
                poi_list_json = json.dumps(poi_list) if poi_list is not None else None
                stored_context = neighborhood_context
                stored_confidence = neighborhood_confidence
            else:
                geo_expires_at = str(existing["geo_expires_at"])
                context_expires_at = str(existing["context_expires_at"])
                coordinates_json = existing["coordinates_json"]
                poi_list_json = existing["poi_list_json"]
                stored_context = existing["neighborhood_context"]
                stored_confidence = existing["neighborhood_confidence"]

                if coordinates is not None:
                    coordinates_json = json.dumps(
                        {"lat": coordinates[0], "lon": coordinates[1]}
                    )
                    geo_expires_at = _format_dt(
                        now + timedelta(days=VENUE_GEO_TTL_DAYS)
                    )
                if poi_list is not None:
                    poi_list_json = json.dumps(poi_list)
                    geo_expires_at = _format_dt(
                        now + timedelta(days=VENUE_GEO_TTL_DAYS)
                    )

                if neighborhood_context is not None or neighborhood_confidence is not None:
                    context_expires_at = _format_dt(
                        now + timedelta(days=VENUE_CONTEXT_TTL_DAYS)
                    )
                    if neighborhood_context is not None:
                        stored_context = neighborhood_context
                    if neighborhood_confidence is not None:
                        stored_confidence = neighborhood_confidence

            conn.execute(
                """
                INSERT INTO venue_cache (
                    venue_key,
                    coordinates_json,
                    poi_list_json,
                    neighborhood_context,
                    neighborhood_confidence,
                    cached_at,
                    geo_expires_at,
                    context_expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(venue_key) DO UPDATE SET
                    coordinates_json = excluded.coordinates_json,
                    poi_list_json = excluded.poi_list_json,
                    neighborhood_context = excluded.neighborhood_context,
                    neighborhood_confidence = excluded.neighborhood_confidence,
                    cached_at = excluded.cached_at,
                    geo_expires_at = excluded.geo_expires_at,
                    context_expires_at = excluded.context_expires_at
                """,
                (
                    venue_key,
                    coordinates_json,
                    poi_list_json,
                    stored_context,
                    stored_confidence,
                    now_text,
                    geo_expires_at,
                    context_expires_at,
                ),
            )
            conn.commit()

    def get_vibe(self, content_hash: str) -> list[str] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT vibe_tags_json, expires_at
                FROM vibe_cache
                WHERE content_hash = ?
                """,
                (content_hash,),
            ).fetchone()

        if row is None or _is_expired(str(row["expires_at"])):
            self._record_miss("vibe_cache")
            return None

        self._record_hit("vibe_cache")
        tags = json.loads(str(row["vibe_tags_json"]))
        return [str(tag) for tag in tags]

    def set_vibe(self, content_hash: str, tags: list[str]) -> None:
        now = _utc_now()
        expires_at = now + timedelta(days=VIBE_TTL_DAYS)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vibe_cache (
                    content_hash,
                    vibe_tags_json,
                    cached_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    vibe_tags_json = excluded.vibe_tags_json,
                    cached_at = excluded.cached_at,
                    expires_at = excluded.expires_at
                """,
                (
                    content_hash,
                    json.dumps(tags),
                    _format_dt(now),
                    _format_dt(expires_at),
                ),
            )
            conn.commit()
