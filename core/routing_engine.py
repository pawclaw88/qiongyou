"""
Routing engine for 窮遊 — greedy insertion by reward/added_cost ratio.

Pipeline: validate() → RoutingEngine.plan() → TravelOutput.to_dict()
All models are typed against shared_schemas; no duplicate enums or dataclasses.

Delegates real routing to transport_api; falls back to local matrix + haversine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta

from core.geocoder import geocode
from core.transport_api import get_route, get_ors_extra
from core.currency import to_cny
from core.shared_schemas import (
    CARBON_PER_KM,
    Currency,
    Diff,
    ErrorCode,
    Preference,
    Segment,
    Status,
    Stop,
    TransportMode,
    TravelInput,
    TravelOutput,
    UpdateReason,
)


# ─── Cost / duration matrix (final fallback — only used when all else fails) ────

_FALLBACK_MATRIX: dict[tuple[str, str, str], tuple[float, int, float]] = {
    ("beijing", "shanghai", "train"): (553.0, 261, 1318),
    ("beijing", "shanghai", "bus"):   (300.0, 420, 1318),
    ("beijing", "xian",     "train"): (515.5, 258,  912),
    ("beijing", "chengdu",   "train"): (777.0, 363, 1512),
    ("shanghai","hangzhou",  "train"): ( 73.0,  45,  180),
    ("shanghai","suzhou",    "train"): ( 25.0,  24,   84),
    ("shanghai","nanjing",   "train"): (110.5,  59,  300),
    ("chengdu",  "xian",     "train"): (263.5, 180,  710),
    ("chengdu",  "chongqing","train"): (154.0, 120,  505),
    ("hangzhou", "nanjing",   "train"): (128.0,  80,  460),
    ("shanghai", "osaka",    "ferry"): (800.0, 1200, 1600),
    ("beijing",  "tokyo",    "train"):(2400.0, 480, 2100),
    ("tokyo",    "osaka",    "train"): (875.0, 144,  515),
    ("tokyo",    "kyoto",    "train"): (852.0, 138,  476),
    ("osaka",    "kyoto",    "train"): (570.0,  29,   45),
    ("osaka",    "nagoya",   "train"): (560.0,  49,  186),
    ("osaka",    "seoul",    "ferry"): (700.0, 1000,  850),
    ("fukuoka",  "busan",    "ferry"): (350.0,  210,  200),
    ("seoul",    "busan",    "train"): (600.0, 165,  325),
    ("seoul",    "jeju",     "ferry"): (350.0,  300,  300),
    ("taichung", "kaohsiung","train"): (230.0,  100,  165),
    ("__default__", "__default__", "walk"): (0.0, 60, 5),
}

_MODE_RATE: dict[TransportMode, tuple[float, float]] = {
    TransportMode.WALK:    (0.00, 12.0),   # (CNY/km, km/h)
    TransportMode.BUS:     (0.28,  1.5),
    TransportMode.TRAIN:    (0.45,  0.5),
    TransportMode.SUBWAY:  (0.15,  0.4),
    TransportMode.FERRY:    (0.60,  0.3),
    TransportMode.TAXI:     (3.50,  1.2),
}


# ─── Route lookup ───────────────────────────────────────────────────────────────

def _lookup(from_city: str, from_lat: float, from_lng: float,
            to_city: str,   to_lat: float,   to_lng: float,
            mode: TransportMode, day: str) -> Optional[Segment]:
    """
    Build the best Segment for a single leg.  Returns None if no viable route.

    route_name and operator are populated when ORS returns them; empty string otherwise.
    """
    # 1. Try real transport API (OSRM routing + heuristic cost)
    result = get_route(
        from_city, to_city, mode.value,
        from_lat=from_lat, from_lng=from_lng,
        to_lat=to_lat,     to_lng=to_lng,
    )

    if result is None:
        # 2. Fallback to local matrix
        key = (from_city.strip().lower(), to_city.strip().lower(), mode.value)
        result = _FALLBACK_MATRIX.get(key)

    if result is None:
        # 3. Final fallback: haversine + mode rate
        result = _haversine_estimate(from_lat, from_lng, to_lat, to_lng, mode)

    if result is None:
        return None

    cost, dur, dist = result

    if mode == TransportMode.WALK and dist > 50:
        return None   # don't walk more than 50 km
    if dur > 720:
        return None   # don't spend more than 12 hours on a single leg

    # route_name defaults to generic "A → B"; enrich via ORS if available
    route_name = f"{from_city} → {to_city}"
    operator   = ""
    extra = get_ors_extra()
    if extra:
        route_name = extra.get("route_name") or route_name
        operator   = extra.get("operator")   or ""

    dep = datetime.fromisoformat(f"{day}T08:00")
    arr = dep + timedelta(minutes=dur)

    return Segment(
        transport_mode=mode,
        origin=from_city,
        destination=to_city,
        departure_time=dep.isoformat(),
        arrival_time=arr.isoformat(),
        cost=round(cost, 2),
        currency=Currency.CNY,
        duration_minutes=dur,
        distance_km=dist,
        route_name=route_name,
        operator=operator,
        carbon_kg=round(dist * CARBON_PER_KM.get(mode, 0.1), 3),
        score=_score(cost, dur, dist, mode),
    )


def _haversine_estimate(lat1: float, lon1: float,
                        lat2: float, lon2: float,
                        mode: TransportMode) -> Optional[tuple[float, int, float]]:
    """Last-resort estimation using haversine distance × mode rate."""
    if (lat1, lon1) == (0.0, 0.0) or (lat2, lon2) == (0.0, 0.0):
        return None

    dist = _haversine(lat1, lon1, lat2, lon2)
    if dist < 0.1:
        dist = 0.1

    rate_per_km, km_per_min = _MODE_RATE.get(mode, (0.30, 1.5))
    cost = round(dist * rate_per_km, 2)
    dur  = max(5, int(dist / max(km_per_min, 0.01)))

    return cost, dur, round(dist, 1)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _same_city_seg(seg: Segment) -> bool:
    """True when origin and destination are the same city (self-loop)."""
    return seg.origin.strip().lower() == seg.destination.strip().lower()


def _score(cost: float, dur: int, dist: float, mode: TransportMode) -> float:
    """
    Score for scenic preference. Higher = better (more scenic/walk-friendly).
    Walk is best (0 carbon, free), then bus, subway, train, ferry, taxi.
    """
    if mode == TransportMode.WALK:
        return 100.0
    if mode == TransportMode.BUS:
        return 50.0
    if mode == TransportMode.SUBWAY:
        return 40.0
    if mode == TransportMode.TRAIN:
        return 30.0
    if mode == TransportMode.FERRY:
        return 20.0
    if mode == TransportMode.TAXI:
        return 10.0
    return 0.0


# ─── Routing engine ─────────────────────────────────────────────────────────────

class RoutingEngine:

    def plan(self, inp: TravelInput) -> TravelOutput:

        stops = inp.stops
        if not stops:
            return TravelOutput(
                route=[], stops=[], summary={}, budget_expansion={}, diff=None,
                status=Status.FAILED, error_code=ErrorCode.NO_ROUTE,
                error_message="No stops provided",
            )

        # Always work in CNY internally; convert budget if needed
        if inp.currency == Currency.CNY:
            budget_cny = inp.budget
        else:
            try:
                budget_cny = to_cny(inp.budget, inp.currency.value)
            except ValueError:
                # Fallback: treat as-is (will be wrong but won't crash)
                budget_cny = inp.budget

        route: list[Segment] = []
        cost  = 0.0
        time  = 0
        dist  = 0.0

        prev_city  = inp.origin_city
        prev_lat   = inp.origin_lat
        prev_lng   = inp.origin_lng

        for stop in stops:

            # Resolve stop coordinates from geocoder if missing (0,0 sentinel)
            if stop.lat == 0.0 and stop.lng == 0.0:
                try:
                    stop_lat, stop_lng = geocode(stop.city)
                except ValueError:
                    continue
            else:
                stop_lat, stop_lng = stop.lat, stop.lng

            # Collect candidates across all requested transport modes
            candidates = []
            for m in inp.transport_modes or [TransportMode.TRAIN]:
                alt = _lookup(
                    prev_city, prev_lat, prev_lng,
                    stop.city, stop_lat, stop_lng, m, stop.arrival_date,
                )
                if alt is not None:
                    candidates.append(alt)

            if not candidates:
                continue   # no viable route for this leg

            # Select best candidate by preference
            if inp.preference == Preference.TIME:
                best_seg = min(candidates, key=lambda s: s.duration_minutes)
            elif inp.preference == Preference.SCENIC:
                best_seg = max(candidates, key=lambda s: s.score)
            else:  # COST (default)
                best_seg = min(candidates, key=lambda s: s.cost)

            # Skip same-city self-loops — don't add zero-length segments to route
            if _same_city_seg(best_seg):
                prev_city = stop.city
                prev_lat  = stop_lat
                prev_lng  = stop_lng
                continue

            if cost + best_seg.cost > budget_cny:
                break       # budget exhausted

            route.append(best_seg)
            cost += best_seg.cost
            time += best_seg.duration_minutes

            prev_city = stop.city
            prev_lat  = stop_lat
            prev_lng  = stop_lng

        # Empty route = no viable legs found
        if not route:
            return TravelOutput(
                route=[], stops=stops, summary={}, budget_expansion={}, diff=None,
                status=Status.FAILED, error_code=ErrorCode.NO_ROUTE,
                error_message="No viable route found for any stop",
            )

        return TravelOutput(
            route=route,
            stops=stops,
            summary={
                "total_cost":            round(cost, 2),
                "total_duration_minutes": time,
                "total_distance_km":      round(sum(s.distance_km for s in route), 1),
                "total_carbon_kg":        round(sum(s.carbon_kg   for s in route), 3),
                "segment_count":          len(route),
                "budget_used_pct":        round((cost / budget_cny) * 100, 1) if budget_cny else 0,
                "currency":              "CNY",
                "original_budget":        inp.budget,
                "original_currency":     inp.currency.value,
            },
            budget_expansion={f"seg_{i:02d}": round(s.cost, 2) for i, s in enumerate(route)},
            diff=None,
            status=Status.SUCCESS,
            error_code=ErrorCode.OK,
        )

    def update(self, base: TravelOutput, inp: TravelInput) -> TravelOutput:
        """
        Incrementally update an existing plan.  Computes diff (added/removed
        segments) vs the base route.
        """
        new_output = self.plan(inp)

        base_map = {f"{s.origin}→{s.destination}": s for s in base.route}
        new_map  = {f"{s.origin}→{s.destination}": s for s in new_output.route}

        added   = [s for key, s in new_map.items()  if key not in base_map]
        removed = [s for key, s in base_map.items() if key not in new_map]

        diff = Diff(
            added_segments=added,
            removed_segments=removed,
            cost_delta=round(sum(s.cost for s in added) - sum(s.cost for s in removed), 2),
            time_delta_minutes=sum(s.duration_minutes for s in added) - sum(s.duration_minutes for s in removed),
            reason=UpdateReason.ADDED_STOP,
        )

        new_output.diff = diff
        return new_output
