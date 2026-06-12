"""Idempotency-Key makes create retries safe (no duplicate CIs)."""

from __future__ import annotations

import pytest

from cmdboss.db import IDEMPOTENCY_COLLECTION
from cmdboss.utils import utcnow

from .conftest import ADMIN_KEY, SERVER_TYPE, WRITER_KEY, auth

pytestmark = pytest.mark.asyncio


async def _seed_type(client):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))


async def test_same_key_replays_and_does_not_duplicate(client):
    await _seed_type(client)
    headers = {**auth(WRITER_KEY), "Idempotency-Key": "abc-123"}
    body = {"hostname": "idem-01", "environment": "staging"}

    first = await client.post("/api/v1/ci/server", json=body, headers=headers)
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = await client.post("/api/v1/ci/server", json=body, headers=headers)
    assert second.status_code == 201
    assert second.json()["id"] == first_id  # replayed, not re-created

    listing = await client.get("/api/v1/ci/server", headers=auth(ADMIN_KEY))
    assert listing.json()["paging"]["returned"] == 1


async def test_different_key_creates_new(client):
    await _seed_type(client)
    body = {"hostname": "idem-02", "environment": "staging"}
    r1 = await client.post("/api/v1/ci/server", json=body, headers={**auth(WRITER_KEY), "Idempotency-Key": "k1"})
    body2 = {"hostname": "idem-03", "environment": "staging"}
    r2 = await client.post("/api/v1/ci/server", json=body2, headers={**auth(WRITER_KEY), "Idempotency-Key": "k2"})
    assert r1.json()["id"] != r2.json()["id"]


async def test_in_progress_claim_returns_409(client, database):
    await _seed_type(client)
    # Simulate a concurrent request that has claimed the key but not finished.
    await database.db[IDEMPOTENCY_COLLECTION].insert_one(
        {"_id": "server:inflight", "status": "pending", "created_at": utcnow()}
    )
    r = await client.post(
        "/api/v1/ci/server",
        json={"hostname": "race-01", "environment": "staging"},
        headers={**auth(WRITER_KEY), "Idempotency-Key": "inflight"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_in_progress"
