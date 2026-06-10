"""
Deduplication Agent

Responsibility
--------------
Collapse duplicate ``NormalizedEvent`` records from multiple feeds into single
events with merged source provenance.

Design
------
Inputs  : list[NormalizedEvent], run_id: str
Outputs : Deduplicated list[NormalizedEvent]
"""

from __future__ import annotations

from collections import defaultdict

from rapidfuzz import fuzz

from scene_scout.deduplication_config import FUZZY_TITLE_SIMILARITY_THRESHOLD
from scene_scout.logging import get_logger
from scene_scout.models.event import NormalizedEvent


class _UnionFind:
    """Disjoint-set structure for duplicate clustering."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self._parent[root_right] = root_left


def title_similarity(left: str, right: str) -> float:
    """Return normalized title similarity in the range 0.0–1.0."""
    return fuzz.ratio(left, right) / 100.0


def same_event_date(left: NormalizedEvent, right: NormalizedEvent) -> bool:
    """Return True when both events share a calendar date (UTC)."""
    return left.start_datetime.date() == right.start_datetime.date()


def same_event_venue(left: NormalizedEvent, right: NormalizedEvent) -> bool:
    """Return True when venue names match case-insensitively."""
    return left.venue.strip().lower() == right.venue.strip().lower()


def is_fuzzy_duplicate(left: NormalizedEvent, right: NormalizedEvent) -> bool:
    """Return True when events match by fuzzy title, date, and venue."""
    if left.id == right.id:
        return False
    if not same_event_date(left, right) or not same_event_venue(left, right):
        return False
    return title_similarity(left.title, right.title) > FUZZY_TITLE_SIMILARITY_THRESHOLD


def are_duplicates(left: NormalizedEvent, right: NormalizedEvent) -> bool:
    """Return True when events should be merged."""
    if left.id == right.id:
        return True
    return is_fuzzy_duplicate(left, right)


def merge_events(events: list[NormalizedEvent]) -> NormalizedEvent:
    """Merge duplicate events, keeping content from the highest-quality source."""
    if len(events) == 1:
        return events[0]

    best = max(
        events,
        key=lambda event: (event.source_quality_score, event.best_source_feed),
    )
    source_feeds = sorted({feed for event in events for feed in event.source_feeds})

    return best.model_copy(
        update={
            "source_feeds": source_feeds,
            "source_count": len(source_feeds),
            "best_source_feed": best.best_source_feed,
            "source_quality_score": best.source_quality_score,
        }
    )


def _cluster_merge_type(cluster: list[NormalizedEvent]) -> str:
    if len(cluster) <= 1:
        return "none"
    if len({event.id for event in cluster}) == 1:
        return "exact_id"
    return "fuzzy"


def cluster_events(events: list[NormalizedEvent]) -> list[list[NormalizedEvent]]:
    """Group duplicate events into merge clusters."""
    if not events:
        return []

    union_find = _UnionFind(len(events))
    for left in range(len(events)):
        for right in range(left + 1, len(events)):
            if are_duplicates(events[left], events[right]):
                union_find.union(left, right)

    clusters: dict[int, list[NormalizedEvent]] = defaultdict(list)
    for index, event in enumerate(events):
        clusters[union_find.find(index)].append(event)
    return list(clusters.values())


def deduplicate_events(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
    """Cluster and merge duplicate events."""
    return [merge_events(cluster) for cluster in cluster_events(events)]


async def run(events: list[NormalizedEvent], run_id: str) -> list[NormalizedEvent]:
    """Deduplicate normalized events across feeds.

    Parameters
    ----------
    events : list[NormalizedEvent]
        Normalized events, potentially containing cross-feed duplicates.
    run_id : str
        Pipeline run identifier for logging.

    Returns
    -------
    list[NormalizedEvent]
        Deduplicated events with merged source provenance.
    """
    logger = get_logger("deduplication", run_id=run_id)

    if not events:
        logger.info(
            "Deduplication complete", data={"input_count": 0, "output_count": 0}
        )
        return []

    clusters = cluster_events(events)
    deduplicated: list[NormalizedEvent] = []
    merge_count = 0

    for cluster in clusters:
        merged = merge_events(cluster)
        deduplicated.append(merged)

        if len(cluster) > 1:
            merge_count += 1
            source_feed_ids = sorted(
                {feed for event in cluster for feed in event.source_feeds}
            )
            logger.info(
                "Merged duplicate events",
                data={
                    "merge_type": _cluster_merge_type(cluster),
                    "source_feed_ids": source_feed_ids,
                    "source_count": merged.source_count,
                    "event_id": merged.id,
                    "title": merged.title,
                    "best_source_feed": merged.best_source_feed,
                },
            )

    logger.info(
        "Deduplication complete",
        data={
            "input_count": len(events),
            "output_count": len(deduplicated),
            "merge_count": merge_count,
        },
    )
    return deduplicated
