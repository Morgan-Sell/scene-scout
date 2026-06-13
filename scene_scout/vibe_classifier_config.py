"""Vibe Classifier agent policy constants."""

MIN_VIBE_TAGS = 2
MAX_VIBE_TAGS = 5

VIBE_VOCABULARY: frozenset[str] = frozenset(
    {
        "intimate",
        "high-energy",
        "experimental",
        "social",
        "introspective",
        "outdoor",
        "late-night",
        "family-friendly",
        "industry",
        "touristy",
        "immersive",
        "underground",
        "high-production",
        "free-spirited",
        "niche",
        "single-friendly",
        "pretentious",
        "exclusive",
        "inclusive",
    }
)
