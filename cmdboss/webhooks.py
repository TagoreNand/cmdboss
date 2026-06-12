"""
Outbound webhooks.

A durable subscriber on the event bus fans CI / relationship / discovery events
out to externally registered HTTP endpoints. Because it sits *downstream of the
transactional outbox*, a webhook only fires once the originating write has
committed and the event has been durably dispatched to the bus.

Each delivery is signed with an HMAC-SHA256 over the body
(``X-CMDBoss-Signature: sha256=<hex>``) so receivers can verify authenticity.
The HTTP sender is injectable, so delivery is fully testable without a network.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from .config import Settings
from .db import WEBHOOKS_COLLECTION
from .events import Event
from .observability import get_logger, metrics
from .utils import utcnow

logger = get_logger("cmdboss.webhooks")


class HttpSender:
    """Default sender backed by httpx. Returns the HTTP status code."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout

    async def send(self, url: str, body: bytes, headers: dict) -> int:  # pragma: no cover - network
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, content=body, headers=headers)
            return resp.status_code


class WebhookRegistry:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._coll = db[WEBHOOKS_COLLECTION]

    async def register(self, url: str, event_types: list[str] | None, secret: str | None) -> dict:
        doc = {
            "url": url,
            "event_types": event_types or [],  # empty = all events
            "secret": secret,
            "active": True,
            "created_at": utcnow(),
        }
        result = await self._coll.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        out = {k: v for k, v in doc.items() if k not in ("_id", "secret")}
        out["has_secret"] = bool(secret)
        return out

    async def list(self) -> list[dict]:
        out = []
        async for d in self._coll.find({}, {"secret": 0}):
            d["id"] = str(d.pop("_id"))
            if hasattr(d.get("created_at"), "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            out.append(d)
        return out

    async def active_for(self, event_type: str) -> list[dict]:
        hooks = []
        async for d in self._coll.find({"active": True}):
            allowed = d.get("event_types") or []
            if not allowed or event_type in allowed:
                hooks.append(d)
        return hooks


class WebhookSubscriber:
    """Event-bus handler that delivers events to registered webhooks."""

    def __init__(self, registry: WebhookRegistry, sender: Any, settings: Settings) -> None:
        self._registry = registry
        self._sender = sender
        self._max_attempts = settings.webhook_max_attempts

    async def handle(self, event: Event) -> None:
        hooks = await self._registry.active_for(event.type)
        if not hooks:
            return
        body = json.dumps(
            {
                "id": event.id,
                "type": event.type,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "actor": event.actor,
                "ts": event.ts,
                "payload": event.payload,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        for hook in hooks:
            await self._deliver(hook, body, event.type)

    async def _deliver(self, hook: dict, body: bytes, event_type: str) -> None:
        headers = {"Content-Type": "application/json", "X-CMDBoss-Event": event_type}
        secret = hook.get("secret")
        if secret:
            sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-CMDBoss-Signature"] = f"sha256={sig}"
        url = hook["url"]
        for attempt in range(self._max_attempts):
            try:
                status = await self._sender.send(url, body, headers)
                if 200 <= status < 300:
                    metrics.incr("events_published_total", {"event_type": "webhook.delivered"})
                    return
                raise RuntimeError(f"non-2xx status {status}")
            except Exception as exc:
                if attempt + 1 >= self._max_attempts:
                    metrics.incr("events_dropped_total")
                    logger.warning("webhook delivery failed url=%s event=%s: %s", url, event_type, exc)
                    return
                await asyncio.sleep(min(0.1 * (2**attempt), 2.0))
