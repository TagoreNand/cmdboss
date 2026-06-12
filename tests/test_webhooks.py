"""Outbound webhooks: HMAC-signed delivery, event filtering, retry, and registration RBAC."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from mongomock_motor import AsyncMongoMockClient

from cmdboss.db import Database, ensure_core_indexes
from cmdboss.events import Event
from cmdboss.webhooks import WebhookRegistry, WebhookSubscriber

from ._helpers import make_settings
from .conftest import ADMIN_KEY, READER_KEY, auth

pytestmark = pytest.mark.asyncio


class FakeSender:
    def __init__(self, statuses=None):
        self.calls = []
        self._statuses = statuses or []
        self._i = 0

    async def send(self, url, body, headers):
        self.calls.append({"url": url, "body": body, "headers": headers})
        if self._statuses:
            st = self._statuses[min(self._i, len(self._statuses) - 1)]
            self._i += 1
            return st
        return 200


async def _registry():
    db = Database(AsyncMongoMockClient(), "t")
    await ensure_core_indexes(db.db)
    return WebhookRegistry(db.db)


async def test_delivery_signs_with_hmac():
    reg = await _registry()
    await reg.register("https://example.test/hook", event_types=None, secret="s3cr3t")
    sender = FakeSender()
    sub = WebhookSubscriber(reg, sender, make_settings())

    await sub.handle(Event(type="ci.created", entity_type="server", entity_id="x"))

    assert len(sender.calls) == 1
    call = sender.calls[0]
    expected = "sha256=" + hmac.new(b"s3cr3t", call["body"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-CMDBoss-Signature"] == expected
    assert call["headers"]["X-CMDBoss-Event"] == "ci.created"


async def test_event_type_filter():
    reg = await _registry()
    await reg.register("https://example.test/only-updates", event_types=["ci.updated"], secret=None)
    sender = FakeSender()
    sub = WebhookSubscriber(reg, sender, make_settings())

    await sub.handle(Event(type="ci.created", entity_type="server", entity_id="x"))
    assert sender.calls == []  # filtered out

    await sub.handle(Event(type="ci.updated", entity_type="server", entity_id="x"))
    assert len(sender.calls) == 1


async def test_retry_then_success():
    reg = await _registry()
    await reg.register("https://example.test/flaky", event_types=None, secret=None)
    sender = FakeSender(statuses=[500, 200])
    sub = WebhookSubscriber(reg, sender, make_settings(webhook_max_attempts=3))

    await sub.handle(Event(type="relationship.created", entity_type="rel:depends_on", entity_id="e1"))
    assert len(sender.calls) == 2  # retried once, then delivered


async def test_registration_requires_admin(client):
    forbidden = await client.post(
        "/api/v1/webhooks", json={"url": "https://x.test/h"}, headers=auth(READER_KEY)
    )
    assert forbidden.status_code == 403

    ok = await client.post(
        "/api/v1/webhooks",
        json={"url": "https://x.test/h", "event_types": ["ci.created"], "secret": "k"},
        headers=auth(ADMIN_KEY),
    )
    assert ok.status_code == 201
    assert ok.json()["has_secret"] is True

    listed = await client.get("/api/v1/webhooks", headers=auth(ADMIN_KEY))
    assert len(listed.json()["webhooks"]) == 1
    assert "secret" not in listed.json()["webhooks"][0]  # never returned
