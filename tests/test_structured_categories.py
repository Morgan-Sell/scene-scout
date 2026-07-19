"""Tests for structured category inference."""

from scene_scout.structured_categories import infer_categories_from_text


def test_infer_categories_from_comedy_title() -> None:
    categories = infer_categories_from_text(title="Stand-up Comedy Night")
    assert "Comedy" in categories


def test_infer_categories_from_music_description() -> None:
    categories = infer_categories_from_text(
        title="Thank U, Next",
        description="Late-night live music at The Bowery Ballroom",
    )
    assert "Music" in categories


def test_infer_categories_merges_api_labels() -> None:
    categories = infer_categories_from_text(
        title="Showcase",
        extra_labels=["Music", "Rock"],
    )
    assert "Music" in categories
    assert "Rock" in categories


def test_infer_categories_deduplicates() -> None:
    categories = infer_categories_from_text(
        title="Jazz Night",
        extra_labels=["Jazz", "music"],
    )
    assert categories.count("Jazz") == 1
