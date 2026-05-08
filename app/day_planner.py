"""
app/day_planner.py

Simplified multi-day grouping — v1 MVP only.

Adds day_number to stops (computed from dates) and provides
build_daily_itinerary() which groups route segments by calendar day.

Does NOT introduce Activity / Duration / Category models — those are v2.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

from core.shared_schemas import Segment, Stop


def compute_day_number(stop: Stop, trip_start: date) -> int:
    """
    Return the 1-based day number of a stop's arrival_date
    relative to the first day of the trip.
    """
    arr = date.fromisoformat(stop.arrival_date)
    delta = (arr - trip_start).days
    return max(1, delta + 1)   # day 1 = arrival day of the first stop


def build_daily_itinerary(
    route: List[Segment],
    stops: List[Stop],
) -> List[Dict[str, Any]]:
    """
    Group route segments by departure day.

    The first segment departs on the origin date (stop[0].arrival_date).
    Subsequent segment departures are anchored to each stop's departure_date.

    Returns a list of day dicts, each containing:
      - day_number  (int, 1-based)
      - date        (YYYY-MM-DD)
      - segments    (list of segment dicts for that day)
      - stop        (stop dict for the destination of the last segment on that day)

    Days with no segments are omitted.

    Example response structure:
      [
        {
          "day_number": 1,
          "date": "2026-06-01",
          "segments": [/* Segment.to_dict() for day 1 */],
          "stop": {/* Stop.to_dict() of the last destination reached this day */},
        },
        ...
      ]
    """
    if not route or not stops:
        return []

    # Build a lookup: destination city → stop
    stop_by_city: Dict[str, Stop] = {s.city.strip().lower(): s for s in stops}

    # Determine trip start date from the first stop's arrival
    try:
        trip_start = date.fromisoformat(stops[0].arrival_date)
    except (ValueError, IndexError):
        trip_start = date.today()

    # Group segments by their departure day (1-based)
    days: Dict[int, List[Segment]] = {}
    current_day = 1

    for seg in route:
        seg_date_str = seg.departure_time[:10]   # "2026-06-01T08:00:00" → "2026-06-01"
        try:
            seg_date = date.fromisoformat(seg_date_str)
        except ValueError:
            seg_date = trip_start

        day_num = max(1, (seg_date - trip_start).days + 1)

        if day_num not in days:
            days[day_num] = []
        days[day_num].append(seg)

    # Build the result — one entry per day that has segments
    itinerary: List[Dict[str, Any]] = []

    for day_num in sorted(days.keys()):
        day_segs = days[day_num]

        # Destination of the last segment on this day
        last_seg = day_segs[-1]
        dest_stop = stop_by_city.get(last_seg.destination.strip().lower())

        day_date = date.fromisoformat(last_seg.arrival_time[:10])
        day_date_str = day_date.isoformat()

        # If the stop has accommodation stored, include it
        accommodation = dest_stop.accommodation if dest_stop else None

        itinerary.append({
            "day_number": day_num,
            "date": day_date_str,
            "segments": [s.to_dict() for s in day_segs],
            "stop": dest_stop.to_dict() if dest_stop else None,
            "accommodation": accommodation,
        })

    return itinerary
