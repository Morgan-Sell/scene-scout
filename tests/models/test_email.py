"""Tests for email composition models."""

from scene_scout.models.email import EmailComposerLLMOutput


def test_email_composer_llm_output_accepts_descriptions_alias() -> None:
    output = EmailComposerLLMOutput.model_validate(
        {
            "intro_paragraph": "Hello there.",
            "descriptions": ["First event.", "Second event."],
        }
    )

    assert output.event_descriptions == ["First event.", "Second event."]


def test_email_composer_llm_output_coerces_object_descriptions() -> None:
    output = EmailComposerLLMOutput.model_validate(
        {
            "intro_paragraph": "Hello there.",
            "event_descriptions": [
                {"description": "Brass night at the Sandlot."},
                "Second event.",
            ],
        }
    )

    assert output.event_descriptions == [
        "Brass night at the Sandlot.",
        "Second event.",
    ]
