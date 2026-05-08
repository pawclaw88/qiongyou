"""
Currency conversion for 窮遊.

Uses the free Frankfurter API (https://api.frankfurter.app) — no key required.
Caches rates in-memory for 1 hour to avoid excessive API calls.

All internal calculations are done in CNY (the budget currency).
"""

from __future__ import annotations

import time
import httpx
from functools import lru_cache
from typing import Optional
import logging

logger = logging.getLogger(__name__)

_BASE_CURRENCY = "CNY"

# ─── Hardcoded fallback rates (CNY per 1 unit) ─────────────────────────────────
# Updated periodically; used when Frankfurter API is unavailable.
# Source: approximate rates as of 2026.

_FALLBACK_RATES: dict[str, float] = {
    "CNY":  1.0000,
    "JPY":  0.0475,   # 1 CNY = ~21 JPY
    "KRW":  0.00535,   # 1 CNY = ~187 KRW
    "THB":  0.196,    # 1 CNY = ~5.1 THB
    "SGD":  0.187,    # 1 CNY = ~5.35 SGD
    "MYR":  0.158,    # 1 CNY = ~6.3 MYR
    "VND":  0.00028,  # 1 CNY = ~3570 VND
    "PHP":  0.125,    # 1 CNY = ~8 PHP
    "IDR":  0.000448, # 1 CNY = ~2230 IDR
    "TWD":  0.228,    # 1 CNY = ~4.4 TWD
    "EUR":  0.129,    # 1 CNY = ~7.75 CNY/EUR
    "GBP":  0.108,    # 1 CNY = ~9.25 CNY/GBP
    "USD":  0.137,    # 1 CNY = ~7.3 CNY/USD
    "AUD":  0.088,    # 1 CNY = ~11.4 AUD
    "CAD":  0.095,    # 1 CNY = ~10.5 CAD
    "HKD":  0.0176,  # 1 CNY = ~1.12 HKD
    "MOP":  0.0171,  # 1 CNY = ~1.28 MOP (Macau)
    "MNT":  0.000038, # 1 CNY = ~2620 MNT
    "KHR":  0.00017,  # 1 CNY = ~5850 KHR
    "LAK":  0.000071, # 1 CNY = ~14100 LAK
    "MMK":  0.000051, # 1 CNY = ~19700 MMK
    "BND":  0.099,   # 1 CNY = ~10.1 BND
    "NZD":  0.081,   # 1 CNY = ~12.3 NZD
}


# ─── Rate cache ─────────────────────────────────────────────────────────────────

class _RateCache:
    """Simple TTL cache for exchange rates."""

    def __init__(self, ttl_seconds: int = 3600):
        self._ttl    = ttl_seconds
        self._rates: Optional[dict[str, float]] = None
        self._fetched_at: float = 0.0

    def get_rates(self) -> dict[str, float]:
        """Return cached rates, fetching from API if stale or empty."""
        now = time.time()
        if self._rates is None or (now - self._fetched_at) > self._ttl:
            self._rates = self._fetch_from_api()
            self._fetched_at = now
        return self._rates

    def _fetch_from_api(self) -> dict[str, float]:
        """Fetch latest rates from Frankfurter API. Falls back to hardcoded on failure."""
        try:
            url = f"https://api.frankfurter.app/latest?from={_BASE_CURRENCY}"
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url)
                response.raise_for_status()
                data = response.json()

            rates: dict[str, float] = {"CNY": 1.0000}
            for code, rate in data.get("rates", {}).items():
                if code in _FALLBACK_RATES:
                    rates[code] = float(rate)
            logger.info("Fetched %d exchange rates from Frankfurter", len(rates))
            return rates

        except Exception as e:
            logger.warning("Failed to fetch exchange rates: %s. Using fallback.", e)
            return dict(_FALLBACK_RATES)


_rate_cache = _RateCache(ttl_seconds=3600)


# ─── Public API ─────────────────────────────────────────────────────────────────

def convert(amount: float, from_currency: str, to_currency: str) -> float:
    """
    Convert ``amount`` from ``from_currency`` to ``to_currency``.
    Both currencies are resolved to CNY first, then to the target.

    Returns the converted amount, rounded to 2 decimal places.

    >>> convert(100, "USD", "CNY")   # $100 → ¥?
    >>> convert(5000, "JPY", "CNY")  # ¥5000 → ¥?
    """
    if from_currency == to_currency:
        return round(amount, 2)

    rates = _rate_cache.get_rates()
    from_code = from_currency.upper()
    to_code   = to_currency.upper()

    from_rate = rates.get(from_code)
    to_rate   = rates.get(to_code)

    if from_rate is None:
        raise ValueError(f"Unknown currency: {from_currency!r}")
    if to_rate is None:
        raise ValueError(f"Unknown currency: {to_currency!r}")

    # Convert: amount in from_currency → CNY → to_currency
    amount_cny = amount / from_rate
    result     = amount_cny * to_rate

    return round(result, 2)


def to_cny(amount: float, from_currency: str) -> float:
    """Shortcut: convert any currency to CNY."""
    return convert(amount, from_currency, "CNY")


def from_cny(amount: float, to_currency: str) -> float:
    """Shortcut: convert CNY to any currency."""
    return convert(amount, "CNY", to_currency)


def get_rate(currency: str) -> float:
    """Return the CNY rate for a currency (1 unit = X CNY)."""
    rates = _rate_cache.get_rates()
    currency = currency.upper()
    if currency not in rates:
        raise ValueError(f"Unknown currency: {currency!r}")
    return rates[currency]


def supported_currencies() -> list[str]:
    """Return list of supported currency codes."""
    return sorted(_rate_cache.get_rates().keys())


def _force_refresh() -> None:
    """Force-clear the cache so the next call fetches fresh rates."""
    _rate_cache._rates = None
    _rate_cache._fetched_at = 0.0
