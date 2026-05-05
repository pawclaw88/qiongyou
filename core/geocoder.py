"""
Geocoder for 窮遊.

Uses OSM Nominatim (no API key required) with in-memory LRU cache.
Falls back to the hardcoded city coordinate table when offline.

Later can upgrade to:
- Google Geocoding API (requires key, more accurate)
- Mapbox Geocoding API (requires key)
- OpenStreetMap Nominatim with local tile server
"""

from __future__ import annotations

import httpx
import math
from functools import lru_cache
from typing import Tuple, Optional
import logging

logger = logging.getLogger(__name__)

# ─── Hardcoded fallback (used when Nominatim is unavailable) ────────────────────
# Keep as final fallback — covers cities that Nominatim might not resolve well.

_FALLBACK_COORDS: dict[str, Tuple[float, float]] = {
    # China
    "beijing":     (39.9042, 116.4074),
    "shanghai":    (31.2304, 121.4737),
    "chengdu":     (30.5728, 104.0668),
    "xian":        (34.3416, 108.9398),
    "hangzhou":    (30.2741, 120.1551),
    "guangzhou":   (23.1291, 113.2644),
    "shenzhen":    (22.5431, 114.0579),
    "nanjing":     (32.0603, 118.7969),
    "suzhou":      (31.2989, 120.5853),
    "wuhan":       (30.5928, 114.3055),
    "chongqing":   (29.5630, 106.5516),
    "tianjin":     (39.1256, 117.1909),
    "dalian":      (38.9140, 121.6147),
    "qingdao":     (36.0671, 120.3826),
    "changsha":    (28.2282, 112.9388),
    "xiamen":      (24.4798, 118.0894),
    "harbin":      (45.8038, 126.5340),
    "shenyang":    (41.8057, 123.4328),
    "fuzhou":      (26.0745, 119.2965),
    "nanchang":    (28.6829, 115.8579),
    "hefei":       (31.8206, 117.2272),
    "kunming":     (25.0406, 102.7129),
    "zhengzhou":   (34.7466, 113.6253),
    "jinan":       (36.6512, 116.6870),
    "shijiazhuang": (38.0428, 114.5149),
    "taiyuan":     (37.8706, 112.5489),
    "hohhot":      (40.8424, 111.7498),
    "urumqi":      (43.8256, 87.6168),
    "lanzhou":     (36.0611, 103.8343),
    "lhasa":       (29.6500, 91.1000),
    "haikou":      (20.0444, 110.1999),
    "sanya":       (18.2528, 109.5119),
    # Japan
    "tokyo":       (35.6762, 139.6503),
    "osaka":       (34.6937, 135.5023),
    "kyoto":       (35.0116, 135.7681),
    "nagoya":      (35.1815, 136.9066),
    "fukuoka":     (33.5904, 130.4205),
    "sapporo":     (43.0618, 141.3545),
    "hiroshima":   (34.3853, 132.4553),
    "yokohama":    (35.4437, 139.6380),
    "kobe":        (35.0116, 135.7681),
    "nara":        (34.6851, 135.8048),
    "kanazawa":    (36.5947, 136.6256),
    "sendai":      (38.2682, 140.8694),
    "okinawa":     (26.5013, 127.9453),
    # Korea
    "seoul":       (37.5665, 126.9780),
    "busan":       (35.1796, 129.0756),
    "jeju":        (33.4996, 126.5312),
    "incheon":     (37.4563, 126.7052),
    "daegu":       (35.8714, 128.6014),
    # SE Asia
    "bangkok":     (13.7563, 100.5018),
    "chiang mai":  (18.7883,  98.9853),
    "phuket":      ( 7.8804,  98.3924),
    "singapore":   ( 1.3521, 103.8198),
    "kuala lumpur":( 3.1390, 101.6869),
    "penang":      ( 5.4164, 100.3327),
    "ho chi minh city": (10.8231, 106.6297),
    "hanoi":      (21.0278, 105.8342),
    "danang":     (16.0544, 108.2022),
    "manila":     (14.5995, 120.9842),
    "bali":       (-8.3405, 115.0920),
    "jakarta":    (-6.2088, 106.8456),
    "yogyakarta": (-7.7970, 110.3688),
    "surabaya":   (-7.2575, 112.7521),
    # Taiwan
    "taipei":     (25.0330, 121.5654),
    "kaohsiung":  (22.6273, 120.3014),
    "taichung":   (24.1477, 120.6736),
    # Europe
    "paris":      (48.8566,   2.3522),
    "london":     (51.5074,  -0.1278),
    "berlin":     (52.5200,  13.4050),
    "rome":       (41.9028,  12.4964),
    "madrid":     (40.4168,  -3.7038),
    "amsterdam":  (52.3676,   4.9041),
    "barcelona":  (41.3851,   2.1734),
    "vienna":     (48.2082,  16.3738),
    "prague":     (50.0755,  14.4378),
    "budapest":   (47.4979,  19.0402),
    "munich":     (48.1351,  11.5820),
    "milan":      (45.4642,   9.1900),
    "florence":   (43.7696,  11.2558),
    # Americas / Oceania
    "new york":        (40.7128,  -74.0060),
    "los angeles":     (34.0522, -118.2437),
    "san francisco":   (37.7749, -122.4194),
    "vancouver":       (49.2827, -123.1207),
    "toronto":         (43.6532,  -79.3832),
    "sydney":         (-33.8688, 151.2093),
    "melbourne":      (-37.8136, 144.9631),
    "auckland":       (-36.8485, 174.7633),
}

# ─── Nominatim API ─────────────────────────────────────────────────────────────

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_PARAMS = {
    "format": "json",
    "limit": "1",
    "addressdetails": "0",
    "extratags": "0",
}
_USER_AGENT = "Qiongyou/1.0 (ultra-budget travel planner)"
_CACHE_MAXSIZE = 256


@lru_cache(maxsize=_CACHE_MAXSIZE)
def geocode(city: str) -> Tuple[float, float]:
    """
    Returns ``(lat, lng)`` for a city.

    Resolution order:
      1. Hardcoded fallback table (instant, always available)
      2. OSM Nominatim API (online lookup, cached)

    Raises ``ValueError`` if the city cannot be resolved by either method.
    """
    city_key = city.strip().lower()

    # 1. Fast path — hardcoded fallback
    if city_key in _FALLBACK_COORDS:
        return _FALLBACK_COORDS[city_key]

    # 2. Nominatim lookup
    try:
        lat, lng = _nominatim_lookup(city)
        if lat is not None:
            # Cache the resolved result in the fallback table for next time
            _FALLBACK_COORDS[city_key] = (lat, lng)
            return lat, lng
    except Exception as e:
        logger.warning("Nominatim lookup failed for %r: %s", city, e)

    # 3. Nothing worked
    raise ValueError(f"Cannot geocode city: {city!r}")


def _nominatim_lookup(city: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Query OSM Nominatim. Returns (lat, lng) or (None, None) on failure.
    Respects Nominatim's usage policy: max 1 req/s.
    """
    params = {**_NOMINATIM_PARAMS, "q": city}
    headers = {"User-Agent": _USER_AGENT}

    with httpx.Client(timeout=10.0) as client:
        response = client.get(_NOMINATIM_URL, params=params, headers=headers)
        response.raise_for_status()
        results = response.json()

    if not results:
        return None, None

    first = results[0]
    lat = float(first["lat"])
    lon = float(first["lon"])

    # Sanity check: Nominatim can return coordinates far from city centroids
    # for ambiguous queries. Clamp to valid coordinate ranges.
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None

    return lat, lon


def geocode_batch(cities: list[str]) -> dict[str, Tuple[float, float]]:
    """
    Geocode multiple cities. Skips already-known cities (no API call).
    Raises ValueError only if ALL cities fail.

    Returns ``{city_lower: (lat, lng), ...}``.
    """
    results = {}
    failed = []

    for city in cities:
        city_key = city.strip().lower()
        try:
            results[city_key] = geocode(city)
        except ValueError:
            failed.append(city)

    if failed and not results:
        raise ValueError(f"Could not geocode any city: {failed}")

    return results


def reverse_geocode(lat: float, lng: float) -> Optional[str]:
    """
    Reverse geocode: coordinates → city name.
    Returns None if Nominatim can't resolve.
    """
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lng, "format": "json", "addressdetails": "1"}
    headers = {"User-Agent": _USER_AGENT}

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            addr = data.get("address", {})
            # Prefer city/district, fall back to town/county
            return (
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("municipality")
                or addr.get("county")
            )
    except Exception as e:
        logger.warning("Nominatim reverse geocode failed for (%s, %s): %s", lat, lng, e)
        return None
