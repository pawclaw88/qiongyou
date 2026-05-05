"""Input validation for 窮遊 TravelInput."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from core.shared_schemas import (
    Currency,
    ErrorCode,
    Preference,
    Status,
    Stop,
    TransportMode,
    TravelInput,
    TravelOutput,
)

# ─── Helpers ───────────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Derive KNOWN_CITIES from geocoder's fallback table — single source of truth.
from core.geocoder import _FALLBACK_COORDS as _GEO_COORDS
KNOWN_CITIES: set[str] = set(_GEO_COORDS.keys())

COORD_RANGE = (-90, 90)   # lat
LON_RANGE   = (-180, 180)  # lng


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    if not _DATE_RE.match(s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _city_known(city: str) -> bool:
    return city.strip().lower() in KNOWN_CITIES


# ─── Core validator ─────────────────────────────────────────────────────────────

def validate(inp: TravelInput) -> TravelOutput:
    """
    Validates every field of ``inp``.
    Returns ``TravelOutput`` with ``status=SUCCESS`` and ``error_code=OK``
    on success; otherwise ``status=FAILED`` and a specific ``error_code``.
    """
    errors: list[str] = []

    # ── origin ────────────────────────────────────────────────────────────────
    if not inp.origin_city or not inp.origin_city.strip():
        errors.append("origin_city is required")

    if not _city_known(inp.origin_city):
        return _fail(ErrorCode.CITY_NOT_FOUND, f"Unknown origin city: {inp.origin_city}")

    if not _in_range(inp.origin_lat, COORD_RANGE, "origin_lat"):
        return _fail(ErrorCode.INVALID_FIELD,
                        f"origin_lat {inp.origin_lat} out of range [-90, 90]")
    if not _in_range(inp.origin_lng, LON_RANGE, "origin_lng"):
        return _fail(ErrorCode.INVALID_FIELD,
                        f"origin_lng {inp.origin_lng} out of range [-180, 180]")

    # ── dates ─────────────────────────────────────────────────────────────────
    start = _parse_date(inp.start_date)
    end   = _parse_date(inp.end_date)
    if start is None and inp.start_date is not None:
        return _fail(ErrorCode.INVALID_DATE, f"Invalid start_date format: {inp.start_date}")
    if end is None and inp.end_date is not None:
        return _fail(ErrorCode.INVALID_DATE, f"Invalid end_date format: {inp.end_date}")
    if start is not None and end is not None and end <= start:
        return _fail(ErrorCode.INVALID_DATE, "end_date must be after start_date")
    if start is not None and start < date.today():
        return _fail(ErrorCode.DATE_IN_PAST, f"start_date {inp.start_date} is in the past")

    # ── budget ────────────────────────────────────────────────────────────────
    if inp.budget <= 0:
        return _fail(ErrorCode.INVALID_BUDGET, f"budget must be positive, got {inp.budget}")

    # ── stops ─────────────────────────────────────────────────────────────────
    if not inp.stops or len(inp.stops) == 0:
        return _fail(ErrorCode.EMPTY_STOPS, "At least one stop is required")

    for i, stop in enumerate(inp.stops):
        err = _validate_stop(stop, i, start, end)
        if err:
            return err

    # ── same-city guard: stop city must differ from origin city ───────────────
    origin_key = inp.origin_city.strip().lower()
    seen_cities = {origin_key}
    for i, stop in enumerate(inp.stops):
        city_key = stop.city.strip().lower()
        if city_key == origin_key:
            return _fail(ErrorCode.INVALID_CITY,
                         f"stop[{i}] city '{stop.city}' cannot be the same as origin '{inp.origin_city}'")
        if city_key in seen_cities:
            return _fail(ErrorCode.INVALID_CITY,
                         f"stop[{i}] city '{stop.city}' is a duplicate — each city may appear only once")
        seen_cities.add(city_key)

    # ── preference ────────────────────────────────────────────────────────────
    if not isinstance(inp.preference, Preference):
        return _fail(ErrorCode.INVALID_PREFERENCE,
                     f"Unknown preference: {inp.preference}")

    # ── transport_modes ───────────────────────────────────────────────────────
    for mode in inp.transport_modes:
        if not isinstance(mode, TransportMode):
            return _fail(ErrorCode.UNSUPPORTED_TRANSPORT,
                         f"Unsupported transport mode: {mode}")

    # ── currency ──────────────────────────────────────────────────────────────
    if not isinstance(inp.currency, Currency):
        return _fail(ErrorCode.INVALID_BUDGET,
                     f"Unsupported currency: {inp.currency}")

    if errors:
        # structural validation errors that don't map to a specific code
        return TravelOutput(
            route=[], stops=[], summary={}, budget_expansion={}, diff=None,
            status=Status.FAILED,
            error_code=ErrorCode.INVALID_DATE,
            error_message="; ".join(errors),
        )

    return TravelOutput(
        route=[], stops=[], summary={}, budget_expansion={}, diff=None,
        status=Status.SUCCESS, error_code=ErrorCode.OK,
    )


def _validate_stop(stop: Stop, idx: int, trip_start: Optional[date] = None, trip_end: Optional[date] = None) -> Optional[TravelOutput]:
    prefix = f"stop[{idx}]"
    if not stop.city or not stop.city.strip():
        return _fail(ErrorCode.INVALID_CITY, f"{prefix}: city is required")
    if not _city_known(stop.city):
        return _fail(ErrorCode.CITY_NOT_FOUND, f"{prefix}: Unknown city: {stop.city}")
    if not _in_range(stop.lat, COORD_RANGE, f"{prefix}.lat"):
        return _fail(ErrorCode.INVALID_CITY, f"{prefix}.lat out of range")
    if not _in_range(stop.lng, LON_RANGE, f"{prefix}.lng"):
        return _fail(ErrorCode.INVALID_CITY, f"{prefix}.lng out of range")
    arr = _parse_date(stop.arrival_date)
    dep = _parse_date(stop.departure_date)
    if arr is None:
        return _fail(ErrorCode.INVALID_DATE, f"{prefix}: invalid arrival_date")
    if dep is None:
        return _fail(ErrorCode.INVALID_DATE, f"{prefix}: invalid departure_date")
    if dep <= arr:
        return _fail(ErrorCode.INVALID_DATE,
                     f"{prefix}: departure_date must be after arrival_date")
    if trip_start and arr < trip_start:
        return _fail(ErrorCode.INVALID_DATE, f"{prefix}: arrival before trip start")
    if trip_end and dep > trip_end:
        return _fail(ErrorCode.INVALID_DATE, f"{prefix}: departure after trip end")
    return None


def _in_range(value: float, rng: tuple[float, float], field: str) -> bool:
    return rng[0] <= value <= rng[1]


def _fail(code: ErrorCode, message: str) -> TravelOutput:
    return TravelOutput(
        route=[], stops=[], summary={}, budget_expansion={}, diff=None,
        status=Status.FAILED, error_code=code, error_message=message,
    )
