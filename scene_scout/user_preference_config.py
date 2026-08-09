"""User Preference Agent policy constants."""

import math

from scene_scout.ranking_config import NEUTRAL_CATEGORY_FIT

# Behavioral feedback deltas (architecture.md — Phase 2 warm personalization).
FEEDBACK_CLICK_CATEGORY_DELTA = 0.03
FEEDBACK_NEGATIVE_CATEGORY_DELTA = -0.05

# Exponential signal decay: weight = e^(-λt), half-life 30 days.
FEEDBACK_HALF_LIFE_DAYS = 30
FEEDBACK_DECAY_LAMBDA = math.log(2) / FEEDBACK_HALF_LIFE_DAYS

# Default weight for categories not yet present in the profile map.
FEEDBACK_DEFAULT_CATEGORY_WEIGHT = NEUTRAL_CATEGORY_FIT

# Discrete vibe list updates apply only above this decay weight.
VIBE_UPDATE_MIN_DECAY_WEIGHT = 0.5
