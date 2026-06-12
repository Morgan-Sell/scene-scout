"""
Tests for the geocoding service.

Nominatim and Overpass HTTP responses are mocked via respx so tests run offline.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import httpx
import pytest
import respx

from scene_scout.services import geocoding as geocoding_service
from scene_scout.services.cache import CacheService
from tests.conftest import TEST_RUN_ID

VENUE = "The Sandlot"
CITY = "Los Angeles"
VENUE_KEY = geocoding_service.venue_cache_key(VENUE, CITY)
LAT = 34.0522
LON = -118.2437

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    geocoding_service._last_request_at = None


@pytest.fixture
def cache() -> CacheService:
    return CacheService(run_id=TEST_RUN_ID)


def test_venue_cache_key_normalizes_whitespace_and_case() -> None:
    assert geocoding_service.venue_cache_key("  The Sandlot ", " Los Angeles ") == (
        "the sandlot|los angeles"
    )


@pytest.mark.asyncio
async def test_geocode_venue_returns_coordinates(respx_mock: respx.MockRouter) -> None:
    nominatim_payload = [
        {"lat": "34.0522", "lon": "-118.2437", "display_name": "The Sandlot"},
    ]
    respx_mock.get(NOMINATIM_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=nominatim_payload),
    )

    coordinates = await geocoding_service.geocode_venue(VENUE, CITY, run_id=TEST_RUN_ID)

    assert coordinates == (34.0522, -118.2437)
    request = respx_mock.calls[0].request
    assert request.url.params["q"] == f"{VENUE}, {CITY}"
    assert request.headers["User-Agent"]


@pytest.mark.asyncio
async def test_geocode_venue_returns_none_when_nominatim_has_no_results(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(NOMINATIM_SEARCH_URL).mock(return_value=httpx.Response(200, json=[]))

    assert await geocoding_service.geocode_venue(VENUE, CITY) is None


@pytest.mark.asyncio
async def test_geocode_venue_returns_none_on_http_error(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(NOMINATIM_SEARCH_URL).mock(
        return_value=httpx.Response(503, json={"error": "unavailable"})
    )

    assert await geocoding_service.geocode_venue(VENUE, CITY) is None


@pytest.mark.asyncio
async def test_geocode_venue_uses_venue_cache(
    respx_mock: respx.MockRouter,
    cache: CacheService,
) -> None:
    cache.set_venue(VENUE_KEY, coordinates=(LAT, LON))

    coordinates = await geocoding_service.geocode_venue(
        VENUE,
        CITY,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert coordinates == (LAT, LON)
    assert len(respx_mock.calls) == 0


@pytest.mark.asyncio
async def test_geocode_venue_stores_coordinates_in_venue_cache(
    respx_mock: respx.MockRouter,
    cache: CacheService,
) -> None:
    respx_mock.get(NOMINATIM_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=[{"lat": str(LAT), "lon": str(LON)}],
        )
    )

    coordinates = await geocoding_service.geocode_venue(
        VENUE,
        CITY,
        cache=cache,
        run_id=TEST_RUN_ID,
    )

    assert coordinates == (LAT, LON)
    cached = cache.get_venue(VENUE_KEY)
    assert cached is not None
    assert cached.coordinates == (LAT, LON)


@pytest.mark.asyncio
async def test_get_nearby_pois_returns_parsed_pois(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(OVERPASS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "lat": 34.0525,
                        "lon": -118.244,
                        "tags": {"amenity": "cafe", "name": "Treehouse Cafe"},
                    },
                    {
                        "type": "way",
                        "tags": {"amenity": "restaurant", "name": "Treehouse Cafe"},
                    },
                    {
                        "type": "node",
                        "lat": 34.051,
                        "lon": -118.242,
                        "tags": {"amenity": "park", "name": "Sandlot Park"},
                    },
                ]
            },
        )
    )

    pois = await geocoding_service.get_nearby_pois(LAT, LON, run_id=TEST_RUN_ID)

    assert pois == [
        {"name": "Treehouse Cafe", "type": "cafe", "lat": 34.0525, "lon": -118.244},
        {"name": "Sandlot Park", "type": "park", "lat": 34.051, "lon": -118.242},
    ]


@pytest.mark.asyncio
async def test_get_nearby_pois_returns_empty_list_on_http_error(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(OVERPASS_URL).mock(return_value=httpx.Response(504, json={}))

    assert await geocoding_service.get_nearby_pois(LAT, LON) == []


@pytest.mark.asyncio
async def test_get_nearby_pois_uses_venue_cache(
    respx_mock: respx.MockRouter,
    cache: CacheService,
) -> None:
    cached_pois = [{"name": "Treehouse", "type": "landmark"}]
    cache.set_venue(VENUE_KEY, poi_list=cached_pois)

    pois = await geocoding_service.get_nearby_pois(
        LAT,
        LON,
        cache=cache,
        venue_key=VENUE_KEY,
        run_id=TEST_RUN_ID,
    )

    assert pois == cached_pois
    assert len(respx_mock.calls) == 0


@pytest.mark.asyncio
async def test_get_nearby_pois_stores_results_in_venue_cache(
    respx_mock: respx.MockRouter,
    cache: CacheService,
) -> None:
    respx_mock.post(OVERPASS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "lat": 34.0525,
                        "lon": -118.244,
                        "tags": {"amenity": "landmark", "name": "Treehouse"},
                    }
                ]
            },
        )
    )

    pois = await geocoding_service.get_nearby_pois(
        LAT,
        LON,
        cache=cache,
        venue_key=VENUE_KEY,
        run_id=TEST_RUN_ID,
    )

    expected_poi = {
        "name": "Treehouse",
        "type": "landmark",
        "lat": 34.0525,
        "lon": -118.244,
    }
    assert pois == [expected_poi]
    cached = cache.get_venue(VENUE_KEY)
    assert cached is not None
    assert cached.poi_list == pois


@pytest.mark.asyncio
async def test_rate_limit_enforces_one_request_per_second(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(NOMINATIM_SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=[{"lat": str(LAT), "lon": str(LON)}]),
            httpx.Response(200, json=[{"lat": "34.0600", "lon": "-118.2500"}]),
        ]
    )

    with patch(
        "scene_scout.services.geocoding.GEOCODING_RATE_LIMIT_SECONDS",
        0.2,
    ):
        start = time.monotonic()
        await geocoding_service.geocode_venue(VENUE, CITY)
        await geocoding_service.geocode_venue("Dodger Stadium", CITY)
        elapsed = time.monotonic() - start

    assert elapsed >= 0.2
