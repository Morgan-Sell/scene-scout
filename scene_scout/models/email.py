"""
Email composition domain models for SceneScout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EmailComposerLLMOutput(BaseModel):
    """LLM response schema for weekly email copy generation."""

    model_config = ConfigDict(extra="forbid")

    intro_paragraph: str = Field(min_length=1)
    event_descriptions: list[str]

    @model_validator(mode="before")
    @classmethod
    def _normalize_response_shape(cls, data: Any) -> Any:
        """Accept common alternate keys and coerce description items to strings."""
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if "event_descriptions" not in normalized and "descriptions" in normalized:
            normalized["event_descriptions"] = normalized.pop("descriptions")

        raw_descriptions = normalized.get("event_descriptions")
        if isinstance(raw_descriptions, list):
            coerced: list[str] = []
            for item in raw_descriptions:
                if isinstance(item, str):
                    coerced.append(item)
                elif isinstance(item, dict):
                    text = (
                        item.get("description") or item.get("text") or item.get("body")
                    )
                    if text is not None:
                        coerced.append(str(text))
            normalized["event_descriptions"] = coerced

        return normalized

    @field_validator("event_descriptions", mode="before")
    @classmethod
    def _strip_descriptions(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return value
        return [str(item).strip() for item in value if str(item).strip()]


class EmailComposerResult(BaseModel):
    """Rendered email output from the Email Composer Agent."""

    model_config = ConfigDict(extra="forbid")

    html: str
    subject: str
    preview_path: Path | None = None
    sent: bool = False
    resend_message_id: str | None = None
