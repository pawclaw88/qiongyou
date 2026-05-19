"""
Routing engine for 窮遊 — Routehop greedy insertion algorithm.

Converts a fixed transport budget into the maximum number of high-value stops
along a corridor, using only the cheapest transport links.

Pipeline: validate() → RoutingEngine.plan() → TravelOutput.to_dict()

Core algorithm (Routehop):
  1. Origin O is the starting point (no events scheduled there).
  2. Destination D is always the mandatory final stop.
  3. Among all unvisited candidates, pick the one with the highest popularity.
  4. Simulate inserting it into the route (reorder by distance from O).
  5. If total_cost <= remaining_budget: permanently add it.
     Else: stop — budget exhausted.
  6. After insertion, reorder route by actual distance from O to minimize travel cost.
  7. Once all candidates are evaluated or budget is exhausted, build the schedule.

No luxury upgrades. Food/housing/events never count against budget.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta, date

logger = logging.getLogger("qiongyou")

from core.currency import convert, to_cny
from core.geocoder import city_currency, geocode, generate_candidates
from core.transport_api import aget_route, aget_routes_batch, get_ors_extra, get_route
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


# ─── Haversine ────────────────────────────────────────────────────────────────

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ─── Cost / duration matrix (fallback only) ───────────────────────────────────

_FALLBACK_MATRIX: dict[tuple[str, str, str], tuple[float, int, float]] = {
    ("beijing", "shanghai", "train"): (553.0, 261, 1318),
    ("beijing", "shanghai", "bus"):   (300.0, 420, 1318),
    ("beijing", "xian",     "train"): (515.5, 258,  912),
    ("beijing", "chengdu",  "train"): (777.0, 363, 1512),
    ("shanghai","hangzhou", "train"): ( 73.0,  45,  180),
    ("shanghai","suzhou",   "train"): ( 25.0,  24,   84),
    ("shanghai","nanjing",  "train"): (110.5,  59,  300),
    ("chengdu",  "xian",    "train"): (263.5, 180,  710),
    ("chengdu",  "chongqing","train"): (154.0, 120,  505),
    ("hangzhou", "nanjing",  "train"): (128.0,  80,  460),
    ("shanghai", "osaka",   "ferry"): (800.0, 1200, 1600),
    ("beijing",  "tokyo",   "train"):(2400.0, 480, 2100),
    ("tokyo",    "osaka",   "train"): (875.0, 144,  515),
    ("tokyo",    "kyoto",   "train"): (852.0, 138,  476),
    ("osaka",    "kyoto",   "train"): (570.0,  29,   45),
    ("osaka",    "nagoya",  "train"): (560.0,  49,  186),
    ("osaka",    "seoul",   "ferry"): (700.0, 1000,  850),
    ("fukuoka",  "busan",   "ferry"): (350.0,  210,  200),
    ("seoul",    "busan",   "train"): (600.0,  165,  325),
    ("seoul",    "jeju",    "ferry"): (350.0,  300,  300),
    ("taichung", "kaohsiung","train"): (230.0, 100,  165),
    ("__default__", "__default__", "walk"): (0.0, 60, 5),
}

_MODE_RATE: dict[TransportMode, tuple[float, float]] = {
    TransportMode.WALK:    (0.00, 12.0),   # (CNY/km, km/h)
    TransportMode.BUS:     (0.28,  1.5),
    TransportMode.TRAIN:    (0.45,  0.5),
    TransportMode.SUBWAY:  (0.15,  0.4),
    TransportMode.FERRY:   (0.60,  0.3),
    TransportMode.TAXI:    (3.50,  1.2),
}


# ─── Segment lookup ───────────────────────────────────────────────────────────

def _lookup(from_city: str, from_lat: float, from_lng: float,
            to_city: str,   to_lat: float,   to_lng: float,
            mode: TransportMode, day: str) -> Optional[Segment]:
    """
    Build the cheapest Segment for a single leg.  Returns None if no viable route.
    Routehop rule: ALWAYS pick cheapest mode (Cost preference enforced at call site).
    """
    result = get_route(
        from_city, to_city, mode.value,
        from_lat=from_lat, from_lng=from_lng,
        to_lat=to_lat,     to_lng=to_lng,
    )

    if result is None:
        key = (from_city.strip().lower(), to_city.strip().lower(), mode.value)
        result = _FALLBACK_MATRIX.get(key)

    if result is None:
        result = _haversine_estimate(from_lat, from_lng, to_lat, to_lng, mode)

    if result is None:
        return None

    cost, dur, dist = result

    if mode == TransportMode.WALK and dist > 50:
        return None
    if dur > 2000:
        return None   # reject legs beyond ~33h (handles overnight long-haul trains)

    # ── Validate: don't build segments with insane durations ──
    if dur <= 0 or dur > 2000:
        return None

    route_name = f"{from_city} → {to_city}"
    operator = ""
    try:
        extra = get_ors_extra()
        if extra:
            route_name = extra.get("route_name") or route_name
            operator   = extra.get("operator")   or ""
    except Exception:
        pass  # ORS extra lookup is best-effort; don't fail the whole segment

    # Parse departure time from the `day` param (ISO date string, e.g. "2026-05-17")
    try:
        dep_date = date.fromisoformat(day)
        dep = datetime(dep_date.year, dep_date.month, dep_date.day, 8, 0)
    except (ValueError, TypeError):
        dep = datetime(2000, 1, 1, 8, 0)  # fallback
    arr = dep + timedelta(minutes=dur)
    local_ccy = Currency(city_currency(to_city))

    return Segment(
        transport_mode=mode,
        origin=from_city,
        destination=to_city,
        departure_time=dep.isoformat(),
        arrival_time=arr.isoformat(),
        cost=round(cost, 2),
        currency=Currency.CNY,
        local_currency=local_ccy,
        duration_minutes=dur,
        distance_km=dist,
        route_name=route_name,
        operator=operator,
        carbon_kg=round(dist * CARBON_PER_KM.get(mode, 0.1), 3),
        score=0.0,
    )


def _haversine_estimate(lat1: float, lon1: float,
                        lat2: float, lon2: float,
                        mode: TransportMode) -> Optional[tuple[float, int, float]]:
    if (lat1, lon1) == (0.0, 0.0) or (lat2, lon2) == (0.0, 0.0):
        return None
    dist = _haversine(lat1, lon1, lat2, lon2)
    if dist < 0.1:
        dist = 0.1
    rate_per_km, km_per_min = _MODE_RATE.get(mode, (0.30, 1.5))
    cost = round(dist * rate_per_km, 2)
    dur  = max(5, int(dist / max(km_per_min, 0.01)))
    return cost, dur, round(dist, 1)


def _cheapest_segment(from_city: str, from_lat: float, from_lng: float,
                      to_city: str,   to_lat: float,   to_lng: float,
                      modes: list[TransportMode], day: str) -> Optional[Segment]:
    """Return the cheapest segment across all requested transport modes."""
    candidates = []
    for m in modes or [TransportMode.TRAIN]:
        seg = _lookup(from_city, from_lat, from_lng, to_city, to_lat, to_lng, m, day)
        if seg is not None:
            candidates.append(seg)
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.cost)


def _geo_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Wrapper for sorting stops by straight-line distance from origin."""
    return _haversine(lat1, lon1, lat2, lon2)


def reorder_by_distance(stops_to_sort: list, origin_lat: float, origin_lng: float, coord_map: dict) -> list:
    """Sort stops by straight-line distance from origin (geographic sort)."""
    return sorted(
        stops_to_sort,
        key=lambda s: _geo_distance(
            origin_lat, origin_lng,
            coord_map.get(s.city, (0.0, 0.0))[0],
            coord_map.get(s.city, (0.0, 0.0))[1],
        ),
    )


async def _acheapest_segment(
    from_city: str, from_lat: float, from_lng: float,
    to_city: str,   to_lat: float,   to_lng: float,
    modes: list[TransportMode], day: str,
) -> Optional[Segment]:
    """Async version: return cheapest segment across all modes (concurrent lookup)."""
    tasks = [
        aget_route(from_city, to_city, m.value,
                   from_lat=from_lat, from_lng=from_lng,
                   to_lat=to_lat,     to_lng=to_lng)
        for m in modes or [TransportMode.TRAIN]
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    best = None
    best_cost = float("inf")
    for m, result in zip(modes or [TransportMode.TRAIN], results):
        if isinstance(result, Exception):
            continue
        cost_dur_dist = result
        if cost_dur_dist is None:
            continue
        cost, dur, dist = cost_dur_dist
        if cost < best_cost:
            best_cost = cost
            best = m, cost, dur, dist

    if best is None:
        return None

    m, cost, dur, dist = best

    route_name = f"{from_city} → {to_city}"
    operator = ""
    try:
        extra = get_ors_extra()
        if extra:
            route_name = extra.get("route_name") or route_name
            operator   = extra.get("operator")   or ""
    except Exception:
        pass

    try:
        dep_date = date.fromisoformat(day)
        dep = datetime(dep_date.year, dep_date.month, dep_date.day, 8, 0)
    except (ValueError, TypeError):
        dep = datetime(2000, 1, 1, 8, 0)
    arr = dep + timedelta(minutes=dur)
    local_ccy = Currency(city_currency(to_city))

    return Segment(
        transport_mode=m,
        origin=from_city,
        destination=to_city,
        departure_time=dep.isoformat(),
        arrival_time=arr.isoformat(),
        cost=round(cost, 2),
        currency=Currency.CNY,
        local_currency=local_ccy,
        duration_minutes=dur,
        distance_km=dist,
        route_name=route_name,
        operator=operator,
        carbon_kg=round(dist * CARBON_PER_KM.get(m, 0.1), 3),
        score=0.0,
    )


# ─── Routehop greedy algorithm ───────────────────────────────────────────────

class RoutingEngine:

    async def plan(self, inp: TravelInput) -> TravelOutput:
        """
        Routehop greedy insertion algorithm.

        Steps:
          1. Separate candidates from the mandatory destination stop D.
          2. Sort candidates by popularity descending.
          3. Greedily insert the next-highest-popularity city into the route
             if it fits within the remaining budget.
          4. After each insertion, reorder route by straight-line distance from O.
          5. When budget is exhausted, stop — no upgrades, no better class.
          6. Compute schedule (arrive/depart times) from departure_time, allowances,
             travel durations, and leeway.
          7. If end_date is provided, validate that arrival at D <= end_date.
        """
        stops = inp.stops
        if not stops:
            return TravelOutput(
                route=[], stops=[], summary={}, budget_expansion={}, diff=None,
                status=Status.FAILED, error_code=ErrorCode.NO_ROUTE,
                error_message="No stops provided",
            )

        # ── 1. Separate mandatory destination from candidate pool ────────────────
        destination_stop = None
        candidates: list[Stop] = []

        for s in stops:
            if getattr(s, "is_destination", False) or s == stops[-1]:
                # Last stop in the list is treated as D if not explicitly flagged
                if destination_stop is None:
                    destination_stop = s
            else:
                candidates.append(s)

        # Fallback: use the last stop as destination if none flagged
        if destination_stop is None and stops:
            destination_stop = stops[-1]

        if destination_stop is None:
            return TravelOutput(
                route=[], stops=[], summary={}, budget_expansion={}, diff=None,
                status=Status.FAILED, error_code=ErrorCode.NO_ROUTE,
                error_message="No destination stop found",
            )

        # ── 1b. Auto-generate candidates if none provided ──────────────────────
        # Resolve origin and destination coordinates inline (needed before generate_candidates)
        _origin_lat = inp.origin_lat
        _origin_lng = inp.origin_lng
        if _origin_lat == 0.0 and _origin_lng == 0.0:
            try:
                _origin_lat, _origin_lng = geocode(inp.origin_city)
            except ValueError:
                _origin_lat, _origin_lng = 0.0, 0.0

        _dest_lat = destination_stop.lat
        _dest_lng = destination_stop.lng
        if _dest_lat == 0.0 and _dest_lng == 0.0:
            try:
                _dest_lat, _dest_lng = geocode(destination_stop.city)
            except ValueError:
                _dest_lat, _dest_lng = 0.0, 0.0

        if not candidates:
            auto_candidates_raw = generate_candidates(
                origin_city=inp.origin_city,
                origin_lat=_origin_lat,
                origin_lng=_origin_lng,
                dest_city=destination_stop.city,
                dest_lat=_dest_lat,
                dest_lng=_dest_lng,
                max_distance_km=400.0,
            )
            candidates = []
            for c in auto_candidates_raw:
                candidates.append(Stop(
                    city=c["city"],
                    lat=c["lat"],
                    lng=c["lng"],
                    popularity=c["popularity"],
                    time_allowance_hours=getattr(destination_stop, "time_allowance_hours", 6.0),
                    leeway_hours=getattr(destination_stop, "leeway_hours", 1.0),
                    locked=False,
                    is_origin=False,
                    is_destination=False,
                ))

        # ── 2. Budget in CNY ────────────────────────────────────────────────────
        if inp.currency == Currency.CNY:
            budget_cny = inp.budget
        else:
            try:
                budget_cny = to_cny(inp.budget, inp.currency.value)
            except ValueError:
                budget_cny = inp.budget

        # ── 3. Sort candidates by popularity descending ───────────────────────
        def pop_key(s: Stop) -> float:
            p = getattr(s, "popularity", None)
            return p if p is not None else 0.0

        candidates_sorted = sorted(candidates, key=pop_key, reverse=True)

        # ── 4. Resolve coordinates for origin ──────────────────────────────────
        origin_lat = inp.origin_lat
        origin_lng = inp.origin_lng
        if origin_lat == 0.0 and origin_lng == 0.0:
            try:
                origin_lat, origin_lng = geocode(inp.origin_city)
            except ValueError:
                return TravelOutput(
                    route=[], stops=[], summary={}, budget_expansion={}, diff=None,
                    status=Status.FAILED, error_code=ErrorCode.CITY_NOT_FOUND,
                    error_message=f"Origin city not found: {inp.origin_city}",
                )

        # Resolve destination coordinates
        dest_lat = destination_stop.lat
        dest_lng = destination_stop.lng
        if dest_lat == 0.0 and dest_lng == 0.0:
            try:
                dest_lat, dest_lng = geocode(destination_stop.city)
            except ValueError:
                return TravelOutput(
                    route=[], stops=[], summary={}, budget_expansion={}, diff=None,
                    status=Status.FAILED, error_code=ErrorCode.CITY_NOT_FOUND,
                    error_message=f"Destination city not found: {destination_stop.city}",
                )

        # Build coordinate map for all stops
        coord_map: dict[str, tuple[float, float]] = {
            inp.origin_city: (origin_lat, origin_lng),
            destination_stop.city: (dest_lat, dest_lng),
        }
        for c in candidates:
            if c.lat != 0.0 or c.lng != 0.0:
                coord_map[c.city] = (c.lat, c.lng)
            else:
                try:
                    lat, lng = geocode(c.city)
                    coord_map[c.city] = (lat, lng)
                except ValueError:
                    pass  # skip cities that can't be geocoded

        modes = inp.transport_modes or [TransportMode.TRAIN]
        day = inp.start_date or datetime.today().isoformat()

        # ── 5. Separate locked vs auto-candidate stops ──────────────────────────
        locked_stops = [c for c in candidates if getattr(c, "locked", False)]
        auto_candidates = [c for c in candidates if not getattr(c, "locked", False)]
        auto_candidates_sorted = sorted(auto_candidates, key=lambda s: getattr(s, "popularity", 0) or 0, reverse=True)

        # ── 6. Batch pre-fetch all route costs before greedy insertion ─────────────
        #
        # Build the complete directed graph of all legs we might need:
        #   O→each candidate, each candidate→D, candidate→candidate pairs.
        # Fire ALL of them concurrently in one asyncio.gather call.
        # Then the greedy loop reads from the pre-computed cache — zero network I/O
        # during evaluation.
        #
        # For N candidates this is O(N²) legs but all run in parallel, so the wall
        # clock time is the same as O(N) sequential calls (~2-5 s instead of 40-80 s).

        _all_stops_for_batch = auto_candidates_sorted  # only non-locked for pre-fetch

        _batch_legs: list[tuple[str, str, str, tuple[float, float] | None, tuple[float, float] | None]] = []

        # O → each candidate
        for c in _all_stops_for_batch:
            cc = coord_map.get(c.city)
            if cc:
                for m in modes:
                    _batch_legs.append((inp.origin_city, c.city, m.value, (origin_lat, origin_lng), cc))

        # each candidate → D
        for c in _all_stops_for_batch:
            cc = coord_map.get(c.city)
            if cc:
                for m in modes:
                    _batch_legs.append((c.city, destination_stop.city, m.value, cc, (_dest_lat, _dest_lng)))

        # candidate → candidate pairs (for future reordering)
        for i, c_i in enumerate(_all_stops_for_batch):
            cc_i = coord_map.get(c_i.city)
            if not cc_i:
                continue
            for c_j in _all_stops_for_batch[i + 1:]:
                cc_j = coord_map.get(c_j.city)
                if not cc_j:
                    continue
                for m in modes:
                    _batch_legs.append((c_i.city, c_j.city, m.value, cc_i, cc_j))
                    _batch_legs.append((c_j.city, c_i.city, m.value, cc_j, cc_i))

        # Pre-fetch all at once — this is the ONE expensive I/O operation.
        # Use asyncio.wait_for as a safety net: if all 364 legs don't complete
        # in 60 s, abandon and fall back to on-demand (no network = fast enough
        # for the few legs the greedy loop will actually try).
        logger.info("Pre-fetching %d route legs (semaphore-capped, 60s timeout)", len(_batch_legs))
        _raw_results: dict[int, tuple[float, int, float] | None] = {}
        if _batch_legs:
            try:
                _raw_results = await asyncio.wait_for(
                    aget_routes_batch(_batch_legs),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Route pre-fetch timed out after 60 s — falling back to on-demand")
            except Exception as e:
                logger.warning("Route pre-fetch error: %s — falling back to on-demand", e)

        # Build a cache: (from_city_lower, to_city_lower, mode) → (cost, dur, dist)
        _route_cache: dict[tuple[str, str, str], tuple[float, int, float]] = {}
        for i, (from_c, to_c, mode, _, __) in enumerate(_batch_legs):
            result = _raw_results.get(i)
            if result is not None:
                cost, dur, dist = result
                _route_cache[(from_c.strip().lower(), to_c.strip().lower(), mode)] = (cost, dur, dist)

        logger.info("Route cache populated: %d entries", len(_route_cache))

        # ── 7. Greedy insertion (now reads from cache — no I/O) ─────────────────

        async def aroute_cost_from_cache(
            ordered_stops: list[Stop],
        ) -> tuple[float, list[Segment]]:
            """Read pre-computed route costs from cache. Zero network calls."""
            if not ordered_stops:
                return 0.0, []
            segs = []
            cost = 0.0
            prev_city = inp.origin_city
            prev_lat, prev_lng = origin_lat, origin_lng
            for stop in ordered_stops:
                stop_coords = coord_map.get(stop.city)
                if stop_coords is None:
                    continue
                slat, slng = stop_coords

                # Try each mode, pick cheapest from cache
                best = None
                best_cost = float("inf")
                for m in modes:
                    key = (prev_city.strip().lower(), stop.city.strip().lower(), m.value)
                    raw = _route_cache.get(key)
                    if raw is not None:
                        c_cost, c_dur, c_dist = raw
                        if c_cost < best_cost:
                            best_cost = c_cost
                            best = m, c_cost, c_dur, c_dist

                if best is None:
                    # Cache miss: fall back to async on-demand lookup.
                    # This still runs ORS+OSRM in parallel per leg (not batch-prefetched
                    # but not sequential either). Avoids returning an invalid route.
                    seg = await _acheapest_segment(prev_city, prev_lat, prev_lng,
                                                    stop.city, slat, slng, modes, day)
                    if seg is None:
                        return float("inf"), []
                    segs.append(seg)
                    cost += seg.cost
                    prev_city = stop.city
                    prev_lat, prev_lng = slat, slng
                    continue

                m, c_cost, c_dur, c_dist = best

                route_name = f"{prev_city} → {stop.city}"
                operator = ""
                try:
                    extra = get_ors_extra()
                    if extra:
                        route_name = extra.get("route_name") or route_name
                        operator = extra.get("operator") or ""
                except Exception:
                    pass

                try:
                    dep_date = date.fromisoformat(day)
                    dep = datetime(dep_date.year, dep_date.month, dep_date.day, 8, 0)
                except (ValueError, TypeError):
                    dep = datetime(2000, 1, 1, 8, 0)
                arr = dep + timedelta(minutes=c_dur)
                local_ccy = Currency(city_currency(stop.city))

                seg = Segment(
                    transport_mode=m,
                    origin=prev_city,
                    destination=stop.city,
                    departure_time=dep.isoformat(),
                    arrival_time=arr.isoformat(),
                    cost=round(c_cost, 2),
                    currency=Currency.CNY,
                    local_currency=local_ccy,
                    duration_minutes=c_dur,
                    distance_km=c_dist,
                    route_name=route_name,
                    operator=operator,
                    carbon_kg=round(c_dist * CARBON_PER_KM.get(m, 0.1), 3),
                    score=0.0,
                )
                segs.append(seg)
                cost += round(c_cost, 2)
                prev_city = stop.city
                prev_lat, prev_lng = slat, slng
            return cost, segs

        # Pre-populate selected with locked stops (always included, user manually added)
        selected: list[Stop] = reorder_by_distance(locked_stops, origin_lat, origin_lng, coord_map)

        # For locked stops, use _acheapest_segment directly (user-added, few in number)
        async def _locked_cost(stops: list[Stop]) -> tuple[float, list[Segment]]:
            if not stops:
                return 0.0, []
            segs = []
            cost = 0.0
            prev_city = inp.origin_city
            prev_lat, prev_lng = origin_lat, origin_lng
            for stop in stops:
                cc = coord_map.get(stop.city)
                if cc is None:
                    continue
                slat, slng = cc
                seg = await _acheapest_segment(prev_city, prev_lat, prev_lng,
                                                stop.city, slat, slng, modes, day)
                if seg is None:
                    return float("inf"), []
                segs.append(seg)
                cost += seg.cost
                prev_city = stop.city
                prev_lat, prev_lng = slat, slng
            return cost, segs

        # Verify that locked stops fit within budget (O → locked → D)
        locked_route_cost, _ = await _locked_cost(selected + [destination_stop])
        if locked_route_cost > budget_cny:
            # Locked stops alone exceed budget — fail immediately
            return TravelOutput(
                route=[],
                stops=stops,
                summary={},
                budget_expansion={},
                diff=None,
                status=Status.FAILED,
                error_code=ErrorCode.INVALID_BUDGET,
                error_message=(
                    f"Locked stops cost ${locked_route_cost:.2f} but budget is ${budget_cny:.2f}. "
                    f"Unlock or remove a stop, or increase budget."
                ),
            )

        remaining_budget = budget_cny - locked_route_cost

        # Greedily add auto-candidates by popularity until budget exhausted
        for candidate in auto_candidates_sorted:
            # Simulate inserting this candidate
            trial = reorder_by_distance(selected + [candidate], origin_lat, origin_lng, coord_map)
            trial_dest_last = trial + [destination_stop]

            cost_with, _ = await aroute_cost_from_cache(trial_dest_last)
            if cost_with <= budget_cny:
                # Accept the candidate
                selected.append(candidate)
                selected = reorder_by_distance(selected, origin_lat, origin_lng, coord_map)
                remaining_budget = budget_cny - cost_with
            # Else: reject, move to next candidate

        # Final route: O → selected stops (reordered) → D
        final_route_stops = selected + [destination_stop]

        # Build the actual segments
        final_route_cost, final_segments = await aroute_cost_from_cache(final_route_stops)

        # Early guard: even the direct O→D route exceeds budget
        if final_route_cost > budget_cny and not selected:
            return TravelOutput(
                route=final_segments,
                stops=stops,
                summary={
                    "total_cost": round(final_route_cost, 2),
                    "budget_remaining": round(budget_cny - final_route_cost, 2),
                    "budget_used_pct": round((final_route_cost / budget_cny) * 100, 1),
                    "stops_selected": 0,
                    "stops_total_candidates": len(candidates),
                    "candidates_rejected_budget": len(candidates),
                    "error": "Budget too low for even the direct origin→destination route.",
                },
                budget_expansion={f"seg_{i:02d}": round(s.cost, 2) for i, s in enumerate(final_segments)},
                diff=None,
                status=Status.FAILED,
                error_code=ErrorCode.INVALID_BUDGET,
                error_message=(
                    f"Budget ${budget_cny:.2f} is below the minimum "
                    f"transport cost of ${final_route_cost:.2f} for "
                    f"{inp.origin_city} → {destination_stop.city}. "
                    f"Increase budget to at least ${final_route_cost:.2f} or "
                    f"use a different transport mode."
                ),
            )
        total_cost = sum(s.cost for s in final_segments)
        remaining_budget = budget_cny - total_cost  # always recalculate

        if not final_segments:
            return TravelOutput(
                route=[], stops=stops, summary={}, budget_expansion={}, diff=None,
                status=Status.FAILED, error_code=ErrorCode.NO_ROUTE,
                error_message="No viable route found for any stop",
            )

        # ── 6. Compute schedule ──────────────────────────────────────────────────
        departure_time_str = inp.departure_time or "09:00"
        dep_h, dep_m = map(int, departure_time_str.split(":"))

        if inp.start_date:
            try:
                trip_start = date.fromisoformat(inp.start_date)
            except ValueError:
                trip_start = date.today()
        else:
            trip_start = date.today()

        default_allowance = inp.default_time_allowance_hours
        default_leeway = inp.default_leeway_hours

        schedule: list[dict] = []
        current_datetime = datetime(
            trip_start.year, trip_start.month, trip_start.day,
            dep_h, dep_m,
        )
        current_city = inp.origin_city
        current_lat, current_lng = origin_lat, origin_lng

        # Departure from origin
        schedule.append({
            "city": inp.origin_city,
            "type": "departure",
            "datetime": current_datetime.isoformat(),
            "action": f"Depart {inp.origin_city}",
        })

        seg_idx = 0
        for stop in selected + [destination_stop]:
            stop_coords = coord_map.get(stop.city)
            if stop_coords is None:
                continue
            slat, slng = stop_coords

            # Travel to this stop
            if seg_idx < len(final_segments):
                travel_seg = final_segments[seg_idx]
                # Advance time by travel duration
                current_datetime += timedelta(minutes=travel_seg.duration_minutes)
                seg_idx += 1

            # Arrive at stop
            arrival_dt = current_datetime
            schedule.append({
                "city": stop.city,
                "type": "arrival",
                "datetime": arrival_dt.isoformat(),
                "action": f"Arrive {stop.city}",
            })

            # Time allowance at stop
            allowance = getattr(stop, "time_allowance_hours", None) or default_allowance
            leeway    = getattr(stop, "leeway_hours", None)        or default_leeway

            if stop == destination_stop and inp.end_date:
                # Destination: allocate remaining time up to end_date
                try:
                    end_str = inp.end_date.split("T")[0]  # handle YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS
                    trip_end = date.fromisoformat(end_str)
                    end_dt = datetime(trip_end.year, trip_end.month, trip_end.day, 23, 59)
                    remaining = (end_dt - current_datetime).total_seconds() / 3600
                    if remaining > 0:
                        allowance = min(allowance, remaining)
                except (ValueError, IndexError):
                    pass

            current_datetime += timedelta(hours=allowance)
            schedule.append({
                "city": stop.city,
                "type": "departure",
                "datetime": current_datetime.isoformat(),
                "action": f"Depart {stop.city} (spent {allowance}h)",
            })

            # Leeway before next leg
            if stop != destination_stop:
                current_datetime += timedelta(hours=leeway)

            current_city = stop.city
            current_lat, current_lng = slat, slng

        # ── 7. Schedule validation ───────────────────────────────────────────────
        schedule_warning = None
        if inp.end_date:
            try:
                end_dt = datetime.fromisoformat(inp.end_date)
                if end_dt.hour == 0 and end_dt.minute == 0:
                    end_dt = end_dt.replace(hour=23, minute=59)
                if current_datetime > end_dt:
                    schedule_warning = (
                        f"Schedule exceeds end_date by "
                        f"{(current_datetime - end_dt).total_seconds() / 3600:.1f}h. "
                        f"Reduce stops, extend end_date, or reduce time allowances."
                    )
            except ValueError:
                pass

        # ── 8. Local currency summary ────────────────────────────────────────────
        local_cost_by_currency: dict[str, float] = {}
        for seg in final_segments:
            local_ccy_val = seg.local_currency.value
            local_cost = convert(seg.cost, "CNY", local_ccy_val)
            local_cost_by_currency[local_ccy_val] = \
                local_cost_by_currency.get(local_ccy_val, 0.0) + local_cost

        if local_cost_by_currency:
            primary_local_ccy = max(local_cost_by_currency, key=local_cost_by_currency.get)
            total_cost_local = round(local_cost_by_currency[primary_local_ccy], 2)
        else:
            primary_local_ccy = "CNY"
            total_cost_local = round(total_cost, 2)

        total_time_minutes = sum(s.duration_minutes for s in final_segments)

        result = TravelOutput(
            route=final_segments,
            stops=stops,
            summary={
                "total_cost":            round(total_cost, 2),
                "total_cost_local":      total_cost_local,
                "local_currency":        primary_local_ccy,
                "total_duration_minutes": total_time_minutes,
                "total_distance_km":     round(sum(s.distance_km for s in final_segments), 1),
                "total_carbon_kg":       round(sum(s.carbon_kg   for s in final_segments), 3),
                "segment_count":         len(final_segments),
                "budget_used_pct":       round((total_cost / budget_cny) * 100, 1) if budget_cny else 0,
                "budget_remaining":      round(remaining_budget, 2),
                "currency":              "CNY",
                "original_budget":       inp.budget,
                "original_currency":     inp.currency.value,
                "stops_selected":       len(selected),
                "stops_total_candidates": len(candidates),
                "candidates_rejected_budget": len(candidates) - len(selected),
            },
            budget_expansion={f"seg_{i:02d}": round(s.cost, 2) for i, s in enumerate(final_segments)},
            diff=None,
            status=Status.SUCCESS,
            error_code=ErrorCode.OK,
        )

        # Attach schedule + warning
        result.schedule = schedule
        result.schedule_warning = schedule_warning
        return result

    # ── Schedule / itinerary helpers (called after plan()) ────────────────────

    async def update(self, base: TravelOutput, inp: TravelInput) -> TravelOutput:
        """
        Incrementally update an existing plan.  Computes diff (added/removed
        segments) vs the base route.
        """
        new_output = await self.plan(inp)

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