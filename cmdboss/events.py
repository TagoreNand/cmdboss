"""
Event bus abstraction.

This replaces the original hook engine, which was a single ``threading.Thread``
draining a ``queue.Queue`` (serialized, head-of-line-blocked, unbounded, lost on
crash) and — critically — was never actually wired to any handler.

Design split, by durability requirement:

  * **Durable change-audit** is written synchronously by the repository (see
    :mod:`cmdboss.audit`). It must never be dropped.
  * **Best-effort side-effects** (notifications, downstream discovery triggers,
    cache busting) flow through this :class:`EventBus`. Publishing is
    non-blocking: if the bounded queue is full we drop and increment a metric
    rather than block the request path. This is the right trade-off for the
    "don't block on network updates / auto-discovery triggers" directive.

The in-memory backend is the default; the abstract base lets a Kafka/NATS/Redis
Streams backend slot in later without touching call sites (the extensible
discovery milestone).
"""

from __future__ import annotations

import abc
import asyncio
import uuid
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from .observability import get_logger, metrics
from .utils import utcnow

logger = get_logger("cmdboss.events")

Handler = Callable[["Event"], Awaitable[None]]


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str  # e.g. "ci.created", "ci.updated", "ci.deleted", "type.created"
    entity_type: str
    entity_id: str | None = None
    actor: str = "system"
    ts: str = Field(default_factory=lambda: utcnow().isoformat())
    payload: dict = Field(default_factory=dict)


class EventBus(abc.ABC):
    @abc.abstractmethod
    async def publish(self, event: Event) -> None: ...

    @abc.abstractmethod
    def subscribe(self, handler: Handler) -> None: ...

    async def start(self) -> None:  # pragma: no cover - optional lifecycle
        return None

    async def aclose(self) -> None:  # pragma: no cover - optional lifecycle
        return None


class InMemoryEventBus(EventBus):
    """Bounded, non-blocking, single-process async event bus."""

    def __init__(self, max_queue: int = 10_000) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue)
        self._handlers: list[Handler] = []
        self._task: asyncio.Task | None = None
        self._closing = False

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: Event) -> None:
        """Enqueue without blocking. Drops + records a metric if the queue is full."""
        metrics.incr("events_published_total", {"event_type": event.type})
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            metrics.incr("events_dropped_total")
            logger.warning(
                "event_bus queue full; dropping event type=%s entity=%s",
                event.type,
                event.entity_id,
            )

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="cmdboss-event-bus")
            logger.info("event bus started (handlers=%d)", len(self._handlers))

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if self._handlers:
                    results = await asyncio.gather(
                        *(h(event) for h in self._handlers), return_exceptions=True
                    )
                    for h, res in zip(self._handlers, results, strict=False):
                        if isinstance(res, Exception):
                            logger.error(
                                "event handler %s failed for %s: %s",
                                getattr(h, "__name__", repr(h)),
                                event.type,
                                res,
                                exc_info=res,
                            )
            finally:
                self._queue.task_done()

    async def aclose(self) -> None:
        self._closing = True
        # Drain best-effort, then cancel the consumer.
        try:
            await asyncio.wait_for(self._queue.join(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("event bus drain timed out; %d events pending", self._queue.qsize())
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("event bus stopped")
