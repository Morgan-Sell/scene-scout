"""
Tests for CuratorConfig loading and defaults.
"""

from __future__ import annotations

from scene_scout.curator_config import CuratorConfig, load_curator_config


def test_load_curator_config_defaults_name_to_allegra() -> None:
    config = load_curator_config()

    assert config.name == "Allegra"
    assert isinstance(config, CuratorConfig)


def test_load_curator_config_reads_voice_brief_from_prompt_file() -> None:
    config = load_curator_config()

    assert "Allegra" in config.voice_brief
    assert 'Never use the word "curated"' in config.voice_brief
    assert len(config.voice_brief) > 0


def test_load_curator_config_accepts_custom_name() -> None:
    config = load_curator_config(name="Custom Curator")

    assert config.name == "Custom Curator"
    assert len(config.voice_brief) > 0
