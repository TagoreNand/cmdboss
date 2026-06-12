"""Transactional outbox: events are recorded with the write and dispatched durably."""

from __future__ import annotations

import pytest

from cmdboss.outbox import OutboxDispatcher

from ._helpers import build_repo

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    def __init__(self):
        self.published = []

    async def publish(self, event):
        self.published.append(event)

    def subscribe(self, handler):
        pass


async def test_create_writes_audit_and_outbox_atomically():
    db, settings, registry, repo, outbox = await build_repo()
    created = await repo.create("server", {"hostname": "h1"}, actor="t", request_id=None)

    # The audit record and the outbox event are both present after the write.
    assert await db.db["_audit"].count_documents({}) == 1
    pending = await outbox.fetch_pending(10)
    assert len(pending) == 1
    assert pending[0]["event"]["type"] == "ci.created"
    assert pending[0]["event"]["entity_id"] == created["id"]


async def test_dispatcher_delivers_and_marks_sent():
    db, settings, registry, repo, outbox = await build_repo()
    await repo.create("server", {"hostname": "h1"}, actor="t", request_id=None)
    await repo.create("server", {"hostname": "h2"}, actor="t", request_id=None)

    bus = _RecordingBus()
    dispatcher = OutboxDispatcher(db.db, bus, settings)
    delivered = await dispatcher.dispatch_once()

    assert delivered == 2
    assert {e.type for e in bus.published} == {"ci.created"}
    # Nothing left pending; a second pass delivers nothing.
    assert await outbox.fetch_pending(10) == []
    assert await dispatcher.dispatch_once() == 0
