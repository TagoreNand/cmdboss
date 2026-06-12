"""
Per-principal fixed-window rate limiter.

In-process (per worker) and dependency-free — enforced in ``get_principal`` so
every authenticated request is counted. For cross-worker limiting and bounded
memory at very high cardinality, a Redis-backed limiter is the production
upgrade; the :meth:`check` contract is identical, so it is a drop-in swap.
"""

from __future__ import annotations

import time


class FixedWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: float, max_keys: int = 100_000) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, int]] = {}

    def check(self, key: str) -> tuple[bool, float]:
        """Count one hit for ``key``. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        start, count = self._buckets.get(key, (now, 0))
        if now - start >= self._window:
            start, count = now, 0
        count += 1
        self._buckets[key] = (start, count)
        if len(self._buckets) > self._max_keys:
            self._evict(now)
        if count > self._limit:
            return False, max(0.0, self._window - (now - start))
        return True, 0.0

    def _evict(self, now: float) -> None:
        stale = [k for k, (s, _) in self._buckets.items() if now - s >= self._window]
        for k in stale:
            self._buckets.pop(k, None)
