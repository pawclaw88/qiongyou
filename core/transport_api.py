"""
core/transport_api.py

Real transport lookup with intelligent fallback chain:
  1. OpenRouteService (ORS) — free key, 2,000 req/day, provides route_name + operator
  2. OSRM — free, no key, unlimited, for real distances/durations
  3. Heuristic cost estimation — based on mode × distance
  4. Hardcoded matrix — final fallback for known popular routes

Return: (cost_CNY, duration_minutes, distance_km) or None on complete failure.

ORS enriches segments with route_name and operator; OSRM falls back gracefully.
"""

from __future__ import annotations

import httpx
import math
import os
from dataclasses import dataclass
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# ─── OSRM public demo server ──────────────────────────────────────────────────
# Rate-limit: do not exceed ~1 req/s in production. In production, host your own.
_OSRM_BASE = "https://router.project-osrm.org"
_USER_AGENT = "Qiongyou/1.0 (ultra-budget travel planner)"

# ─── OpenRouteService ────────────────────────────────────────────────────────
# Sign up free at openrouteservice.org — 2,000 req/day, provides operators + route names.
# Reads ORS_API_KEY from environment (set by app/config.py at startup).
_ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()
_ORS_BASE = "https://api.openrouteservice.org"

# Module-level extra dict set by _ors_route(), consumed by routing_engine._lookup()
_ors_extra: dict = {}

# ─── Cost estimation rates (CNY per km) ─────────────────────────────────────────
# Derived from: 12306 pricing (China HSR ¥0.45-0.55/km 2nd class),
#               typical bus ¥0.25-0.35/km, domestic flight ¥1.0/km base.

_COST_RATES: dict[str, float] = {
    "train":    0.45,   # CNY/km — high-speed rail 2nd class
    "subway":   0.15,   # CNY/km — urban transit flat fare absorbed
    "bus":      0.28,   # CNY/km — intercity coach
    "taxi":     3.50,   # CNY/km — urban taxi (flag fall ¥12 handled separately)
    "walk":     0.00,   # CNY/km — free
    "ferry":    0.60,   # CNY/km — cross-border ferry average
    "flight":   0.90,   # CNY/km — budget domestic flight base
}

# Ferry routes we know — not routable by OSRM (water).
_FERRY_MATRIX: dict[Tuple[str, str], Tuple[float, int, float]] = {
    ("shanghai", "osaka"):     (800.0, 1200, 1600),
    ("osaka",   "seoul"):      (700.0, 1000, 850),
    ("fukuoka", "busan"):      (350.0,  210, 200),
    ("seoul",   "jeju"):       (350.0,  300, 300),
    ("taichung","kaohsiung"):  (230.0,  100, 165),
}

# Known best routes that override the heuristic (e.g., China HSR deals).
_OVERRIDE_MATRIX: dict[Tuple[str, str, str], Tuple[float, int, float]] = {
    ("beijing", "shanghai", "train"):  (553.0, 261, 1318),
    ("beijing", "shanghai", "bus"):    (300.0, 420, 1318),
    ("beijing", "xian",     "train"):  (515.5, 258,  912),
    ("beijing", "chengdu",   "train"):  (777.0, 363, 1512),
    ("shanghai","hangzhou",  "train"): ( 73.0,  45,  180),
    ("shanghai","suzhou",    "train"): ( 25.0,  24,   84),
    ("shanghai","nanjing",   "train"): (110.5,  59,  300),
    ("chengdu",  "xian",     "train"): (263.5, 180,  710),
    ("chengdu",  "chongqing","train"): (154.0, 120,  505),
    ("hangzhou", "nanjing",   "train"): (128.0,  80,  460),
    ("beijing",  "tokyo",    "train"):(2400.0, 480, 2100),
    ("tokyo",    "osaka",    "train"): (875.0, 144,  515),
    ("tokyo",    "kyoto",    "train"): (852.0, 138,  476),
    ("osaka",    "kyoto",    "train"): (570.0,  29,   45),
    ("osaka",    "nagoya",   "train"): (560.0,  49,  186),
    ("seoul",    "busan",    "train"): (600.0, 165,  325),
}


# ─── Dataclasses for structured return ──────────────────────────────────────────

@dataclass
class RouteInfo:
    distance_km: float
    duration_minutes: int
    cost_cny: float

    def as_tuple(self) -> Tuple[float, int, float]:
        return self.cost_cny, self.duration_minutes, self.distance_km


# ─── ORS routing ─────────────────────────────────────────────────────────────────

def _ors_profile(mode: str) -> str:
    """Map transport mode to ORS profile."""
    return {
        "walk":    "foot-walking",
        "bike":    "cycling-regular",
        "bus":     "bus",
        "train":   "driving",
        "taxi":    "driving",
        "subway":  "driving",
        "flight":  "driving",
    }.get(mode, "driving")


def _ors_route(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    mode: str,
) -> Optional[RouteInfo]:
    """
    Query OpenRouteService for real distances + route_name + operator.
    Returns None if ORS is not configured, times out, or returns no route.

    Sets module-level _ors_extra on success so the caller can read route_name/operator.
    """
    global _ors_extra
    _ors_extra = {}

    if not _ORS_API_KEY:
        return None

    profile = _ors_profile(mode)
    url = f"{_ORS_BASE}/v2/directions/{profile}"

    params = {
        "api_key":     _ORS_API_KEY,
        "start":       f"{lon1},{lat1}",
        "end":         f"{lon2},{lat2}",
    }

    try:
        with httpx.Client(timeout=12.0) as client:
            response = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
            response.raise_for_status()
            data = response.json()

        routes = data.get("routes", [])
        if not routes:
            return None

        summary = routes[0].get("summary", {})
        distance_m = summary.get("distance", 0)   # meters
        duration_s = summary.get("duration", 0)  # seconds

        # ORS doesn't provide route_name/operator in the free tier directions endpoint.
        # Extract via the legs summary if available.
        legs = routes[0].get("legs", [])
        route_name = ""
        operator   = ""

        for leg in legs:
            for step in leg.get("steps", []):
                name = step.get("name", "") or step.get("mode", "").lower()
                if name and not route_name:
                    route_name = name
                ops = step.get("operator", "")
                if ops and not operator:
                    operator = ops

        if not route_name:
            route_name = "OpenRouteService route"

        _ors_extra = {"route_name": route_name, "operator": operator}

        return RouteInfo(
            distance_km=round(distance_m / 1000, 1),
            duration_minutes=max(1, int(duration_s / 60)),
            cost_cny=0.0,  # filled in by caller
        )

    except httpx.TimeoutException:
        logger.warning("ORS timeout: %.4f,%.4f → %.4f,%.4f", lat1, lon1, lat2, lon2)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning("ORS HTTP %d: %s", e.response.status_code, url)
        return None
    except Exception as e:
        logger.warning("ORS error: %s", e)
        return None


# ─── OSRM ───────────────────────────────────────────────────────────────────────

def _osrm_route(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    mode: str,
) -> Optional[RouteInfo]:
    """
    Query OSRM for real driving/walking/cycling distances.
    Transport modes map to OSRM profiles:
      train  → driving (approximates rail corridor)
      bus    → driving
      taxi   → driving
      subway → walking (OSRM has no transit profile; use walk for distance only)
      flight → not supported (use haversine fallback)
    """
    profile = _osrm_profile(mode)
    url = f"{_OSRM_BASE}/route/v1/{profile}/{lon1},{lat1};{lon2},{lat2}"

    params = {
        "overview": "false",
        "steps": "false",
        "annotations": "false",
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
            response.raise_for_status()
            data = response.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            return None

        route = data["routes"][0]
        distance_m = route["distance"]  # meters
        duration_s = route["duration"]  # seconds

        return RouteInfo(
            distance_km=round(distance_m / 1000, 1),
            duration_minutes=max(1, int(duration_s / 60)),
            cost_cny=0.0,  # filled in by caller
        )

    except httpx.TimeoutException:
        logger.warning("OSRM timeout: %.4f,%.4f → %.4f,%.4f", lat1, lon1, lat2, lon2)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning("OSRM HTTP %d: %s", e.response.status_code, url)
        return None
    except Exception as e:
        logger.warning("OSRM error: %s", e)
        return None


def _osrm_profile(mode: str) -> str:
    """Map transport mode to OSRM profile name."""
    return {
        "train":  "driving",
        "bus":    "driving",
        "taxi":   "driving",
        "subway": "walking",
        "flight": "driving",
    }.get(mode, "driving")


# ─── Cost estimation ─────────────────────────────────────────────────────────────

def _estimate_cost(distance_km: float, mode: str) -> float:
    """Estimate CNY cost based on distance × mode rate + fixed costs."""
    rate = _COST_RATES.get(mode, 0.30)
    cost = distance_km * rate

    # Flat fixed costs for certain modes
    if mode == "bus" and distance_km > 200:
        cost += 10   # long-distance coach terminal fees
    if mode == "taxi":
        cost += 12   # flag fall

    return round(cost, 2)


# ─── Walk / Haversine ───────────────────────────────────────────────────────────

def _walk_route(
    lat1: Optional[float], lon1: Optional[float],
    lat2: Optional[float], lon2: Optional[float],
) -> Optional[Tuple[float, int, float]]:
    """Walk route: 5km/h, free. Uses haversine if no OSRM."""
    if lat1 is not None and lon1 is not None and lat2 is not None and lon2 is not None:
        dist = _haversine(lat1, lon1, lat2, lon2)
        if dist < 0.1:
            dist = 0.1
        dur = max(5, int(dist / 5.0 * 60))
        return 0.0, dur, round(dist, 1)
    return None


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ─── Ferry heuristic ───────────────────────────────────────────────────────────

def _ferry_heuristic(
    lat1: Optional[float], lon1: Optional[float],
    lat2: Optional[float], lon2: Optional[float],
) -> Optional[Tuple[float, int, float]]:
    """Estimate ferry cost from distance. Requires coordinates."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    dist = _haversine(lat1, lon1, lat2, lon2)
    if dist < 1:
        dist = 1.0
    # Sea routes are typically 1.2–1.5x the great-circle distance
    actual_dist = dist * 1.3
    cost = round(actual_dist * _COST_RATES["ferry"], 2)
    # Ferries have fixed port fees ~¥80-150
    cost += 100
    dur = max(30, int(actual_dist / 25 * 60))  # avg 25 km/h
    return cost, dur, round(actual_dist, 1)


# ─── Public API ─────────────────────────────────────────────────────────────────

def get_route(
    from_city: str,
    to_city: str,
    mode: str,
    *,
    from_lat: Optional[float] = None,
    from_lng: Optional[float] = None,
    to_lat: Optional[float] = None,
    to_lng: Optional[float] = None,
) -> Optional[Tuple[float, int, float]]:
    """
    Returns ``(cost_CNY, duration_minutes, distance_km)`` for a leg.

    Resolution order (when coordinates are available):
      1. ORS — if ORS_API_KEY is set and route is found (provides route_name + operator)
      2. OSRM — always available, free
      3. Heuristic cost estimation (using OSRM distance if available)
      4. Hardcoded override matrix
      5. Ferry matrix / ferry heuristic
      6. Walk haversine

    Returns ``None`` if the route cannot be resolved by any method.
    """
    global _ors_extra
    _ors_extra = {}   # reset each call

    from_lower = from_city.strip().lower()
    to_lower   = to_city.strip().lower()
    mode_lower = mode.strip().lower()

    has_coords = (
        from_lat is not None and from_lng is not None
        and to_lat is not None and to_lng is not None
    )

    # 1. Hardcoded override matrix (known best prices)
    key = (from_lower, to_lower, mode_lower)
    if key in _OVERRIDE_MATRIX:
        logger.debug("Override hit: %s %s %s", from_city, to_city, mode)
        return _OVERRIDE_MATRIX[key]

    # 2. Ferry routes (not routable by OSRM — water)
    if mode_lower == "ferry":
        ferry_key = (from_lower, to_lower)
        rev_ferry_key = (to_lower, from_lower)
        if ferry_key in _FERRY_MATRIX:
            return _FERRY_MATRIX[ferry_key]
        if rev_ferry_key in _FERRY_MATRIX:
            cost, dur, dist = _FERRY_MATRIX[rev_ferry_key]
            return cost, dur, dist
        # Fallback: heuristic
        return _ferry_heuristic(from_lat, from_lng, to_lat, to_lng)

    # 3. Walk — always free, OSRM or haversine
    if mode_lower == "walk":
        return _walk_route(from_lat, from_lng, to_lat, to_lng)

    # 4. ORS routing (enriched with route_name + operator)
    if has_coords:
        info = _ors_route(from_lat, from_lng, to_lat, to_lng, mode_lower)
        if info is not None:
            cost = _estimate_cost(info.distance_km, mode_lower)
            info.cost_cny = cost
            logger.debug(
                "ORS hit: %s → %s (%s): %.1fkm %.0fmin ¥%.2f operator=%r",
                from_city, to_city, mode, info.distance_km, info.duration_minutes, cost,
                _ors_extra.get("operator", ""),
            )
            return info.as_tuple()

    # 5. OSRM routing + heuristic cost
    if has_coords:
        info = _osrm_route(from_lat, from_lng, to_lat, to_lng, mode_lower)
        if info is not None:
            cost = _estimate_cost(info.distance_km, mode_lower)
            info.cost_cny = cost
            logger.debug(
                "OSRM hit: %s → %s (%s): %.1fkm %.0fmin ¥%.2f",
                from_city, to_city, mode, info.distance_km, info.duration_minutes, cost,
            )
            return info.as_tuple()

    # 6. Complete failure
    logger.warning("No route found: %s → %s by %s", from_city, to_city, mode)
    return None


def get_ors_extra() -> dict:
    """Return the last ORS extra dict (route_name, operator) from the previous get_route() call."""
    return _ors_extra


# ─── Batch helper ───────────────────────────────────────────────────────────────

def get_routes_batch(
    legs: list[Tuple[str, str, str, Optional[Tuple[float, float]], Optional[Tuple[float, float]]]],
) -> dict[int, Optional[Tuple[float, int, float]]]:
    """
    Bulk route lookup. ``legs`` is a list of:
      (from_city, to_city, mode, (from_lat, from_lng), (to_lat, to_lng))

    Returns ``{leg_index: result_or_None}``.
    Caller is responsible for managing OSRM/ORS rate limits.
    """
    results = {}
    for i, (from_c, to_c, mode, from_coords, to_coords) in enumerate(legs):
        from_lat, from_lng = from_coords if from_coords else (None, None)
        to_lat,   to_lng   = to_coords   if to_coords   else (None, None)
        results[i] = get_route(from_c, to_c, mode,
                               from_lat=from_lat, from_lng=from_lng,
                               to_lat=to_lat,     to_lng=to_lng)
    return results
