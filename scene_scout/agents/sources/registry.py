"""
Source adapter registry and dispatch.
"""

from scene_scout.agents.sources.event_api import EventApiSourceAdapter
from scene_scout.agents.sources.ical import IcalSourceAdapter
from scene_scout.agents.sources.protocol import SourceAdapter
from scene_scout.agents.sources.rss import RssSourceAdapter
from scene_scout.agents.sources.stub import StubSourceAdapter
from scene_scout.models.feed import SourceType

_RSS_ADAPTER = RssSourceAdapter()
_ICAL_ADAPTER = IcalSourceAdapter()
_API_ADAPTER = EventApiSourceAdapter()
_STUB_ADAPTERS: dict[SourceType, SourceAdapter] = {
    "scrape": StubSourceAdapter("scrape"),
}


def get_adapter(source_type: SourceType) -> SourceAdapter:
    """Return the adapter registered for ``source_type``."""
    if source_type == "rss":
        return _RSS_ADAPTER
    if source_type == "ical":
        return _ICAL_ADAPTER
    if source_type == "api":
        return _API_ADAPTER
    return _STUB_ADAPTERS[source_type]
