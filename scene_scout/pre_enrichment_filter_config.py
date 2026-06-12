"""Pre-enrichment filter policy constants for SceneScout."""

# Events must start within this many days (inclusive) to proceed to enrichment.
PRE_ENRICHMENT_COMING_WEEK_DAYS = 7

# Event IDs recommended within this window are hard-excluded from enrichment.
PRE_ENRICHMENT_HARD_EXCLUDE_DAYS = 14
