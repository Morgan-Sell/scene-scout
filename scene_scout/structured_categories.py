"""
Keyword-based category inference for structured ingest rows.

Used when adapters return title/venue/datetime but no category labels (e.g. DoNYC
listing cards). Output labels align with :data:`EVENT_CATEGORIES` vocabulary so
normalization and ranking can score against profile weights.
"""

from __future__ import annotations

import re

from scene_scout.normalization_config import CATEGORY_ALIASES, EVENT_CATEGORIES

_WHITESPACE = re.compile(r"\s+")

# Ordered rules: first match wins per category bucket; multiple categories allowed.
_CATEGORY_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Comedy", ("comedy", "stand-up", "standup", "improv", "open mic")),
    ("Jazz", ("jazz", "bebop", "swing night")),
    ("Classical", ("classical", "orchestra", "symphony", "philharmonic")),
    ("Theater", ("theater", "theatre", "broadway", "musical", "playwright")),
    ("Dance", ("dance", "ballet", "club night", "dj set", "techno", "house music")),
    ("Sports", ("sports", "baseball", "basketball", "soccer", "marathon", "game")),
    ("Film", ("film", "screening", "cinema", "movie")),
    ("Art", ("art", "gallery", "museum", "exhibition")),
    ("Food", ("food", "tasting", "brunch", "dinner series")),
    ("Nightlife", ("nightlife", "club", "late night", "afterparty")),
    ("Music", ("concert", "live music", "band", "tour", "festival", "show")),
)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _WHITESPACE.sub(" ", value.strip().lower())


def _canonical_label(label: str) -> str | None:
    normalized = _normalize_text(label)
    if not normalized:
        return None
    if normalized in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[normalized]
    title_case = normalized.title()
    if title_case in EVENT_CATEGORIES:
        return title_case
    for category in EVENT_CATEGORIES:
        if _normalize_text(category) == normalized:
            return category
    return None


def infer_categories_from_text(
    *,
    title: str | None = None,
    description: str | None = None,
    extra_labels: list[str] | None = None,
) -> list[str]:
    """Return deduplicated canonical categories inferred from free text."""
    haystack = " ".join(
        part
        for part in (
            _normalize_text(title),
            _normalize_text(description),
        )
        if part
    )
    categories: list[str] = []

    for label in extra_labels or []:
        canonical = _canonical_label(label)
        if canonical and canonical not in categories:
            categories.append(canonical)

    if haystack:
        for category, keywords in _CATEGORY_KEYWORD_RULES:
            if any(keyword in haystack for keyword in keywords):
                if category not in categories:
                    categories.append(category)

    return categories
