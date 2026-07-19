"""Normalization policy constants for SceneScout."""

from scene_scout.models.user import DEFAULT_HORIZON_DAYS

# Fallback when ``horizon_days`` is not supplied (e.g. unit tests, feed-probe).
# Pipeline runs pass ``UserProfile.horizon_days`` from the orchestrator instead.
DEFAULT_PIPELINE_HORIZON_DAYS = DEFAULT_HORIZON_DAYS
MAX_RECURRING_OCCURRENCES = 5

# Aggregated discard logging (UAT-D.6).
NORMALIZATION_DISCARD_TERMINAL_SAMPLE_SIZE = 5
NORMALIZATION_DISCARD_JSONL_SAMPLE_SIZE = 50

# Controlled vocabulary for event categories after normalization.
EVENT_CATEGORIES: frozenset[str] = frozenset(
    {
        "Art",
        "Classical",
        "Comedy",
        "Community",
        "Country",
        "Dance",
        "Education",
        "Electronic",
        "Family",
        "Fashion",
        "Film",
        "Folk",
        "Food",
        "Hip-Hop",
        "Jazz",
        "Literature",
        "Music",
        "Nightlife",
        "Outdoor",
        "Rock",
        "Sports",
        "Technology",
        "Theater",
        "Wellness",
    }
)

# Maps common RSS / LLM labels to canonical category names.
CATEGORY_ALIASES: dict[str, str] = {
    "art": "Art",
    "arts": "Art",
    "baseball": "Sports",
    "comedy": "Comedy",
    "community": "Community",
    "concert": "Music",
    "concerts": "Music",
    "country": "Country",
    "dance": "Dance",
    "education": "Education",
    "electronic": "Electronic",
    "family": "Family",
    "fashion": "Fashion",
    "film": "Film",
    "films": "Film",
    "food": "Food",
    "hip hop": "Hip-Hop",
    "hip-hop": "Hip-Hop",
    "indie": "Film",
    "independent": "Film",
    "jazz": "Jazz",
    "legends": "Community",
    "literature": "Literature",
    "music": "Music",
    "nightlife": "Nightlife",
    "outdoor": "Outdoor",
    "pool": "Outdoor",
    "rock": "Rock",
    "sport": "Sports",
    "sports": "Sports",
    "tech": "Technology",
    "technology": "Technology",
    "theater": "Theater",
    "theatre": "Theater",
    "wellness": "Wellness",
}
