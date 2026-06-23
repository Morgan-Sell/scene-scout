"""
Placeholder adapters for source types not yet implemented.

Returns a per-source health report without halting the pipeline.
"""

from datetime import datetime, timezone

from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.models.feed import (
    FeedConfig,
    FeedHealthReport,
    FeedStatus,
    RawFeedEntry,
    SourceType,
)

_PHASE_BY_TYPE: dict[SourceType, str] = {
    "ical": "1B.2",
    "api": "1B.3",
    "scrape": "1B.4",
}


class StubSourceAdapter:
    """Adapter that reports an unimplemented source type."""

    def __init__(self, source_type: SourceType) -> None:
        self._source_type = source_type

    async def fetch(
        self,
        config: FeedConfig,
        run_id: str,
        cache_hooks: CacheHooks,
    ) -> tuple[list[RawFeedEntry], FeedHealthReport]:
        """Return an empty entry list and a not-yet-implemented health report."""
        del run_id, cache_hooks
        phase = _PHASE_BY_TYPE[self._source_type]
        return [], FeedHealthReport(
            feed_id=config.id,
            feed_name=config.name,
            feed_url=config.url,
            status=FeedStatus.UNREACHABLE,
            entries_fetched=0,
            error_message=(
                f"Source type '{self._source_type}' adapter not implemented "
                f"(Phase {phase})"
            ),
            fetched_at=datetime.now(timezone.utc),
        )
