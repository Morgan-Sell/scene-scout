"""Description quality rubric constants for SceneScout."""

WEIGHT_DESCRIPTION_LENGTH = 0.20
WEIGHT_VENUE_PRESENCE = 0.20
WEIGHT_DATE_TIME_PRESENT = 0.20
WEIGHT_PERFORMER_NAMED = 0.15
WEIGHT_CATEGORY_COVERAGE = 0.10
WEIGHT_URL_VALIDITY = 0.10
WEIGHT_PRICE_CLARITY = 0.05

GENERIC_VENUE_NAMES: frozenset[str] = frozenset(
    {
        "tba",
        "tbd",
        "venue",
        "location",
        "online",
        "various",
        "n/a",
        "los angeles",
        "la",
    }
)

GENERIC_CATEGORY_NAMES: frozenset[str] = frozenset(
    {
        "event",
        "events",
        "general",
        "other",
        "misc",
        "uncategorized",
    }
)

GENERIC_PERFORMER_PHRASES: frozenset[str] = frozenset(
    {
        "local artists",
        "local artist",
        "special guests",
        "special guest",
        "the dj",
        "various artists",
        "tba",
    }
)
