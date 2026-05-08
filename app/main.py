"""
app/main.py

FastAPI entry point for 窮遊 mobile API.
Brings together:
  - Routing engine (core/routing_engine.py)
  - Transport API with ORS + OSRM fallback (core/transport_api.py)
  - Validator (app/validator.py)
  - Day planner post-processor (app/day_planner.py)
  - Phase 3 operational middleware: auth, rate-limiting, CORS, structured logging

Run:
  uvicorn app.main:app --host 0.0.0.0 --port 8000
  docker compose up
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import ALLOWED_ORIGINS, API_KEYS, AUTH_ENABLED, HOST, OPENROUTER_API_KEY, PORT
from app.middleware import (
    LoggingMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from app.validator import validate
from app.day_planner import build_daily_itinerary
from core.routing_engine import RoutingEngine
from core.llm import describe
from core.shared_schemas import TravelInput, TravelOutput

# ─── Logging ───────────────────────────────────────────────────────────────────

logger = logging.getLogger("qiongyou")

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="窮遊 API",
    description="Ultra-budget travel planner — plan your multi-city trip within budget",
    version="1.0.0",
)

# ─── Middleware stack ───────────────────────────────────────────────────────────

app.add_middleware(RequestIDMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

# ─── Deps ─────────────────────────────────────────────────────────────────────

engine = RoutingEngine()


# ─── Auth helper ───────────────────────────────────────────────────────────────

def _check_api_key(x_api_key: Optional[str]) -> None:
    """Raise HTTPException if the provided API key is not valid."""
    if not AUTH_ENABLED:
        return   # auth disabled in dev
    if x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ─── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.post("/plan")
def plan(
    inp: TravelInput,
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Plan a multi-city trip.

    Returns a ``TravelOutput`` with route segments, stops, summary,
    budget breakdown, and an optional ``diff`` if an existing plan was provided.

    The optional field ``include_itinerary`` adds a ``daily_itinerary``
    key with segments grouped by day.
    """
    _check_api_key(x_api_key)

    # Validate input
    validation = validate(inp)
    if validation.status.value != "success":
        return JSONResponse(
            status_code=400,
            content=validation.to_dict(),
        )

    # Build plan
    output = engine.plan(inp)

    # Optionally enrich with daily itinerary grouping
    include_itinerary = request.query_params.get("include_itinerary", "false").lower() == "true"
    if include_itinerary and output.status.value == "success":
        itinerary = build_daily_itinerary(output.route, output.stops)
        result = output.to_dict()
        result["daily_itinerary"] = itinerary
        return result

    return output.to_dict()


@app.post("/update")
def update(
    inp: TravelInput,
    base: TravelOutput,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Incrementally update a plan when the user adds/changes stops.
    Returns a diff (added_segments, removed_segments, cost_delta).
    """
    _check_api_key(x_api_key)

    validation = validate(inp)
    if validation.status.value != "success":
        return JSONResponse(status_code=400, content=validation.to_dict())

    output = engine.update(base, inp)
    return output.to_dict()


@app.get("/currencies")
def currencies():
    """Return supported currency codes and their CNY exchange rates."""
    from core.currency import supported_currencies, get_rate
    codes = supported_currencies()
    rates = {code: get_rate(code) for code in codes}
    return {"base": "CNY", "currencies": rates}


@app.get("/cities")
def cities():
    """Return the list of known cities with their coordinates."""
    from core.geocoder import _FALLBACK_COORDS
    return {
        city: {"lat": lat, "lng": lng}
        for city, (lat, lng) in sorted(_FALLBACK_COORDS.items())
    }


@app.post("/describe")
def describe_trip(
    output: TravelOutput,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Generate a human-readable trip description from a TravelOutput.

    Requires OPENROUTER_API_KEY to be configured on the server.
    Returns ``{"description": str, "model": str}`` on success,
    or ``{"description": None, "error": str}`` if LLM is unavailable or fails.
    """
    _check_api_key(x_api_key)

    if not OPENROUTER_API_KEY:
        return JSONResponse(
            status_code=503,
            content={"description": None, "error": "LLM not configured on server"},
        )

    return describe(output.to_dict())


# ─── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    mode = "PRODUCTION" if AUTH_ENABLED else "DEV (no auth)"
    logger.info("Starting 窮遊 API — %s", mode)
    if not AUTH_ENABLED:
        logger.warning("API key auth is DISABLED — set QIONGYOU_API_KEYS to enable")
    logger.info("Allowed CORS origins: %s", ALLOWED_ORIGINS)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
