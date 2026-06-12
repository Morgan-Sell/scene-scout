"""
OpenStreetMap geocoding for SceneScout.

``geocode_venue`` resolves venue names via Nominatim. ``get_nearby_pois`` queries
Overpass for amenity nodes within a walking radius. Results are cached in
``venue_cache`` with the 90-day geo TTL enforced by :class:`CacheService`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from scene_scout.geocoding_config import (
    DEFAULT_POI_RADIUS_M,
    GEOCODING_HTTP_TIMEOUT_SECONDS,
    GEOCODING_RATE_LIMIT_SECONDS,
    NOMINATIM_BASE_URL,
    NOMINATIM_USER_AGENT,
    OVERPASS_API_URL,
)
from scene_scout.logging import get_logger
from scene_scout.services.cache import CacheService

_rate_lock = asyncio.Lock()
_last_request_at: float | None = None


def venue_cache_key(venue: str, city: str) -> str:
    """Return the ``venue_cache`` key for a venue and city pair."""
    return f"{venue.strip().lower()}|{city.strip().lower()}"


def coord_cache_key(lat: float, lon: float) -> str:
    """Return a cache key for coordinate-only lookups."""
    return f"coord:{lat:.5f}:{lon:.5f}"


async def _throttle() -> None:
    """Enforce Nominatim's 1 request per second usage policy."""
    global _last_request_at
    async with _rate_lock:
        now = time.monotonic()
        if _last_request_at is not None:
            elapsed = now - _last_request_at
            if elapsed < GEOCODING_RATE_LIMIT_SECONDS:
                await asyncio.sleep(GEOCODING_RATE_LIMIT_SECONDS - elapsed)
        _last_request_at = time.monotonic()


def _nominatim_headers() -> dict[str, str]:
    return {"User-Agent": NOMINATIM_USER_AGENT}


def _parse_nominatim_coordinates(
    payload: list[dict[str, Any]],
) -> tuple[float, float] | None:
    if not payload:
        return None
    first = payload[0]
    lat_raw = first.get("lat")
    lon_raw = first.get("lon")
    if lat_raw is None or lon_raw is None:
        return None
    try:
        return float(str(lat_raw)), float(str(lon_raw))
    except ValueError:
        return None


def _poi_type_from_tags(tags: dict[str, Any]) -> str:
    for key in ("amenity", "tourism", "shop", "leisure", "historic", "building"):
        value = tags.get(key)
        if value:
            return str(value)
    return "place"


def _poi_name_from_tags(tags: dict[str, Any]) -> str | None:
    for key in ("name", "brand", "operator"):
        value = tags.get(key)
        if value:
            return str(value)
    return None


def _parse_overpass_pois(payload: dict[str, Any]) -> list[dict[str, Any]]:
    elements = payload.get("elements")
    if not isinstance(elements, list):
        return []

    pois: list[dict[str, Any]] = []
    seen: set[str] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue
        name = _poi_name_from_tags(tags)
        if not name:
            continue
        name_key = name.lower()
        if name_key in seen:
            continue
        seen.add(name_key)
        poi_type = _poi_type_from_tags(tags)
        poi: dict[str, Any] = {"name": name, "type": poi_type}
        element_lat = element.get("lat")
        element_lon = element.get("lon")
        if element_lat is not None and element_lon is not None:
            poi["lat"] = float(element_lat)
            poi["lon"] = float(element_lon)
        pois.append(poi)
    return pois


def _build_overpass_poi_query(lat: float, lon: float, radius_m: int) -> str:
    return (
        f"[out:json][timeout:25];("
        f'node(around:{radius_m}, {lat}, {lon})["amenity"];'
        f'way(around:{radius_m}, {lat}, {lon})["amenity"];'
        f");out body;"
    )


async def geocode_venue(
    venue: str,
    city: str,
    *,
    cache: CacheService | None = None,
    run_id: str = "",
) -> tuple[float, float] | None:
    """Geocode a venue via Nominatim, returning ``(lat, lon)`` or ``None``.

    Coordinates are cached in ``venue_cache`` under :func:`venue_cache_key`.
    """
    venue = venue.strip()
    city = city.strip()
    if not venue or not city:
        return None

    logger = get_logger("geocoding", run_id=run_id)
    cache_key = venue_cache_key(venue, city)

    if cache is not None:
        cached = cache.get_venue(cache_key)
        if cached is not None and cached.coordinates is not None:
            logger.info(
                "Geocode cache hit",
                data={"venue_key": cache_key, "coordinates": cached.coordinates},
            )
            return cached.coordinates

    query = f"{venue}, {city}"
    url = f"{NOMINATIM_BASE_URL}/search"
    params = {"q": query, "format": "json", "limit": 1}

    await _throttle()
    try:
        async with httpx.AsyncClient(timeout=GEOCODING_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                url,
                params=params,
                headers=_nominatim_headers(),
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Nominatim geocode failed",
            data={"venue": venue, "city": city, "error": str(exc)},
        )
        return None

    if not isinstance(payload, list):
        logger.warning(
            "Nominatim geocode returned unexpected payload",
            data={"venue": venue, "city": city},
        )
        return None

    coordinates = _parse_nominatim_coordinates(payload)
    if coordinates is None:
        logger.info(
            "Nominatim geocode returned no coordinates",
            data={"venue": venue, "city": city},
        )
        return None

    if cache is not None:
        cache.set_venue(cache_key, coordinates=coordinates)

    logger.info(
        "Geocoded venue",
        data={"venue": venue, "city": city, "coordinates": coordinates},
    )
    return coordinates


async def get_nearby_pois(
    lat: float,
    lon: float,
    radius_m: int = DEFAULT_POI_RADIUS_M,
    *,
    cache: CacheService | None = None,
    run_id: str = "",
    venue_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return nearby point-of-interest dicts within ``radius_m`` meters of a coordinate.

    Each dict contains at least ``name`` and ``type`` keys. When ``venue_key`` is
    provided, results are cached in ``venue_cache`` with the 90-day geo TTL.
    """
    logger = get_logger("geocoding", run_id=run_id)
    cache_key = venue_key or coord_cache_key(lat, lon)

    if cache is not None:
        cached = cache.get_venue(cache_key)
        if cached is not None and cached.poi_list is not None:
            logger.info(
                "POI cache hit",
                data={"venue_key": cache_key, "poi_count": len(cached.poi_list)},
            )
            return cached.poi_list

    await _throttle()
    try:
        async with httpx.AsyncClient(timeout=GEOCODING_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                OVERPASS_API_URL,
                data={"data": _build_overpass_poi_query(lat, lon, radius_m)},
                headers={"User-Agent": NOMINATIM_USER_AGENT},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Overpass POI query failed",
            data={"lat": lat, "lon": lon, "error": str(exc)},
        )
        return []

    if not isinstance(payload, dict):
        logger.warning(
            "Overpass returned unexpected payload",
            data={"lat": lat, "lon": lon},
        )
        return []

    pois = _parse_overpass_pois(payload)
    if cache is not None:
        cache.set_venue(cache_key, poi_list=pois)

    logger.info(
        "Fetched nearby POIs",
        data={"lat": lat, "lon": lon, "poi_count": len(pois)},
    )
    return pois
