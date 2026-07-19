"""Pre-enrichment filter policy constants for SceneScout."""

# The coming-horizon window uses ``UserProfile.horizon_days``, passed from the
# orchestrator into ``apply_pre_enrichment_filter`` (same value as normalization).

# Event IDs recommended within this window are hard-excluded from enrichment.
PRE_ENRICHMENT_HARD_EXCLUDE_DAYS = 14
