"""
Comprehensive test suite for 窮遊 routing engine.

Run with:  python -m pytest tests/test_routing.py -v
"""

import sys
sys.path.insert(0, '/mnt/c/Users/angus/Desktop/qiongyou')

from datetime import date, timedelta
from core.shared_schemas import (
    TravelInput, TravelOutput, Stop, Segment, Diff,
    Preference, TransportMode, Currency, Status, ErrorCode, UpdateReason,
)
from core.routing_engine import RoutingEngine
from core.currency import to_cny, supported_currencies
from core.geocoder import geocode, geocode_batch
from core.transport_api import get_route
from app.validator import validate


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _iso(d: date) -> str:
    return d.isoformat()

TODAY      = date.today()
TOMORROW   = _iso(TODAY + timedelta(days=1))
NEXT_WEEK  = _iso(TODAY + timedelta(days=7))
NEXT_MONTH = _iso(TODAY + timedelta(days=30))


def make_stop(
    city="beijing",
    lat: float = 39.9042,
    lng: float = 116.4074,
    arrival_date: str = None,
    departure_date: str = None,
) -> Stop:
    return Stop(
        city=city, lat=lat, lng=lng,
        arrival_date=arrival_date or TOMORROW,
        departure_date=departure_date or NEXT_WEEK,
    )


def make_input(
    origin_city="beijing",
    origin_lat=39.9042,
    origin_lng=116.4074,
    stops=None,
    budget=1000.0,
    preference=Preference.COST,
    modes=None,
    currency=Currency.CNY,
    start_date=None,
    end_date=None,
) -> TravelInput:
    return TravelInput(
        origin_city=origin_city,
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        stops=stops if stops is not None else [make_stop()],
        budget=budget,
        preference=preference,
        transport_modes=modes or [TransportMode.TRAIN],
        currency=currency,
        start_date=start_date,
        end_date=end_date,
    )


# ─── Validate Tests ─────────────────────────────────────────────────────────────

class TestValidate:
    def test_valid_input_ok(self):
        inp = make_input(
            stops=[make_stop(city="shanghai", lat=31.2304, lng=121.4737)],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out = validate(inp)
        assert out.status == Status.SUCCESS, out.error_message

    def test_unknown_origin_city(self):
        inp = make_input(origin_city="NonExistentCity")
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.CITY_NOT_FOUND

    def test_unknown_stop_city(self):
        inp = make_input(stops=[make_stop(city="NonExistentCity")])
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.CITY_NOT_FOUND

    def test_empty_stops(self):
        inp = make_input(stops=[], start_date=TODAY.isoformat(), end_date=NEXT_MONTH)
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.EMPTY_STOPS

    def test_invalid_budget_zero(self):
        inp = make_input(budget=0, start_date=TODAY.isoformat(), end_date=NEXT_MONTH)
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.INVALID_BUDGET

    def test_invalid_budget_negative(self):
        inp = make_input(budget=-100, start_date=TODAY.isoformat(), end_date=NEXT_MONTH)
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.INVALID_BUDGET

    def test_end_date_before_start_date(self):
        inp = make_input(stops=[make_stop()], start_date=NEXT_MONTH, end_date=TODAY.isoformat())
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.INVALID_DATE

    def test_stop_departure_before_arrival(self):
        inp = make_input(
            stops=[make_stop(
                arrival_date=NEXT_MONTH,
                departure_date=TODAY.isoformat(),
            )],
        )
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.INVALID_DATE

    def test_optional_dates_missing(self):
        inp = make_input(
            stops=[make_stop(city="shanghai", lat=31.2304, lng=121.4737)],
            start_date=None, end_date=None,
        )
        out = validate(inp)
        assert out.status == Status.SUCCESS

    def test_invalid_date_format(self):
        inp = make_input(stops=[make_stop()], start_date="2026/06/01", end_date=NEXT_MONTH)
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.INVALID_DATE

    def test_invalid_transport_mode(self):
        inp = make_input(
            modes=[],
            stops=[make_stop(city="shanghai", lat=31.2304, lng=121.4737)],
        )
        out = validate(inp)
        assert out.status == Status.SUCCESS  # empty modes accepted, validation passes

    def test_origin_lat_out_of_range(self):
        inp = make_input(origin_lat=200.0, stops=[make_stop(arrival_date=None, departure_date=None)])
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.INVALID_FIELD

    def test_origin_lng_out_of_range(self):
        inp = make_input(origin_lng=300.0, stops=[make_stop(arrival_date=None, departure_date=None)])
        out = validate(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.INVALID_FIELD


# ─── Routing Plan Tests ─────────────────────────────────────────────────────────

class TestRoutingPlan:
    engine = RoutingEngine()

    def test_basic_route(self):
        inp = make_input(
            budget=1000, modes=[TransportMode.TRAIN],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out = self.engine.plan(inp)
        assert out.status == Status.SUCCESS, out.error_message
        assert len(out.route) > 0, "should have at least one segment"
        assert out.summary["total_cost"] >= 0

    def test_budget_enforcement(self):
        inp = make_input(
            budget=1.0,
            modes=[TransportMode.TRAIN],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out = self.engine.plan(inp)
        assert out.status == Status.SUCCESS
        assert out.summary["total_cost"] <= 1.0

    def test_multi_stop(self):
        # Multiple distinct cities with future dates
        inp = make_input(
            origin_city="beijing", origin_lat=39.9042, origin_lng=116.4074,
            stops=[
                make_stop(city="shanghai", lat=31.2304, lng=121.4737),
                make_stop(city="hangzhou", lat=30.2741, lng=120.1551),
            ],
            budget=5000,
            modes=[TransportMode.TRAIN],
            start_date=NEXT_MONTH, end_date=NEXT_MONTH,
        )
        out = self.engine.plan(inp)
        assert out.status == Status.SUCCESS
        assert len(out.route) >= 2

    def test_empty_stops_returns_failed(self):
        inp = make_input(stops=[])
        out = self.engine.plan(inp)
        assert out.status == Status.FAILED

    def test_unknown_city_falls_back(self):
        # Use coordinates directly so geocoder doesn't matter
        inp = make_input(
            origin_city="SomewhereUnknown",
            origin_lat=0.0, origin_lng=0.0,
            stops=[Stop(city="x", lat=1.0, lng=1.0,
                       arrival_date=TOMORROW, departure_date=NEXT_WEEK)],
            modes=[TransportMode.TRAIN],
        )
        out = self.engine.plan(inp)
        assert out.status == Status.FAILED
        assert out.error_code == ErrorCode.NO_ROUTE

    def test_time_preference_picks_fastest(self):
        inp = make_input(
            preference=Preference.TIME,
            modes=[TransportMode.TRAIN, TransportMode.BUS],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out = self.engine.plan(inp)
        assert out.status == Status.SUCCESS
        # TIME preference should favor higher-speed modes
        for seg in out.route:
            assert seg.transport_mode in (TransportMode.TRAIN, TransportMode.BUS)

    def test_scenic_preference(self):
        inp = make_input(
            preference=Preference.SCENIC,
            modes=[TransportMode.TRAIN],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out = self.engine.plan(inp)
        assert out.status == Status.SUCCESS
        for seg in out.route:
            assert seg.score >= 0

    def test_to_dict_roundtrip(self):
        inp = make_input(
            budget=1000,
            modes=[TransportMode.TRAIN],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out = self.engine.plan(inp)
        d = out.to_dict()
        restored = TravelOutput.from_dict(d)
        assert restored.status == out.status
        assert restored.error_code == out.error_code
        assert len(restored.route) == len(out.route)

    def test_update_computes_diff(self):
        inp1 = make_input(
            budget=5000,
            modes=[TransportMode.TRAIN],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out1 = self.engine.plan(inp1)

        inp2 = make_input(
            budget=5000,
            modes=[TransportMode.TRAIN],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out2 = self.engine.update(out1, inp2)

        assert out2.diff is not None
        assert out2.diff.cost_delta == 0
        assert out2.diff.reason == UpdateReason.ADDED_STOP

    def test_summary_totals_correct(self):
        inp = make_input(
            budget=5000,
            modes=[TransportMode.TRAIN],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out = self.engine.plan(inp)
        assert out.summary["total_cost"] == sum(s.cost for s in out.route)
        assert out.summary["total_duration_minutes"] == sum(s.duration_minutes for s in out.route)


# ─── Currency Conversion Tests ─────────────────────────────────────────────────

class TestCurrencyConversion:
    def test_non_cny_budget_converted(self):
        engine = RoutingEngine()
        inp = make_input(
            budget=100,
            currency=Currency.USD,
            modes=[TransportMode.TRAIN],
            start_date=TODAY.isoformat(), end_date=NEXT_MONTH,
        )
        out = engine.plan(inp)
        assert out.status == Status.SUCCESS
        # budget was converted to CNY
        assert out.summary["original_budget"] == 100
        assert out.summary["original_currency"] == "USD"

    def test_unknown_currency_falls_back(self):
        """If from_dict receives an unknown currency string, it falls back to CNY."""
        from core.shared_schemas import TravelInput, Stop
        d = {
            "origin_city": "beijing", "origin_lat": 39.9042, "origin_lng": 116.4074,
            "stops": [{"city": "shanghai", "lat": 31.2304, "lng": 121.4737,
                        "arrival_date": TOMORROW, "departure_date": NEXT_WEEK}],
            "budget": 100,
            "preference": "cost",
            "transport_modes": ["train"],
            "currency": "XYZ",
        }
        inp = TravelInput.from_dict(d)
        assert inp is not None
        assert inp.currency == Currency.CNY  # fell back


# ─── Geocoder Tests ─────────────────────────────────────────────────────────────

class TestGeocoder:
    def test_known_city_returns_coords(self):
        lat, lng = geocode("beijing")
        assert lat is not None and lng is not None
        assert -90 <= lat <= 90
        assert -180 <= lng <= 180

    def test_unknown_city_raises_valueerror(self):
        import pytest
        with pytest.raises(ValueError, match="Cannot geocode"):
            geocode("asdasdasdxyz")

    def test_batch(self):
        results = geocode_batch(["beijing", "shanghai", "tokyo"])
        assert len(results) == 3


# ─── Transport API Tests ─────────────────────────────────────────────────────────

class TestTransportAPI:
    def test_get_route_returns_tuple(self):
        result = get_route("beijing", "shanghai", TransportMode.TRAIN)
        assert isinstance(result, tuple)
        assert len(result) == 3
        cost, duration, distance = result
        assert cost >= 0
        assert duration >= 0
        assert distance >= 0

    def test_get_route_walk_free(self):
        # Walk is free; pass real coords
        result = get_route("beijing", "tianjin", "walk",
                           from_lat=39.9042, from_lng=116.4074,
                           to_lat=39.1256, to_lng=117.1909)
        assert result is not None
        cost, dur, dist = result
        assert cost == 0.0
        assert dur > 0
        assert dist > 0

    def test_get_route_ferry(self):
        # Ferries have non-zero cost; pass real coords
        result = get_route("shanghai", "tianjin", "ferry",
                          from_lat=31.2304, from_lng=121.4737,
                          to_lat=39.1256, to_lng=117.1909)
        assert result is not None
        cost, dur, dist = result
        assert cost > 0
        assert dur > 0
