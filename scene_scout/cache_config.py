"""Cache policy constants for SceneScout."""

CACHE_DB_FILENAME = "cache.db"

SEEN_ENTRIES_TTL_DAYS = 14
PERFORMER_TTL_DAYS = 90
VENUE_GEO_TTL_DAYS = 90
VENUE_CONTEXT_TTL_DAYS = 30
VIBE_TTL_DAYS = 14

CACHE_TYPES = (
    "feed_etags",
    "seen_entries",
    "performer_cache",
    "venue_cache",
    "vibe_cache",
)
