"""
core/shared_schemas.py

Shared domain models for 窮遊 routing engine.
Uses lightweight dataclasses + enums for FastAPI compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any, Union
# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _safe_enum(cls: type[Enum], value: Any, default: Enum) -> Enum:
    if isinstance(value, cls):
        return value
    if isinstance(value, str):
        try:
            return cls(value)
        except ValueError:
            return default
    return default


# ─────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────

class Preference(str, Enum):
    COST = "cost"
    TIME = "time"
    SCENIC = "scenic"


class TransportMode(str, Enum):
    WALK = "walk"
    BUS = "bus"
    TRAIN = "train"
    SUBWAY = "subway"
    TAXI = "taxi"
    FERRY = "ferry"


class Currency(str, Enum):
    CNY  = "CNY"   # Chinese Yuan (base currency)
    JPY  = "JPY"   # Japanese Yen
    KRW  = "KRW"   # South Korean Won
    THB  = "THB"   # Thai Baht
    SGD  = "SGD"   # Singapore Dollar
    MYR  = "MYR"   # Malaysian Ringgit
    VND  = "VND"   # Vietnamese Dong
    PHP  = "PHP"   # Philippine Peso
    IDR  = "IDR"   # Indonesian Rupiah
    TWD  = "TWD"   # Taiwan Dollar
    EUR  = "EUR"   # Euro
    GBP  = "GBP"   # British Pound
    USD  = "USD"   # US Dollar
    AUD  = "AUD"   # Australian Dollar
    CAD  = "CAD"   # Canadian Dollar
    HKD  = "HKD"   # Hong Kong Dollar
    MOP  = "MOP"   # Macau Pataca
    MNT  = "MNT"   # Mongolian Tugrik
    KHR  = "KHR"   # Cambodian Riel
    LAK  = "LAK"   # Lao Kip
    MMK  = "MMK"   # Myanmar Kyat
    BND  = "BND"   # Brunei Dollar
    NZD  = "NZD"   # New Zealand Dollar


class Status(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ErrorCode(str, Enum):
    OK = "ok"
    NO_ROUTE = "no_route"
    INVALID_BUDGET = "invalid_budget"
    CITY_NOT_FOUND = "city_not_found"
    DATE_IN_PAST = "date_in_past"
    EMPTY_STOPS = "empty_stops"
    INVALID_CITY = "invalid_city"
    INVALID_DATE = "invalid_date"
    INVALID_PREFERENCE = "invalid_preference"
    UNSUPPORTED_TRANSPORT = "unsupported_transport"
    UNSUPPORTED_TRANSPORT_MODE = "unsupported_transport_mode"
    INVALID_FIELD = "invalid_field"


class UpdateReason(str, Enum):
    ADDED_STOP = "added_stop"


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

CARBON_PER_KM: Dict[TransportMode, float] = {
    TransportMode.WALK: 0.0,
    TransportMode.BUS: 0.05,
    TransportMode.TRAIN: 0.02,
    TransportMode.SUBWAY: 0.01,
    TransportMode.TAXI: 0.2,
    TransportMode.FERRY: 0.15,
}


# ─────────────────────────────────────────────────────────────
# Core models
# ─────────────────────────────────────────────────────────────

@dataclass
class Stop:
    city: str
    lat: float
    lng: float
    arrival_date: str
    departure_date: str
    accommodation: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "city": self.city,
            "lat": self.lat,
            "lng": self.lng,
            "arrival_date": self.arrival_date,
            "departure_date": self.departure_date,
            "accommodation": self.accommodation,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Stop":
        return cls(
            city=d["city"],
            lat=d["lat"],
            lng=d["lng"],
            arrival_date=d["arrival_date"],
            departure_date=d["departure_date"],
            accommodation=d.get("accommodation"),
            notes=d.get("notes"),
        )


@dataclass
class TravelInput:
    origin_city: str
    origin_lat: float
    origin_lng: float
    stops: List[Stop]
    budget: float
    preference: Preference
    transport_modes: List[TransportMode]

    currency: Currency = Currency.CNY
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "origin_city": self.origin_city,
            "origin_lat": self.origin_lat,
            "origin_lng": self.origin_lng,
            "stops": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.stops],
            "budget": self.budget,
            "preference": self.preference.value,
            "transport_modes": [m.value for m in self.transport_modes],
            "currency": self.currency.value,
            "start_date": self.start_date,
            "end_date": self.end_date,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TravelInput":
        return cls(
            origin_city=d["origin_city"],
            origin_lat=d["origin_lat"],
            origin_lng=d["origin_lng"],
            stops=[Stop.from_dict(s) if isinstance(s, dict) else s for s in d["stops"]],
            budget=d["budget"],
            preference=_safe_enum(Preference, d.get("preference"), Preference.COST),
            transport_modes=[_safe_enum(TransportMode, m, TransportMode.TRAIN) for m in d["transport_modes"]],
            currency=_safe_enum(Currency, d.get("currency", "CNY"), Currency.CNY),
            start_date=d.get("start_date"),
            end_date=d.get("end_date"),
        )


@dataclass
class Segment:
    transport_mode: TransportMode
    origin: str
    destination: str
    departure_time: str
    arrival_time: str
    cost: float
    currency: Currency
    duration_minutes: int
    distance_km: float
    route_name: str       # e.g. "Bus 123", "JR Central Tokaido Line" — empty when OSRM-only
    operator: str         # e.g. "Tokyo Metro", "China Railway" — empty when OSRM-only
    carbon_kg: float
    score: float

    def to_dict(self) -> dict:
        mode_val = self.transport_mode.value if hasattr(self.transport_mode, "value") else str(self.transport_mode)
        return {
            "transport_mode": mode_val,
            "origin": self.origin,
            "destination": self.destination,
            "departure_time": self.departure_time,
            "arrival_time": self.arrival_time,
            "cost": self.cost,
            "currency": self.currency.value if hasattr(self.currency, "value") else str(self.currency),
            "duration_minutes": self.duration_minutes,
            "distance_km": self.distance_km,
            "route_name": self.route_name,
            "operator": self.operator,
            "carbon_kg": self.carbon_kg,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(
            transport_mode=_safe_enum(TransportMode, d.get("transport_mode"), TransportMode.TRAIN),
            origin=d["origin"],
            destination=d["destination"],
            departure_time=d["departure_time"],
            arrival_time=d["arrival_time"],
            cost=d["cost"],
            currency=_safe_enum(Currency, d.get("currency", "CNY"), Currency.CNY),
            duration_minutes=d["duration_minutes"],
            distance_km=d["distance_km"],
            route_name=d.get("route_name", ""),
            operator=d.get("operator", ""),
            carbon_kg=d["carbon_kg"],
            score=d["score"],
        )


@dataclass
class Diff:
    added_segments: List[Segment]
    removed_segments: List[Segment]
    cost_delta: float
    time_delta_minutes: int
    reason: UpdateReason

    def to_dict(self) -> dict:
        return {
            "added_segments": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.added_segments],
            "removed_segments": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.removed_segments],
            "cost_delta": self.cost_delta,
            "time_delta_minutes": self.time_delta_minutes,
            "reason": self.reason.value if hasattr(self.reason, "value") else self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Diff":
        return cls(
            added_segments=[Segment.from_dict(s) if isinstance(s, dict) else s for s in d.get("added_segments", [])],
            removed_segments=[Segment.from_dict(s) if isinstance(s, dict) else s for s in d.get("removed_segments", [])],
            cost_delta=d["cost_delta"],
            time_delta_minutes=d["time_delta_minutes"],
            reason=_safe_enum(UpdateReason, d.get("reason"), UpdateReason.ADDED_STOP),
        )


@dataclass
class TravelOutput:
    route: List[Segment]
    stops: List[Stop]
    summary: Dict[str, Any]
    budget_expansion: Dict[str, float]
    diff: Optional[Diff]
    status: Status
    error_code: ErrorCode
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "route": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.route],
            "stops": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.stops],
            "summary": self.summary,
            "budget_expansion": self.budget_expansion,
            "diff": self.diff.to_dict() if self.diff and hasattr(self.diff, "to_dict") else self.diff,
            "status": self.status.value,
            "error_code": self.error_code.value,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TravelOutput":
        diff = d.get("diff")
        return cls(
            route=[Segment.from_dict(s) if isinstance(s, dict) else s for s in d.get("route", [])],
            stops=[Stop.from_dict(s) if isinstance(s, dict) else s for s in d.get("stops", [])],
            summary=d.get("summary", {}),
            budget_expansion=d.get("budget_expansion", {}),
            diff=Diff.from_dict(diff) if isinstance(diff, dict) else diff,
            status=_safe_enum(Status, d.get("status"), Status.FAILED),
            error_code=_safe_enum(ErrorCode, d.get("error_code"), ErrorCode.OK),
            error_message=d.get("error_message"),
        )
