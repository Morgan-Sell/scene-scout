"""Sell-out risk heuristic constants for SceneScout."""

from __future__ import annotations

# Composite signal weights (must sum to 1.0).
WEIGHT_VENUE_SIZE = 0.25
WEIGHT_PRICE = 0.15
WEIGHT_DATE_PROXIMITY = 0.25
WEIGHT_DESCRIPTION_LANGUAGE = 0.20
WEIGHT_PERFORMER_AFFINITY = 0.15

# Map composite score to risk band.
RISK_THRESHOLD_HIGH = 0.62
RISK_THRESHOLD_MEDIUM = 0.38

# User-facing urgency copy for high-risk events (Email Composer surfaces this).
HIGH_RISK_URGENCY_NOTE = "Tickets may sell out quickly."

# Venue name tokens for capacity heuristics (checked in order: large, small, medium).
LARGE_VENUE_TOKENS: tuple[str, ...] = (
    "arena",
    "stadium",
    "coliseum",
    "amphitheater",
    "amphitheatre",
    "bowl",
    "convention center",
    "fairgrounds",
)

SMALL_VENUE_TOKENS: tuple[str, ...] = (
    "club",
    "bar",
    "lounge",
    "cafe",
    "gallery",
    "basement",
    "attic",
    "studio",
    "pub",
    "speakeasy",
    "backyard",
    "house",
)

MEDIUM_VENUE_TOKENS: tuple[str, ...] = (
    "theater",
    "theatre",
    "hall",
    "ballroom",
    "auditorium",
    "museum",
    "center",
    "centre",
    "park",
)

# Description urgency language.
HIGH_URGENCY_PHRASES: tuple[str, ...] = (
    "limited tickets",
    "selling fast",
    "almost sold out",
    "few tickets",
    "last chance",
    "selling out",
    "limited availability",
    "final release",
    "going fast",
    "nearly sold out",
)

LOW_URGENCY_PHRASES: tuple[str, ...] = (
    "plenty of seating",
    "tickets available",
    "no cover",
    "walk-up welcome",
)

# Price thresholds in cents.
PRICE_LOW_CENTS = 2000
PRICE_MID_CENTS = 6000

# Date proximity thresholds in days.
DAYS_VERY_SOON = 2
DAYS_SOON = 7
DAYS_NEAR = 14
DAYS_FAR = 30
