"""
HTML calendar scraper source adapter.

Supports per-site configuration via ``FeedConfig.scrape``:

- ``json_api`` — fetch a JSON/XHR endpoint (preferred over DOM scraping)
- ``css`` — parse HTML with BeautifulSoup selectors
- ``json_embed`` — extract JSON from ``application/json`` script tags
"""

from __future__ import annotations

import html as html_module
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from scene_scout.agents.sources.protocol import CacheHooks
from scene_scout.models.feed import (
    FeedConfig,
    FeedHealthReport,
    FeedStatus,
    RawFeedEntry,
    ScrapeConfig,
)

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 15
_MIN_EXPECTED_ENTRIES = 1
_USER_AGENT = "SceneScout/0.1 (event discovery agent)"
_LAZY_JSON_URL_RE = re.compile(
    r'"url"\s*:\s*"(?P<url>/api/[^"]+\.json[^"]*)"', re.IGNORECASE
)
_CSRF_TOKEN_RE = re.compile(
    r'name="csrf-token"\s+content="(?P<token>[^"]+)"', re.IGNORECASE
)

_DEFAULT_JSON_FIELD_MAP = {
    "title": "bands",
    "link": "url",
    "published_raw": "starts_at",
    "description": "venue.name",
}


class HtmlCalendarSourceAdapter:
    """Adapter for HTML calendar pages and embedded JSON endpoints."""

    async def fetch(
        self,
        config: FeedConfig,
        run_id: str,
        cache_hooks: CacheHooks,
    ) -> tuple[list[RawFeedEntry], FeedHealthReport]:
        """Scrape events from a configured HTML calendar source."""
        if config.scrape is None:
            fetched_at = datetime.now(timezone.utc)
            return [], _health_report(
                config,
                FeedStatus.UNREACHABLE,
                fetched_at,
                error_message="scrape configuration is required for source_type=scrape",
            )

        if cache_hooks.client is not None:
            return await _scrape(config, cache_hooks.client, run_id)

        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            return await _scrape(config, client, run_id)


async def _scrape(
    config: FeedConfig,
    client: httpx.AsyncClient,
    run_id: str,
) -> tuple[list[RawFeedEntry], FeedHealthReport]:
    """Run the configured scrape strategy for one source."""
    fetched_at = datetime.now(timezone.utc)
    scrape = config.scrape
    assert scrape is not None

    try:
        if scrape.strategy == "json_api":
            items = await _fetch_json_api(config, scrape, client)
        elif scrape.strategy == "json_embed":
            items = await _fetch_json_embed(config, scrape, client)
        else:
            items = await _fetch_css(config, scrape, client)
    except httpx.TimeoutException:
        return [], _health_report(
            config,
            FeedStatus.UNREACHABLE,
            fetched_at,
            error_message="Request timed out",
        )
    except httpx.HTTPStatusError as exc:
        return [], _health_report(
            config,
            FeedStatus.UNREACHABLE,
            fetched_at,
            error_message=f"HTTP {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        return [], _health_report(
            config,
            FeedStatus.UNREACHABLE,
            fetched_at,
            error_message=str(exc),
        )
    except ValueError as exc:
        return [], _health_report(
            config,
            FeedStatus.MALFORMED,
            fetched_at,
            error_message=str(exc),
        )

    if not items:
        return [], _health_report(
            config,
            FeedStatus.EMPTY,
            fetched_at,
            entries_fetched=0,
            error_message="Scrape returned no event entries",
        )

    entries = _map_items_to_entries(items, config, scrape, fetched_at, run_id)
    status = (
        FeedStatus.OK if len(entries) >= _MIN_EXPECTED_ENTRIES else FeedStatus.STALE
    )
    return entries, _health_report(
        config,
        status,
        fetched_at,
        entries_fetched=len(entries),
    )


async def _fetch_json_api(
    config: FeedConfig,
    scrape: ScrapeConfig,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Fetch events from a JSON API endpoint."""
    landing_html = await _get_text(client, config.url)
    json_url = scrape.json_url
    if json_url is None and scrape.discover_json_url:
        json_url = _discover_json_url(landing_html)
    if not json_url:
        raise ValueError("json_api strategy requires json_url or discover_json_url")

    absolute_json_url = urljoin(config.url, json_url)
    headers: dict[str, str] = {
        "Accept": "application/json",
        "Referer": config.url,
        "X-Requested-With": "XMLHttpRequest",
    }
    if scrape.require_csrf:
        token = _extract_csrf_token(landing_html)
        if token:
            headers["X-CSRF-Token"] = token

    response = await client.get(absolute_json_url, headers=headers)
    response.raise_for_status()
    payload = response.json()
    return _extract_items(payload, scrape.json_items_path)


async def _fetch_json_embed(
    config: FeedConfig,
    scrape: ScrapeConfig,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Extract JSON payloads embedded in script tags."""
    html = await _get_text(client, config.url)
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        if not script.string:
            continue
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        items = _extract_items(payload, scrape.json_items_path)
        if items:
            return items
    raise ValueError("No embedded JSON event payload found")


async def _fetch_css(
    config: FeedConfig,
    scrape: ScrapeConfig,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Parse event listings from HTML using CSS selectors."""
    if not scrape.item_selector:
        raise ValueError("css strategy requires item_selector")

    html = await _get_text(client, config.url)
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []

    for element in soup.select(scrape.item_selector):
        title_el = (
            element.select_one(scrape.title_selector) if scrape.title_selector else None
        )
        link_el = (
            element.select_one(scrape.link_selector) if scrape.link_selector else None
        )
        description_el = (
            element.select_one(scrape.description_selector)
            if scrape.description_selector
            else None
        )
        date_el = (
            element.select_one(scrape.date_selector) if scrape.date_selector else None
        )

        title = title_el.get_text(" ", strip=True) if title_el else None
        link = link_el.get("href") if link_el else None
        description = (
            description_el.get_text(" ", strip=True) if description_el else None
        )
        published_raw = _css_date_text(date_el)

        if title or link:
            items.append(
                {
                    "title": title,
                    "url": link,
                    "description": description,
                    "published_raw": published_raw,
                }
            )

    return items


def _css_date_text(date_el: Any) -> Optional[str]:
    """Return a date string from a CSS-selected date element."""
    if date_el is None:
        return None
    text = date_el.get_text(" ", strip=True)
    if text:
        return text
    for attr in ("datetime", "content"):
        value = date_el.get(attr)
        if value:
            return str(value).strip()
    return None


async def _get_text(client: httpx.AsyncClient, url: str) -> str:
    """Fetch a URL and return the response body as text."""
    response = await client.get(url)
    response.raise_for_status()
    return response.text


def _discover_json_url(html: str) -> Optional[str]:
    """Discover a JSON API URL embedded in lazy-load markup."""
    decoded = html_module.unescape(html)

    # Prefer parsing data-lazy JSON blobs (OMR and similar sites).
    soup = BeautifulSoup(decoded, "html.parser")
    for element in soup.find_all(attrs={"data-lazy": True}):
        raw = element.get("data-lazy")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        url = payload.get("url")
        if isinstance(url, str) and url:
            return url.replace("\\u0026", "&")

    match = _LAZY_JSON_URL_RE.search(decoded)
    if not match:
        return None
    return match.group("url").replace("\\u0026", "&")


def _extract_csrf_token(html: str) -> Optional[str]:
    """Extract a CSRF token from HTML meta tags."""
    match = _CSRF_TOKEN_RE.search(html)
    return match.group("token") if match else None


def _extract_items(payload: Any, items_path: Optional[str]) -> list[dict[str, Any]]:
    """Navigate a JSON payload to the list of event objects."""
    if items_path:
        current: Any = payload
        for part in items_path.split("."):
            if not isinstance(current, dict) or part not in current:
                return []
            current = current[part]
        payload = current

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("shows", "events", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def _map_items_to_entries(
    items: list[dict[str, Any]],
    config: FeedConfig,
    scrape: ScrapeConfig,
    fetched_at: datetime,
    run_id: str,
) -> list[RawFeedEntry]:
    """Map scraped item dicts to ``RawFeedEntry`` models."""
    field_map = scrape.field_map or (
        _DEFAULT_JSON_FIELD_MAP if scrape.strategy == "json_api" else {}
    )
    base_url = _origin(config.url)

    entries: list[RawFeedEntry] = []
    for item in items:
        if scrape.strategy == "css" and not field_map:
            title = item.get("title")
            link = _absolute_url(base_url, item.get("url"))
            description = item.get("description")
            published_raw = item.get("published_raw")
        else:
            title = _resolve_field(item, field_map.get("title", "title"))
            link = _absolute_url(
                base_url, _resolve_field(item, field_map.get("link", "url"))
            )
            description = _resolve_field(
                item, field_map.get("description", "description")
            )
            published_raw = _resolve_field(
                item, field_map.get("published_raw", "published_raw")
            )

        if not title and not link:
            continue

        entries.append(
            RawFeedEntry(
                feed_id=config.id,
                feed_name=config.name,
                source_url=config.url,
                run_id=run_id,
                title=title,
                link=link,
                description=description,
                published_raw=published_raw,
                categories=[],
                fetched_at=fetched_at,
            )
        )

    return entries


def _resolve_field(item: dict[str, Any], spec: Optional[str]) -> Optional[str]:
    """Resolve a mapped field from a scraped item."""
    if not spec:
        return None
    if spec == "bands":
        bands = item.get("bands") or []
        names = [
            band.get("name")
            for band in bands
            if isinstance(band, dict) and band.get("name")
        ]
        return ", ".join(names) if names else None

    value: Any = item
    for part in spec.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None

    if value is None:
        return None
    return str(value).strip() or None


def _absolute_url(base_url: str, link: Optional[str]) -> Optional[str]:
    """Return an absolute URL for a possibly relative link."""
    if not link:
        return None
    return urljoin(base_url, link)


def _origin(url: str) -> str:
    """Return the scheme + netloc origin for a URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _health_report(
    config: FeedConfig,
    status: FeedStatus,
    fetched_at: datetime,
    *,
    entries_fetched: int = 0,
    error_message: Optional[str] = None,
) -> FeedHealthReport:
    """Build a ``FeedHealthReport`` for a scrape attempt."""
    return FeedHealthReport(
        feed_id=config.id,
        feed_name=config.name,
        feed_url=config.url,
        status=status,
        entries_fetched=entries_fetched,
        error_message=error_message,
        fetched_at=fetched_at,
    )
