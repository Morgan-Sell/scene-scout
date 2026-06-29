"""Tests for abbreviated UAT orchestrator options."""

from __future__ import annotations

import pytest

from scene_scout.models.feed import FeedConfig
from scene_scout.orchestrator_config import (
    parse_feed_ids,
    resolve_uat_max_extraction,
    select_feed_configs,
)


def _feed_config(feed_id: str) -> FeedConfig:
    return FeedConfig.model_validate(
        {
            "id": feed_id,
            "name": feed_id.title(),
            "url": f"https://example.com/{feed_id}/feed/",
            "city": "New York",
            "source_quality_score": 0.8,
            "active": True,
        }
    )


def test_parse_feed_ids_returns_none_for_empty() -> None:
    assert parse_feed_ids(None) is None
    assert parse_feed_ids("") is None
    assert parse_feed_ids("  ,  ") is None


def test_parse_feed_ids_splits_and_trims() -> None:
    assert parse_feed_ids("brooklynvegan,theskint") == frozenset(
        {"brooklynvegan", "theskint"}
    )
    assert parse_feed_ids(" a , b ") == frozenset({"a", "b"})


def test_resolve_uat_max_extraction_prefers_cli() -> None:
    assert resolve_uat_max_extraction(25) == 25


def test_resolve_uat_max_extraction_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAT_MAX_EXTRACTION", "10")
    assert resolve_uat_max_extraction(None) == 10


def test_resolve_uat_max_extraction_cli_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAT_MAX_EXTRACTION", "10")
    assert resolve_uat_max_extraction(25) == 25


def test_resolve_uat_max_extraction_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="--max-extraction"):
        resolve_uat_max_extraction(0)


def test_select_feed_configs_returns_all_when_unfiltered() -> None:
    feeds = [_feed_config("a"), _feed_config("b")]
    assert select_feed_configs(feeds, None) == feeds


def test_select_feed_configs_filters_to_requested_ids() -> None:
    feeds = [_feed_config("a"), _feed_config("b"), _feed_config("c")]
    selected = select_feed_configs(feeds, frozenset({"b", "a"}))
    assert [config.id for config in selected] == ["a", "b"]


def test_select_feed_configs_raises_for_unknown_ids() -> None:
    feeds = [_feed_config("a")]
    with pytest.raises(ValueError, match="Unknown or inactive feed id"):
        select_feed_configs(feeds, frozenset({"missing"}))
