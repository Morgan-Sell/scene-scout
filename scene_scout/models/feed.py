"""
Feed-level data models for SceneScout.

Models
------
FeedConfig
    A configured RSS source loaded from feeds.yaml.
RawFeedEntry
    A single entry read faithfully from an RSS feed.
FeedHealthReport
    The result of a single feed read attempt.
FeedStatus
    Outcome enumeration for a feed read attempt.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, field_validator

SourceType = Literal["rss", "ical", "api", "scrape"]
ScrapeStrategy = Literal["json_api", "css", "json_embed"]


class ScrapeConfig(BaseModel):
    """Per-site HTML calendar scrape configuration."""

    strategy: ScrapeStrategy = "css"
    json_url: Optional[str] = None
    discover_json_url: bool = False
    json_items_path: Optional[str] = None
    require_csrf: bool = False
    item_selector: Optional[str] = None
    title_selector: Optional[str] = "h3"
    link_selector: Optional[str] = "a"
    description_selector: Optional[str] = None
    date_selector: Optional[str] = None
    field_map: dict[str, str] = {}


class FeedStatus(str, Enum):
    """Outcome of a feed read attempt.

    Attributes
    ----------
    OK : str
        Feed fetched and parsed successfully with entries present.
    UNCHANGED : str
        Server returned 304 Not Modified. Feed skipped; no entries re-fetched.
        Prior run's seen_entries cache entries remain valid.
    UNREACHABLE : str
        Network error, timeout, or HTTP error status.
    MALFORMED : str
        Feed content could not be parsed; no usable entries returned.
    EMPTY : str
        Feed parsed successfully but contained no entries.
    STALE : str
        Fewer entries returned than the minimum expected threshold.
    """

    OK = "ok"
    UNCHANGED = "unchanged"
    UNREACHABLE = "unreachable"
    MALFORMED = "malformed"
    EMPTY = "empty"
    STALE = "stale"


class FeedConfig(BaseModel):
    """A configured event source as defined in feeds.yaml.

    Parameters
    ----------
    id : str
        Unique identifier for this feed. Used as a cache key.
    name : str
        Human-readable feed name for logging and UI display.
    url : str
        Source URL (RSS feed, ICS calendar, API endpoint, or scrape target).
    city : str
        Metro this feed covers when ``is_national`` is false; metadata only when
        ``is_national`` is true (API geo uses ``profile.home_city`` at fetch time).
    is_national : bool
        When true, feed is included for every user metro. National API adapters
        must query using the pipeline ``home_city``, not ``feed.city``.
    source_quality_score : float
        Feed reliability score from 0.0 to 1.0. Higher = more reliable event data.
        Defaults to 0.5 for user-added feeds until calibrated.
    active : bool
        Whether this feed is included in pipeline runs. Default True.
    notes : str, optional
        Operator notes about this feed's content and characteristics.
    source_type : SourceType
        Ingestion adapter to use. Defaults to ``"rss"`` for backward compatibility.
    cursor : str, optional
        Always None for RSS feeds. Reserved for cursor-based pagination APIs
        (e.g. Eventbrite, Meetup) that may be added as future sources.
        Allows the Feed Scout abstraction to accommodate non-RSS sources
        without a schema change.
    scrape : ScrapeConfig, optional
        Per-site selector or JSON API configuration when ``source_type`` is
        ``"scrape"``.
    """

    id: str
    name: str
    url: str
    city: str
    is_national: bool = False
    source_quality_score: float
    active: bool = True
    notes: Optional[str] = None
    source_type: SourceType = "rss"
    cursor: Optional[str] = None
    scrape: Optional[ScrapeConfig] = None

    @field_validator("source_quality_score")
    @classmethod
    def score_must_be_in_range(cls, v: float) -> float:
        """Validate that source_quality_score is between 0.0 and 1.0.

        Parameters
        ----------
        v : float
            The score value to validate.

        Returns
        -------
        float
            The validated score.

        Raises
        ------
        ValueError
            If the score is outside the 0.0–1.0 range.
        """
        if not 0.0 <= v <= 1.0:
            raise ValueError("source_quality_score must be between 0.0 and 1.0")
        return v


class RawFeedEntry(BaseModel):
    """A single entry read from an RSS feed, preserved faithfully.

    All content fields are Optional[str] by design. RSS is inconsistently
    implemented across sources. We capture what is present and pass it
    downstream faithfully without interpretation. The Extraction Agent
    decides what to do with missing or ambiguous fields.

    We do not parse dates here. We do not validate URLs here. We do not
    judge whether this is an event here. That judgment belongs downstream.

    Parameters
    ----------
    feed_id : str
        ID of the source feed. Used as part of the seen_entries cache key.
    feed_name : str
        Human-readable feed name for logging.
    source_url : str
        URL of the source feed.
    run_id : str
        Pipeline run identifier. Attached at fetch time for full traceability.
    title : str, optional
        Entry title as provided by the feed.
    link : str, optional
        URL of the entry. Used as part of the seen_entries cache key hash.
    description : str, optional
        Entry body or summary. Prefers full content over summary when available.
    published_raw : str, optional
        Raw date string exactly as provided by the feed. Not parsed here.
        Used as part of the seen_entries cache key hash.
    author : str, optional
        Entry author if provided.
    categories : list[str]
        Category tags from the feed. May be empty.
    enclosure_url : str, optional
        URL of an attached image or media file.
    fetched_at : datetime
        UTC timestamp when this entry was fetched.
    """

    feed_id: str
    feed_name: str
    source_url: str
    run_id: str

    title: Optional[str] = None
    link: Optional[str] = None
    description: Optional[str] = None
    published_raw: Optional[str] = None
    author: Optional[str] = None
    categories: list[str] = []
    enclosure_url: Optional[str] = None

    fetched_at: datetime


class FeedHealthReport(BaseModel):
    """The result of a single feed read attempt.

    Produced by the Feed Scout Agent after attempting to fetch and parse a
    feed. Used for logging, monitoring, and the web Dev Section feed
    health dashboard. A health report is always returned, even on failure,
    so the orchestrator has full visibility into every feed's status.

    Parameters
    ----------
    feed_id : str
        ID of the source feed.
    feed_name : str
        Human-readable feed name.
    feed_url : str
        URL of the feed that was fetched.
    status : FeedStatus
        Outcome of the fetch attempt.
    entries_fetched : int
        Number of entries returned. 0 for non-OK statuses.
    error_message : str, optional
        Human-readable error description for non-OK statuses.
    fetched_at : datetime
        UTC timestamp of the fetch attempt.
    feed_last_modified : str, optional
        Value of the Last-Modified header from the HTTP response, if present.
        Stored for use as If-Modified-Since on the next request.
    etag : str, optional
        Value of the ETag header from the HTTP response, if present.
        Stored for use as If-None-Match on the next request.
    etag_supported : bool
        True if the server returned an ETag or Last-Modified header.
        Logged for ETag support coverage tracking in the Dev Section.
    """

    feed_id: str
    feed_name: str
    feed_url: str
    status: FeedStatus
    entries_fetched: int = 0
    error_message: Optional[str] = None
    fetched_at: datetime
    feed_last_modified: Optional[str] = None
    etag: Optional[str] = None
    etag_supported: bool = False

    @property
    def succeeded(self) -> bool:
        """Return True if the feed was fetched and parsed successfully.

        Returns
        -------
        bool
            True for OK status only. UNCHANGED is not considered a success
            because no new entries were fetched, but it is not a failure either.
        """
        return self.status == FeedStatus.OK

    @property
    def skipped(self) -> bool:
        """Return True if the feed was intentionally skipped due to 304.

        Returns
        -------
        bool
            True for UNCHANGED status only.
        """
        return self.status == FeedStatus.UNCHANGED
