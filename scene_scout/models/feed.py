"""
Feed-level data models for SceneScout.

These models represent:
  - FeedConfig: a configured RSS source (from feeds.yaml)
  - RawFeedEntry: a single entry as read from a feed, faithfully
  - FeedHealthReport: the result of a feed read attempt

Design principle: these models are intentionally permissive.
RawFeedEntry preserves what the feed said. Cleaning, parsing, and
judgment happen downstream in the Extraction and Normalization agents.
Do not validate, interpret, or discard data here.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, field_validator


class FeedConfig(BaseModel):
    """A configured RSS feed source, as defined in feeds.yaml."""

    id: str
    name: str
    url: str
    city: str
    source_quality_score: float  # 0.0–1.0
    active: bool = True
    notes: Optional[str] = None

    @field_validator("source_quality_score")
    @classmethod
    def score_must_be_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("source_quality_score must be between 0.0 and 1.0")
        return v


class FeedStatus(str, Enum):
    """Outcome of a feed read attempt."""

    OK = "ok"
    UNREACHABLE = "unreachable"
    MALFORMED = "malformed"
    EMPTY = "empty"
    STALE = "stale"


class RawFeedEntry(BaseModel):
    """
    A single entry read from an RSS feed.

    All content fields are Optional[str] by design. RSS is inconsistently
    implemented across sources. We capture what is present and pass it
    downstream faithfully. The Extraction Agent decides what to do with gaps.

    We do not parse dates here. We do not validate URLs here.
    We do not judge whether this is an event here.

    run_id is attached at fetch time so every entry can be traced back
    to the exact pipeline execution that produced it.
    """

    feed_id: str
    feed_name: str
    source_url: str
    run_id: str

    title: Optional[str] = None
    link: Optional[str] = None
    description: Optional[str] = None
    published_raw: Optional[str] = None   # Raw date string — not parsed
    author: Optional[str] = None
    categories: list[str] = []
    enclosure_url: Optional[str] = None   # Image or media attachment

    fetched_at: datetime


class FeedHealthReport(BaseModel):
    """
    The result of a single feed read attempt.

    Produced by the Feed Scout Agent after attempting to fetch and parse a feed.
    Used for logging, monitoring, and skipping downstream processing of failed feeds.
    """

    feed_id: str
    feed_name: str
    feed_url: str
    status: FeedStatus
    entries_fetched: int = 0
    error_message: Optional[str] = None
    fetched_at: datetime
    feed_last_modified: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status == FeedStatus.OK
