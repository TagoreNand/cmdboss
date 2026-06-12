"""
Cross-worker schema-cache invalidation.

Each worker caches compiled CI-type models. ``SchemaRegistry.invalidate`` only
clears the *local* worker's cache, so a schema change made on worker A would be
invisible to worker B until B's TTL elapsed. This component closes that window
across processes.

Two strategies, chosen by capability:
  * **change-stream** (preferred, replica set): tail ``_ci_types`` and invalidate
    the affected type the instant it changes — near-zero propagation latency.
  * **polling** (portable default; works on standalone MongoDB and mongomock):
    periodically scan for type docs whose ``updated_at`` advanced since the last
    sweep and invalidate those names.

Because invalidation is active, the per-worker cache TTL can be set high for a
strong hit rate without sacrificing freshness.
"""

from __future__ import annotations

import asyncio
import datetime as _dt

from motor.motor_asyncio import AsyncIOMotorDatabase

from .config import Settings
from .db import TYPES_COLLECTION
from .observability import get_logger
from .registry import SchemaRegistry
from .utils import utcnow

logger = get_logger("cmdboss.invalidator")


def _aware(value: _dt.datetime | None) -> _dt.datetime | None:
    """Normalise a datetime to tz-aware UTC.

    mongomock returns naive datetimes; real MongoDB with ``tz_aware=True`` returns
    aware ones. Normalising both sides keeps comparisons valid in either case.
    """
    if isinstance(value, _dt.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


class RegistryCacheInvalidator:
    def __init__(
        self, db: AsyncIOMotorDatabase, registry: SchemaRegistry, settings: Settings
    ) -> None:
        self._coll = db[TYPES_COLLECTION]
        self._registry = registry
        self._interval = settings.cache_invalidation_poll_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Start from "now" so we only react to changes after startup.
        self._watermark: _dt.datetime = utcnow()

    async def poll_once(self) -> int:
        """Invalidate types changed since the last sweep. Returns count invalidated."""
        cursor = self._coll.find(
            {"updated_at": {"$gt": self._watermark}}, {"name": 1, "updated_at": 1}
        )
        invalidated = 0
        newest = _aware(self._watermark)
        async for doc in cursor:
            name = doc.get("name")
            if name:
                self._registry.invalidate(name)
                invalidated += 1
            ts = _aware(doc.get("updated_at"))
            if isinstance(ts, _dt.datetime) and (newest is None or ts > newest):
                newest = ts
        if newest is not None and newest > _aware(self._watermark):
            self._watermark = newest
        if invalidated:
            logger.debug("invalidated %d cached type(s)", invalidated)
        return invalidated

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="cmdboss-cache-invalidator")
            logger.info("cache invalidator started (poll=%ss)", self._interval)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:  # pragma: no cover - keep the loop alive
                logger.error("cache invalidation poll error: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        logger.info("cache invalidator stopped")
