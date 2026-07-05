"""
Source adapter protocol for multi-format ingestion.

Each adapter fetches from one source type and normalizes results to
``RawFeedEntry`` plus a per-source ``FeedHealthReport``.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import httpx

from scene_scout.models.feed import FeedConfig, FeedHealthReport, RawFeedEntry

GetFeedEtagFn = Callable[[str], Optional[tuple[Optional[str], Optional[str]]]]
StoreFeedEtagFn = Callable[[str, Optional[str], Optional[str]], None]


@dataclass
class CacheHooks:
    """Optional cache and HTTP dependencies passed to source adapters."""

    client: Optional[httpx.AsyncClient] = None
    get_feed_etag: Optional[GetFeedEtagFn] = None
    store_feed_etag: Optional[StoreFeedEtagFn] = None
    home_city: Optional[str] = None


class SourceAdapter(Protocol):
    """Protocol for pluggable source ingestion adapters."""

    async def fetch(
        self,
        config: FeedConfig,
        run_id: str,
        cache_hooks: CacheHooks,
    ) -> tuple[list[RawFeedEntry], FeedHealthReport]:
        """Fetch entries from a configured source.

        Parameters
        ----------
        config : FeedConfig
            Source configuration including ``source_type`` and URL.
        run_id : str
            Pipeline run identifier attached to every produced entry.
        cache_hooks : CacheHooks
            Shared HTTP client and optional ETag cache callbacks.

        Returns
        -------
        tuple[list[RawFeedEntry], FeedHealthReport]
            Normalized entries and a health report for this source.
        """
        ...
