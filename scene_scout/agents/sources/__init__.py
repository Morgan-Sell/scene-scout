"""Pluggable source adapters for Feed Scout ingestion."""

from scene_scout.agents.sources.protocol import CacheHooks, SourceAdapter
from scene_scout.agents.sources.registry import get_adapter

__all__ = ["CacheHooks", "SourceAdapter", "get_adapter"]
