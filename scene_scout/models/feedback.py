"""
Feedback domain models for SceneScout.

``FeedbackEvent`` is the validated payload written by :func:`feedback.log_signal`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FeedbackSignal = Literal["click", "negative"]


class FeedbackEvent(BaseModel):
    """Behavioral feedback signal captured from email tracking links."""

    model_config = ConfigDict(extra="forbid")

    token: str
    signal: FeedbackSignal
    run_id: str
    event_id: str | None = None
    rank: int | None = None
    categories: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, float] | None = None
    redirect_url: str | None = None
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @field_validator("token")
    @classmethod
    def _validate_token_is_uuid(cls, value: str) -> str:
        uuid.UUID(value)
        return value

    @field_validator("rank")
    @classmethod
    def _validate_rank_non_negative(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("rank must be non-negative when set")
        return value
