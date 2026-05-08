"""
core/llm.py

OpenRouter client for generating human-readable trip descriptions.

Given a TravelOutput (serialised as dict), calls OpenRouter's chat completions API
with a compact prompt and returns the generated narrative string.

Key design decisions:
  - Sync httpx only — no async, no additional deps.
  - Strict token budget: 300 max completion tokens to keep cost near zero on free tier.
  - Prompt is a compact single-shot with no few-shot examples.
  - The model choice (default: claude-3.5-haiku) is configurable via OPENROUTER_MODEL.
  - Graceful degradation: any failure returns {"description": None, "error": "..."}.
    The /describe endpoint never crashes the server.
"""

from __future__ import annotations

import httpx
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────

_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
_MODEL: str = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-haiku")
_BASE_URL: str = "https://openrouter.ai/api/v1"
_MAX_COMPLETION_TOKENS: int = 300
_TIMEOUT: float = 15.0

_ENABLED: bool = bool(_API_KEY)


# ─── Prompt ────────────────────────────────────────────────────────────────────

def _build_prompt(output: dict[str, Any]) -> str:
    """
    Build a compact single-shot prompt from a TravelOutput dict.
    Keeps the prompt under ~300 tokens so the full turn stays well within
    haiku's 200k context window even with the route included.
    """
    summary = output.get("summary", {})
    route = output.get("route", [])

    lines = [
        f"## Trip Summary",
        f"Total cost: ¥{summary.get('total_cost', '?')} CNY",
        f"Duration: {summary.get('total_duration_minutes', 0) // 60}h "
        f"{summary.get('total_duration_minutes', 0) % 60}m",
        f"Distance: {summary.get('total_distance_km', '?')} km",
        f"Budget used: {summary.get('budget_used_pct', '?')}%",
        "",
        "## Route",
    ]

    for i, seg in enumerate(route, 1):
        mode = seg.get("transport_mode", "?")
        dep = seg.get("departure_time", "")[:16]
        arr = seg.get("arrival_time", "")[:16]
        cost = seg.get("cost", 0)
        dist = seg.get("distance_km", "?")
        route_name = seg.get("route_name") or ""
        operator = seg.get("operator") or ""
        extra = f" ({operator})" if operator else ""
        lines.append(
            f"{i}. {seg.get('origin', '?')} → {seg.get('destination', '?')} "
            f"by {mode}「{route_name}」{extra}  "
            f"{dep}→{arr}  ¥{cost:.0f}  {dist}km"
        )

    stops = output.get("stops", [])
    if stops:
        lines.append("")
        lines.append("## Stops")
        for s in stops:
            acc = f" @ {s['accommodation']}" if s.get("accommodation") else ""
            lines.append(
                f"- {s.get('city', '?')}: "
                f"{s.get('arrival_date', '?')} → {s.get('departure_date', '?')}{acc}"
            )

    return "\n".join(lines)


# ─── Public API ───────────────────────────────────────────────────────────────

def describe(output: dict[str, Any], *, model: Optional[str] = None) -> dict[str, Any]:
    """
    Generate a one-paragraph human-readable trip description via OpenRouter.

    Returns ``{"description": str, "model": str}`` on success.
    On any failure returns ``{"description": None, "error": str}`` — never raises.
    """
    if not _ENABLED:
        return {"description": None, "error": "OPENROUTER_API_KEY not configured"}

    prompt = _build_prompt(output)

    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://qiongyou.app",
        "X-Title": "Qiongyou Travel Planner",
    }

    payload = {
        "model": model or _MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a concise travel writer for a budget travel planner app called Qiongyou. "
                    "Write a single engaging paragraph (max 200 words) describing this trip itinerary. "
                    "Highlight the route, key cities, travel modes, total cost, and any interesting operator or route name. "
                    "Use a warm, practical tone. Reply in the same language as the route names if they are in CJK characters, "
                    "otherwise reply in English."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": _MAX_COMPLETION_TOKENS,
        "temperature": 0.7,
    }

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.post(
                f"{_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices", [])
        if not choices:
            return {"description": None, "error": "No completion choices returned"}

        content: str = choices[0].get("message", {}).get("content", "")
        if not content:
            return {"description": None, "error": "Empty completion returned"}

        used_model = data.get("model", model or _MODEL)
        logger.debug("LLM describe success — model=%s tokens=%d", used_model, len(content))
        return {"description": content.strip(), "model": used_model}

    except httpx.TimeoutException:
        logger.warning("OpenRouter timeout during /describe")
        return {"description": None, "error": "LLM request timed out"}

    except httpx.HTTPStatusError as e:
        logger.warning("OpenRouter HTTP %d: %s", e.response.status_code, e.response.text[:200])
        return {"description": None, "error": f"LLM API error {e.response.status_code}"}

    except Exception as e:
        logger.warning("OpenRouter error: %s", e)
        return {"description": None, "error": str(e)}
