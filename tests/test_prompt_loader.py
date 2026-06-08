"""
Tests for the prompt loader service.

Covers successful rendering, FileNotFoundError on missing templates, and
jinja2.UndefinedError on missing template variables.
"""

from pathlib import Path
from unittest.mock import patch

import jinja2
import pytest

from scene_scout.services.prompt_loader import render_prompt

SANDLOT_PROMPT = """\
You're killing me, Smalls! Summarize this sandlot event.

Event: {{ event_title }}
Venue: {{ venue }}
Legend: {{ legend }}
"""


@pytest.fixture
def sandlot_prompts_dir(tmp_path: Path) -> Path:
    """Temp prompts directory with a Sandlot-themed Jinja2 template."""
    (tmp_path / "sandlot_event.txt").write_text(SANDLOT_PROMPT, encoding="utf-8")
    return tmp_path


def test_render_prompt_injects_kwargs(sandlot_prompts_dir: Path) -> None:
    with patch(
        "scene_scout.services.prompt_loader._PROMPTS_DIR",
        sandlot_prompts_dir,
    ):
        result = render_prompt(
            "sandlot_event",
            event_title="The Great Bambino Night",
            venue="The Sandlot",
            legend="Babe Ruth signed the ball — heroes get remembered.",
        )

    assert "The Great Bambino Night" in result
    assert "The Sandlot" in result
    assert "Babe Ruth signed the ball" in result
    assert "You're killing me, Smalls!" in result


def test_render_prompt_raises_file_not_found_for_missing_template(
    sandlot_prompts_dir: Path,
) -> None:
    with (
        patch(
            "scene_scout.services.prompt_loader._PROMPTS_DIR",
            sandlot_prompts_dir,
        ),
        pytest.raises(FileNotFoundError, match="the-beast-escaped"),
    ):
        render_prompt("the-beast-escaped")


def test_render_prompt_raises_undefined_error_for_missing_variable(
    sandlot_prompts_dir: Path,
) -> None:
    with (
        patch(
            "scene_scout.services.prompt_loader._PROMPTS_DIR",
            sandlot_prompts_dir,
        ),
        pytest.raises(jinja2.UndefinedError),
    ):
        render_prompt(
            "sandlot_event",
            event_title="Pool Party at the Rec Center",
            venue="The Sandlot",
            # legend intentionally omitted
        )


def test_render_prompt_static_template_without_variables() -> None:
    """Prompts with no Jinja2 variables render as-is."""
    result = render_prompt("event_extraction")
    assert "event extraction specialist" in result.lower()
    assert "is_event" in result
