"""
Jinja2 prompt loading and rendering for SceneScout agents.

All agent prompts live as ``.txt`` templates under ``scene_scout/prompts/``.
Agents call ``render_prompt()`` — no inline prompt strings in agent code.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_jinja_env = jinja2.Environment(
    undefined=jinja2.StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)


def render_prompt(name: str, **kwargs: object) -> str:
    """Load and render a Jinja2 prompt template.

    Parameters
    ----------
    name : str
        Prompt file name without extension (e.g. ``"talent_scout"``).
    **kwargs
        Variables injected into the template.

    Returns
    -------
    str
        Rendered prompt string.

    Raises
    ------
    FileNotFoundError
        If the prompt file does not exist.
    jinja2.UndefinedError
        If a required template variable is missing.
    """
    template_path = _PROMPTS_DIR / f"{name}.txt"
    if not template_path.is_file():
        raise FileNotFoundError(f"Prompt template not found: {template_path}")

    source = template_path.read_text(encoding="utf-8")
    template = _jinja_env.from_string(source)
    return template.render(**kwargs)
