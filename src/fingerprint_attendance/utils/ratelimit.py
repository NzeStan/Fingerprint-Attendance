"""Tiny fixed-window rate limiter backed by the Django cache (no extra dependency)."""

from __future__ import annotations

import time

from django.core.cache import cache

_PERIODS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400,
            "day": 86400}


def parse_rate(rate: str) -> tuple[int, int]:
    """``"600/m"`` -> ``(600, 60)``."""
    try:
        num, period = rate.strip().split("/", 1)
        count = int(num)
        period = period.strip().lower()
        multiplier = 1
        digits = "".join(ch for ch in period if ch.isdigit())
        if digits:
            multiplier = int(digits)
            period = period[len(digits):]
        seconds = _PERIODS[period] * multiplier
    except (ValueError, KeyError) as exc:
        raise ValueError(f"invalid rate {rate!r}") from exc
    if count < 1:
        raise ValueError(f"invalid rate {rate!r}")
    return count, seconds


def allow(key: str, rate: str | None) -> bool:
    """Return ``True`` if the request identified by ``key`` is within ``rate``."""
    if not rate:
        return True
    count, period = parse_rate(rate)
    window = int(time.time() // period)
    cache_key = f"fpa:rl:{key}:{window}"
    added = cache.add(cache_key, 1, timeout=period + 1)
    if added:
        return True
    try:
        current = cache.incr(cache_key)
    except ValueError:  # expired between add and incr
        cache.set(cache_key, 1, timeout=period + 1)
        return True
    return int(current) <= count
