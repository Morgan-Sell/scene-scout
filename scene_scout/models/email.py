"""
Email composition domain models for SceneScout.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class EmailComposerLLMOutput(BaseModel):
    """LLM response schema for weekly email copy generation."""

    model_config = ConfigDict(extra="forbid")

    intro_paragraph: str = Field(min_length=1)
    event_descriptions: list[str]


class EmailComposerResult(BaseModel):
    """Rendered email output from the Email Composer Agent."""

    model_config = ConfigDict(extra="forbid")

    html: str
    subject: str
    preview_path: Path | None = None
    sent: bool = False
    resend_message_id: str | None = None
