"""Tests for email composer prompt rendering."""

from scene_scout.services.prompt_loader import render_prompt


def test_email_composer_prompt_substitutes_jinja_variables() -> None:
    prompt = render_prompt(
        "email_composer",
        curator_name="Allegra",
        user_name="Morgan",
        profile_summary="Live music and comedy",
        recommendation_count=3,
        week_of="July 19, 2026",
        event_blocks="Event 1:\n  Title: Jazz Night",
        below_minimum=True,
    )

    assert "Allegra" in prompt
    assert "Morgan" in prompt
    assert "exactly 3 non-empty strings" in prompt
    assert "{curator_name}" not in prompt
    assert "{recommendation_count}" not in prompt
