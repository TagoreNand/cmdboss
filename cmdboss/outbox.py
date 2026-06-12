"""
Transactional outbox for durable, atomic event emission, with a dead-letter queue.

The CI/edge write, the audit write, and an outbox record are committed together
(in a transaction when the deployment supports one). A background
:class:`OutboxDispatcher` then publishes pending rows to the in-process
:class:`EventBus`, marking each delivered.

Failure handling (the production-grade part): a delivery that raises is retried
with exponential backoff via ``next_attempt_at``; once it exceeds
``outbox_max_attempts`` it moves to a terminal ``dead`` status (a dead-letter
queue) and a metric is incremented, instead of looping forever. Dead events can
be listed and replayed by an operator.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from .config import Settings
from .db import OUTBOX_COLLECTION
from .events import Event, EventBus
from .observability import get_logger, metrics
from .utils import utcnow

logger = get_logger("cmdboss.outbox")

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_DEAD = "dead"


def _session_kw(session: Any) -> dict:
    return {"session": session} if session is not None else {}


def _aware(value: Any) -> Any:
    if isinstance(value, _dt.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


class Outbox:
    """Append-only store of events awaiting delivery, with retry + dead-letter."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._coll = db[OUTBOX_COLLECTION]

    async def add(self, event: Event, *, session: Any = None) -> None:
        now = utcnow()
        doc = {
            "_id": event.id,
            "status": STATUS_PENDING,
            "attempts": 0,
            "created_at": now,
            "next_attempt_at": now,
            "event": event.model_dump(),
        }
        await self._coll.insert_one(doc, **_session_kw(session))

    async def fetch_pending(self, limit: int) -> list[dict]:
        # Earliest-due first. Due-time is filtered by the dispatcher to stay
        # correct across MongoDB (tz-aware) and mongomock (tz-naive).
        cursor = self._coll.find({"status": STATUS_PENDING}).sort("next_attempt_at", 1).limit(limit)
        return [d async for d in cursor]

    async def mark_sent(self, outbox_id: str) -> None:
        await self._coll.update_one(
            {"_id": outbox_id}, {"$set": {"status": STATUS_SENT, "sent_at": utcnow()}}
        )

    async def reschedule(
        self, outbox_id: str, attempts: int, next_attempt_at: _dt.datetime, error: Exception
    ) -> None:
        await self._coll.update_one(
            {"_id": outbox_id},
            {"$set": {"attempts": attempts, "next_attempt_at": next_attempt_at,
                      "last_error": str(error)[:300]}},
        )

    async def mark_dead(self, outbox_id: str, attempts: int, error: Exception) -> None:
        await self._coll.update_one(
            {"_id": outbox_id},
            {"$set": {"status": STATUS_DEAD, "attempts": attempts, "failed_at": utcnow(),
                      "last_error": str(error)[:300]}},
        )
        metrics.incr("events_dropped_total")

    async def fetch_dead(self, limit: int) -> list[dict]:
        cursor = self._coll.find({"status": STATUS_DEAD}).sort("failed_at", -1).limit(limit)
        out = []
        async for d in cursor:
            for k in ("created_at", "failed_at", "next_attempt_at", "sent_at"):
                if hasattr(d.get(k), "isoformat"):
                    d[k] = d[k].isoformat()
            out.append(d)
        return out

    async def replay(self, outbox_id: str) -> bool:
        result = await self._coll.update_one(
            {"_id": outbox_id, "status": STATUS_DEAD},
            {"$set": {"status": STATUS_PENDING, "attempts": 0, "next_attempt_at": utcnow()},
             "$unset": {"last_error": ""}},
        )
        return result.modified_count > 0


class OutboxDispatcher:
    """Polls the outbox and publishes due events; retries with backoff; dead-letters."""

    def __init__(self, db: AsyncIOMotorDatabase, bus: EventBus, settings: Settings) -> None:
        self._outbox = Outbox(db)
        self._bus = bus
        self._interval = settings.outbox_poll_interval_seconds
        self._batch = settings.outbox_batch_size
        self._max_attempts = settings.outbox_max_attempts
        self._base = settings.outbox_backoff_base_seconds
        self._cap = settings.outbox_backoff_cap_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def _backoff(self, attempts: int) -> float:
        return min(self._base * (2 ** (attempts - 1)), self._cap)

    async def dispatch_once(self) -> int:
        now = utcnow()
        delivered = 0
        for doc in await self._outbox.fetch_pending(self._batch):
            nat = _aware(doc.get("next_attempt_at"))
            if nat is not None and nat > now:
                continue  # not due yet (backing off)
            try:
                await self._bus.publish(Event(**doc["event"]))
                await self._outbox.mark_sent(doc["_id"])
                delivered += 1
            except Exception as exc:  # pragma: no cover - defensive
                attempts = int(doc.get("attempts", 0)) + 1
                if attempts >= self._max_attempts:
                    await self._outbox.mark_dead(doc["_id"], attempts, exc)
                    logger.error("outbox event %s dead-lettered after %d attempts: %s",
                                 doc["_id"], attempts, exc)
                else:
                    delay = self._backoff(attempts)
                    await self._outbox.reschedule(
                        doc["_id"], attempts, now + _dt.timedelta(seconds=delay), exc
                    )
                    logger.warning("outbox event %s retry %d in %.1fs: %s",
                                   doc["_id"], attempts, delay, exc)
        return delivered

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="cmdboss-outbox-dispatcher")
            logger.info("outbox dispatcher started (interval=%ss)", self._interval)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.dispatch_once()
            except Exception as exc:  # pragma: no cover - never let the loop die
                logger.error("outbox dispatch loop error: %s", exc, exc_info=True)
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
        logger.info("outbox dispatcher stopped")
