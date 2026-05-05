"""
app/config.py

Application configuration — read from environment variables with safe defaults.
No third-party deps; plain Python.
"""

from __future__ import annotations

import os
import logging
from logging import Filter

# ─── Default request_id filter ─────────────────────────────────────────────────
# Allows logging to work outside of request context (startup, tests, etc.)

class _DefaultRequestIDFilter(Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "main"
        return True

_default_filter = _DefaultRequestIDFilter()

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("QIONGYOU_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
# Apply the default filter to the root handler so all loggers inherit it
_root = logging.getLogger()
for handler in _root.handlers:
    handler.addFilter(_default_filter)
_root.addFilter(_default_filter)

# ─── API Keys ────────────────────────────────────────────────────────────────
# Comma-separated list of valid API keys.  Leave empty to disable auth (dev only).
_API_KEYS_RAW = os.getenv("QIONGYOU_API_KEYS", "")
API_KEYS: set[str] = set(k.strip() for k in _API_KEYS_RAW.split(",") if k.strip())

# Require API key in production
AUTH_ENABLED = bool(API_KEYS)
if AUTH_ENABLED:
    logging.getLogger(__name__).info("API key auth enabled (%d key(s))", len(API_KEYS))
else:
    logging.getLogger(__name__).warning(
        "QIONGYOU_API_KEYS not set — auth DISABLED. Do NOT use in production."
    )

# ─── Rate Limiting ───────────────────────────────────────────────────────────
# Requests per minute per IP (and per API key if auth is enabled)
RATE_LIMIT_RPM = int(os.getenv("QIONGYOU_RATE_LIMIT_RPM", "60"))

# ─── CORS ────────────────────────────────────────────────────────────────────
# Allowed origins (comma-separated).  Wildcards NOT supported for credentials.
_ALLOWED_ORIGINS_RAW = os.getenv(
    "QIONGYOU_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8081"
)
ALLOWED_ORIGINS: list[str] = [
    o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()
]

# ─── OpenRouteService ────────────────────────────────────────────────────────
ORS_API_KEY: str = os.getenv("ORS_API_KEY", "").strip()
if ORS_API_KEY:
    logging.getLogger(__name__).info("OpenRouteService key configured")
else:
    logging.getLogger(__name__).warning(
        "ORS_API_KEY not set — OpenRouteService disabled, OSRM-only mode"
    )

# ─── OpenRouter (LLM for trip descriptions) ────────────────────────────────────
# Get a free API key at https://openrouter.ai/keys
# Real API keys start with "sk-or-v1-". The base64-encoded site key you may have
# seen (e.g. "eyJv...") is for client-side JS widgets only — it will not work here.
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-haiku")
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
if OPENROUTER_API_KEY:
    # Basic sanity check: real API keys are much longer than site keys
    if OPENROUTER_API_KEY.startswith("eyJ"):
        import logging as _log
        _log.getLogger(__name__).error(
            "OPENROUTER_API_KEY looks like a base64 site key, not an API key. "
            "Get a real API key from https://openrouter.ai/keys — it starts with 'sk-or-v1-'."
        )
        OPENROUTER_API_KEY = ""   # disable to avoid confusing 401 errors
    else:
        logging.getLogger(__name__).info("OpenRouter key configured — LLM description enabled")
else:
    logging.getLogger(__name__).warning("OPENROUTER_API_KEY not set — /describe endpoint disabled")

# ─── Server ──────────────────────────────────────────────────────────────────
HOST = os.getenv("QIONGYOU_HOST", "0.0.0.0")
PORT = int(os.getenv("QIONGYOU_PORT", "8000"))
