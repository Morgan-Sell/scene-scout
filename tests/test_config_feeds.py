"""
Tests for city-scoped feed configuration loading (Phase 1C.2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scene_scout.config import filter_feed_configs_for_home_city, load_feed_configs
from scene_scout.models.feed import FeedConfig

_MIXED_FEEDS_YAML = """
feeds:
  - id: nyc_local
    name: NYC Local
    url: https://example.com/nyc
    city: New York
    source_quality_score: 0.8
    active: true

  - id: la_local
    name: LA Local
    url: https://example.com/la
    city: Los Angeles
    source_quality_score: 0.8
    active: true

  - id: ticketmaster
    name: Ticketmaster
    url: ticketmaster://events/search
    city: New York
    is_national: true
    source_type: api
    source_quality_score: 0.7
    active: true

  - id: inactive_nyc
    name: Inactive NYC
    url: https://example.com/inactive
    city: New York
    source_quality_score: 0.5
    active: false
"""


@pytest.fixture
def mixed_feeds_path(tmp_path: Path) -> Path:
    path = tmp_path / "feeds.yaml"
    path.write_text(_MIXED_FEEDS_YAML, encoding="utf-8")
    return path


def test_load_feed_configs_without_home_city_returns_all_active(
    mixed_feeds_path: Path,
) -> None:
    configs = load_feed_configs(mixed_feeds_path)
    assert {config.id for config in configs} == {
        "nyc_local",
        "la_local",
        "ticketmaster",
    }


def test_load_feed_configs_filters_metro_and_national_for_new_york(
    mixed_feeds_path: Path,
) -> None:
    configs = load_feed_configs(mixed_feeds_path, home_city="New York")
    assert {config.id for config in configs} == {"nyc_local", "ticketmaster"}


def test_load_feed_configs_filters_metro_and_national_for_los_angeles(
    mixed_feeds_path: Path,
) -> None:
    configs = load_feed_configs(mixed_feeds_path, home_city="Los Angeles")
    assert {config.id for config in configs} == {"la_local", "ticketmaster"}


def test_national_feed_included_for_any_home_city(mixed_feeds_path: Path) -> None:
    configs = load_feed_configs(mixed_feeds_path, home_city="Chicago")
    assert [config.id for config in configs] == ["ticketmaster"]


def test_filter_feed_configs_for_home_city_on_in_memory_list() -> None:
    configs = [
        FeedConfig(
            id="a",
            name="A",
            url="https://example.com/a",
            city="New York",
            source_quality_score=0.5,
        ),
        FeedConfig(
            id="b",
            name="B",
            url="https://example.com/b",
            city="Los Angeles",
            source_quality_score=0.5,
        ),
        FeedConfig(
            id="national",
            name="National",
            url="https://example.com/n",
            city="New York",
            is_national=True,
            source_quality_score=0.5,
            source_type="api",
        ),
    ]

    filtered = filter_feed_configs_for_home_city(configs, "New York")
    assert {config.id for config in filtered} == {"a", "national"}
