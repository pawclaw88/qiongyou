"""
app/middleware.py

Starlette middleware used by app/main.py:
  - Request ID middleware (injects request_id into logging context)
  - Structured logging middleware (logs every request with level, duration, status)
  - Simple in-memory rate limiter (sliding window per IP / X-API-Key)
"""

from __future__ import annotations

import time
import uuid
import logging
from collections import defaultdict
from threading import Lock
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("qiongyou")

# ─── Rate limiter ─────────────────────────────────────────────────────────────

from app.config import RATE_LIMIT_RPM

_rate_limit_window = 60.0   # seconds
_rate_hits: dict[str, list[float]] = defaultdict(list)
_rate_lock = Lock()


def rate_limit_key(request: Request) -> str:
    """Key used for rate limiting: X-API-Key header if present, else client IP."""
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        return f"key:{api_key}"
    # Fall back to client IP (X-Forwarded-For respected by Starlette)
    return f"ip:{request.client.host if request.client else 'unknown'}"


def is_rate_limited(request: Request, limit: int = RATE_LIMIT_RPM) -> bool:
    """Returns True if the request should be rejected."""
    key = rate_limit_key(request)
    now = time.time()
    window_start = now - _rate_limit_window

    with _rate_lock:
        # Prune old entries
        _rate_hits[key] = [t for t in _rate_hits[key] if t > window_start]

        if len(_rate_hits[key]) >= limit:
            return True

        _rate_hits[key].append(now)
        return False


# ─── Request ID middleware ────────────────────────────────────────────────────

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Injects a unique request_id into the logging context for every request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        # Bind request_id to the current logging context via the `request_id` extra
        # so %(request_id)s works in our format string.
        def _log(level: int, msg: str, *args, **kwargs):
            logger.log(level, msg, *args, **kwargs, extra={"request_id": request_id})

        request.state.log = _log

        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


# ─── Structured logging middleware ────────────────────────────────────────────

class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every request: method, path, status, duration_ms, client.
    Skips /health to keep logs clean.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path == "/health":
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            status = response.status_code if "response" in dir() else 500

            log_level = logging.INFO if status < 400 else logging.WARNING
            logger.log(
                log_level,
                "%s %s %s %sms",
                request.method,
                request.url.path,
                status,
                duration_ms,
                extra={"request_id": getattr(request.state, "request_id", "?")},
            )

        return response


# ─── Rate limit middleware ─────────────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rejects requests that exceed the per-IP / per-API-key rate limit."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        from starlette.responses import JSONResponse
        from app.config import RATE_LIMIT_RPM

        if is_rate_limited(request, RATE_LIMIT_RPM):
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limit_exceeded", "message": f"Max {RATE_LIMIT_RPM} req/min"},
                headers={"Retry-After": "60"},
            )
        return await call_next(request)
