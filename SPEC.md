# 窮遊 API — Specification

> Ultra-budget multi-city travel planner for mobile (React Native) back-end.

## Overview

The API accepts a travel input (origin, stops, budget, preference) and returns
an optimised route with costs, durations, carbon footprint, and a daily
breakdown. Optional LLM enrichment generates a human-readable trip narrative.

**Version:** 1.0.0
**Base URL:** `http://localhost:8000` (dev) or your VPS IP in production

---

## Architecture

```
Mobile (React Native)
    │  X-API-Key / JSON
    ▼
FastAPI  ─── app/validator.py      ── input validation
    │      app/day_planner.py      ── multi-day grouping
    │      app/middleware.py       ── request ID, logging, rate-limiting
    │
    ▼
RoutingEngine  ─── core/routing_engine.py  ── greedy insertion planner
    │              core/transport_api.py    ── ORS → OSRM → heuristic fallback
    │              core/geocoder.py         ── city → lat/lng
    │              core/currency.py         ── CNY conversion
    │              core/llm.py              ── OpenRouter trip description
    │
    ▼
TravelOutput (JSON) ── consumed directly by React Native useTravelPlanner hook
```

**No database.** All state is request-scoped. Persistent storage is the
React Native client's responsibility.

---

## Enums

### Preference
| Value   | Meaning                        |
|---------|-------------------------------|
| `cost`  | Minimise total transport cost |
| `time`  | Minimise total travel time    |
| `scenic`| Prefer walking / lower carbon |

### TransportMode
`walk` | `bus` | `train` | `subway` | `taxi` | `ferry`

### Currency (21 supported)
`CNY` (base), `JPY`, `KRW`, `THB`, `SGD`, `MYR`, `VND`, `PHP`, `IDR`,
`TWD`, `EUR`, `GBP`, `USD`, `AUD`, `CAD`, `HKD`, `MOP`, `MNT`, `KHR`,
`LAK`, `MMK`, `BND`, `NZD`

### Status
`success` | `partial` | `failed`

### ErrorCode
`ok` | `no_route` | `invalid_budget` | `city_not_found` | `date_in_past` |
`empty_stops` | `invalid_city` | `invalid_date` | `invalid_preference` |
`unsupported_transport` | `unsupported_transport_mode` | `invalid_field`

---

## Data Models

### Stop
```json
{
  "city": "Shanghai",
  "lat": 31.2304,
  "lng": 121.4737,
  "arrival_date": "2026-06-01",
  "departure_date": "2026-06-02",
  "accommodation": null,
  "notes": null
}
```
> Dates are `YYYY-MM-DD`. Both are required. `departure_date` must be
> strictly after `arrival_date`.

### Segment
```json
{
  "transport_mode": "train",
  "origin": "Beijing",
  "destination": "Shanghai",
  "departure_time": "2026-06-01T08:00:00",
  "arrival_time": "2026-06-01T13:30:00",
  "cost": 553.0,
  "currency": "CNY",
  "duration_minutes": 330,
  "distance_km": 1318,
  "route_name": "G1",
  "operator": "China Railway",
  "carbon_kg": 26.36,
  "score": -553.0
}
```
> `route_name` and `operator` are populated by OpenRouteService when
> `ORS_API_KEY` is set; they are empty strings when using OSRM fallback.

### TravelOutput (response)
```json
{
  "route": [Segment, ...],
  "stops": [Stop, ...],
  "summary": {
    "total_cost": 626.0,
    "total_duration_minutes": 547,
    "total_distance_km": 1497.2,
    "total_carbon_kg": 67.692,
    "segment_count": 2,
    "budget_used_pct": 62.6,
    "currency": "CNY",
    "original_budget": 1000.0,
    "original_currency": "CNY"
  },
  "budget_expansion": { "seg_00": 553.0, "seg_01": 73.0 },
  "diff": null,
  "status": "success",
  "error_code": "ok",
  "error_message": null
}
```

### Diff (from `/update`)
```json
{
  "added_segments": [Segment, ...],
  "removed_segments": [Segment, ...],
  "cost_delta": 150.0,
  "time_delta_minutes": 45,
  "reason": "added_stop"
}
```

---

## Endpoints

### `GET /health`
Health check. No auth required.

```bash
curl http://localhost:8000/health
```
```json
{ "status": "ok", "version": "1.0.0" }
```

---

### `POST /plan`
Plan a multi-city trip.

**Auth:** `X-API-Key` header (optional in dev mode).

**Query params:**
| Param | Default | Meaning |
|-------|---------|---------|
| `include_itinerary` | `false` | If `true`, adds `daily_itinerary` key |

**Request body — `TravelInput`:**
```json
{
  "origin_city": "Beijing",
  "origin_lat": 39.9042,
  "origin_lng": 116.4074,
  "stops": [
    {
      "city": "Shanghai",
      "lat": 31.2304,
      "lng": 121.4737,
      "arrival_date": "2026-06-01",
      "departure_date": "2026-06-02"
    },
    {
      "city": "Hangzhou",
      "lat": 30.2741,
      "lng": 120.1551,
      "arrival_date": "2026-06-02",
      "departure_date": "2026-06-03"
    }
  ],
  "budget": 1000.0,
  "currency": "CNY",
  "preference": "cost",
  "transport_modes": ["walk", "bus", "train", "subway", "taxi", "ferry"],
  "start_date": "2026-06-01",
  "end_date": "2026-06-03"
}
```

**Response:** `TravelOutput` (see above). Returns HTTP 400 with the
`TravelOutput` body if validation fails.

**With daily itinerary:**
```bash
curl -X POST "http://localhost:8000/plan?include_itinerary=true" \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{...}'
```
Adds this top-level key:
```json
{
  "daily_itinerary": [
    {
      "day": 1,
      "date": "2026-06-01",
      "departure": "2026-06-01",
      "arrival": "2026-06-01",
      "segments": [Segment, ...],
      "stops": [Stop, ...],
      "total_cost": 553.0,
      "total_distance_km": 1318,
      "total_duration_minutes": 330
    }
  ]
}
```

---

### `POST /update`
Incrementally update an existing plan (e.g., user adds a new stop).
Accepts a new `TravelInput` + the existing `TravelOutput` as `base`.
Returns a `Diff` describing what changed.

**Request body:**
```json
{
  "inp": { /* TravelInput */ },
  "base": { /* TravelOutput from previous /plan call */ }
}
```

**Response:** `TravelOutput` with populated `diff` field.

---

### `GET /currencies`
Return all supported currencies and their CNY exchange rates.

```bash
curl http://localhost:8000/currencies
```
```json
{
  "base": "CNY",
  "currencies": {
    "CNY": 1.0, "USD": 0.14, "EUR": 0.13, "JPY": 21.5, ...
  }
}
```
> Rates are static fallbacks (no live API dependency).

---

### `GET /cities`
Return all known cities and their coordinates.

```bash
curl http://localhost:8000/cities
```
```json
{
  "beijing": { "lat": 39.9042, "lng": 116.4074 },
  "shanghai": { "lat": 31.2304, "lng": 121.4737 },
  ...
}
```

---

### `POST /describe`
Generate a human-readable trip narrative from a `TravelOutput`.

**Auth:** `X-API-Key` header.

**Requires:** `OPENROUTER_API_KEY` env var on the server.

**Request body:** Full `TravelOutput` dict (from `/plan` response).

**Response:**
```json
{
  "description": "Board the G1 Jinghu high-speed rail from Beijing...",
  "model": "anthropic/claude-3-5-haiku"
}
```

If LLM is unavailable:
```json
{ "description": null, "error": "LLM request failed: 503 ..." }
```
HTTP 503 when `OPENROUTER_API_KEY` is not set on the server.

---

## Error Responses

| HTTP Status | When |
|-------------|------|
| 400 | Input validation failed (body contains `TravelOutput` with details) |
| 401 | `X-API-Key` invalid or missing when `QIONGYOU_API_KEYS` is set |
| 429 | Rate limit exceeded (60 req/min per IP by default) |
| 503 | LLM not configured on server (`/describe` only) |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|---------|---------|-------------|
| `QIONGYOU_API_KEYS` | No | (empty) | Comma-separated API keys; if set, auth is enabled |
| `QIONGYOU_RATE_LIMIT_RPM` | No | `60` | Requests per minute per IP/key |
| `QIONGYOU_ALLOWED_ORIGINS` | No | `localhost:3000,localhost:8081` | CORS origins |
| `QIONGYOU_LOG_LEVEL` | No | `INFO` | Log level |
| `ORS_API_KEY` | No | (empty) | OpenRouteService key (enables route_name + operator) |
| `OPENROUTER_API_KEY` | No | (empty) | OpenRouter key (enables `/describe`) |
| `OPENROUTER_MODEL` | No | `anthropic/claude-3.5-haiku` | OpenRouter model for `/describe` |
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `8000` | Port |

---

## Deployment

### Docker (recommended)

```bash
# Build
docker compose build

# Run
docker compose up -d

# Logs
docker compose logs -f
```

The container exposes port `8000`. No database required.

### Manual

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Transport Routing Resolution

When fetching a route between two cities, the engine tries in order:

1. **OpenRouteService** (if `ORS_API_KEY` set) — provides `route_name` + `operator`
2. **OSRM public demo server** — distance + duration only
3. **Haversine heuristic** — estimated distance → cost/duration via mode matrix
4. **Mode matrix override** — hard-coded per-mode per-km rates (bus ¥0.25/km, train ¥0.35/km, etc.)
5. **Ferry flat rate** — ¥0.50/km

---

## Known Limitations

- No live transit schedules — routing is road-network based (OSRM) with mode multipliers
- `route_name` and `operator` are only populated when `ORS_API_KEY` is set
- `departure_date` for the final stop must still be provided (treated as departure from that city)
- Date-only validation: times in `arrival_date`/`departure_date` are rejected
- Carbon estimates use per-mode constants, not real vehicle data
- No persistent session or trip history on the server

---

## Project Structure

```
qiongyou/
├── app/
│   ├── main.py           FastAPI entry point
│   ├── config.py         Environment config + logging
│   ├── middleware.py     Request ID, logging, rate-limiting
│   ├── validator.py      TravelInput validation
│   └── day_planner.py    build_daily_itinerary()
├── core/
│   ├── shared_schemas.py Enums + dataclasses (Segment, Stop, TravelInput, TravelOutput, Diff)
│   ├── routing_engine.py RoutingEngine.plan() / .update()
│   ├── transport_api.py  ORS + OSRM + heuristic routing
│   ├── geocoder.py       City → coordinates
│   ├── currency.py       CNY exchange rates
│   └── llm.py            OpenRouter describe()
├── tests/
│   └── test_routing.py   31 passing tests
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── SPEC.md
```
