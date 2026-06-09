"""SQLite DDL for the cache service."""

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS feed_etags (
        feed_id          TEXT PRIMARY KEY,
        etag             TEXT,
        last_modified    TEXT,
        stored_at        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seen_entries (
        feed_id               TEXT NOT NULL,
        entry_hash            TEXT NOT NULL,
        normalized_event_json TEXT NOT NULL,
        first_seen_at         TEXT NOT NULL,
        expires_at            TEXT NOT NULL,
        PRIMARY KEY (feed_id, entry_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performer_cache (
        performer_name_key    TEXT PRIMARY KEY,
        performer_info_json   TEXT NOT NULL,
        cached_at             TEXT NOT NULL,
        expires_at            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS venue_cache (
        venue_key                TEXT PRIMARY KEY,
        coordinates_json         TEXT,
        poi_list_json            TEXT,
        neighborhood_context     TEXT,
        neighborhood_confidence  REAL,
        cached_at                TEXT NOT NULL,
        geo_expires_at           TEXT NOT NULL,
        context_expires_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vibe_cache (
        content_hash    TEXT PRIMARY KEY,
        vibe_tags_json  TEXT NOT NULL,
        cached_at       TEXT NOT NULL,
        expires_at      TEXT NOT NULL
    )
    """,
)
