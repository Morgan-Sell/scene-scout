"""Recommendation history policy constants for SceneScout."""

# Events recommended within this window are subject to novelty recency decay.
SOFT_RECENCY_DAYS = 28

# Events recommended within this window are hard-excluded from re-recommendation.
HARD_RECENCY_DAYS = 14

# Legacy composite multiplier — used only by history.apply_soft_recency_penalty tests.
# Ranking uses exponential novelty decay instead.
SOFT_RECENCY_SCORE_MULTIPLIER = 0.75
