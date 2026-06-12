"""Production hardening: outbox DLQ, key lifecycle, JWT, rate limiting, body limit, headers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import jwt as jwtlib
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from cmdboss.app import create_app
from cmdboss.db import API_KEYS_COLLECTION, OUTBOX_COLLECTION, Database, ensure_core_indexes
from cmdboss.events import Event
from cmdboss.outbox import Outbox, OutboxDispatcher
from cmdboss.ratelimit import FixedWindowRateLimiter
from cmdboss.security import SCOPE_ADMIN, JwtAuthProvider, hash_key
from cmdboss.utils import utcnow

from ._helpers import make_settings
from .conftest import ADMIN_KEY, SERVER_TYPE, auth

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _app_client(settings, seed=None):
    db = Database(AsyncMongoMockClient(), "t")
    app = create_app(settings, database=db)
    async with LifespanManager(app):
        for raw, scopes in seed or []:
            await db.db[API_KEYS_COLLECTION].insert_one(
                {"name": "k", "key_hash": hash_key(raw), "prefix": raw[:8],
                 "scopes": scopes, "active": True, "created_at": utcnow()}
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac, db


# --- outbox dead-letter --------------------------------------------------- #


class _FailBus:
    async def publish(self, event):
        raise RuntimeError("downstream boom")

    def subscribe(self, handler):
        pass


async def test_outbox_dead_letters_and_replays():
    db = Database(AsyncMongoMockClient(), "t")
    await ensure_core_indexes(db.db)
    outbox = Outbox(db.db)
    await outbox.add(Event(type="ci.created", entity_type="server", entity_id="x"))
    settings = make_settings(outbox_max_attempts=3, outbox_backoff_base_seconds=0.0,
                             outbox_backoff_cap_seconds=0.0)
    disp = OutboxDispatcher(db.db, _FailBus(), settings)

    for _ in range(3):  # fail, fail, then dead-letter
        await disp.dispatch_once()

    dead = await outbox.fetch_dead(10)
    assert len(dead) == 1 and dead[0]["attempts"] == 3
    assert await outbox.fetch_pending(10) == []  # not retried anymore

    assert await outbox.replay(dead[0]["_id"]) is True
    assert len(await outbox.fetch_pending(10)) == 1  # back in the queue


async def test_dead_letter_admin_endpoints(client, database):
    await database.db[OUTBOX_COLLECTION].insert_one(
        {"_id": "dead-1", "status": "dead", "attempts": 5, "created_at": utcnow(),
         "failed_at": utcnow(), "event": {"type": "ci.created", "entity_type": "server"},
         "last_error": "boom"}
    )
    r = await client.get("/api/v1/admin/outbox/dead", headers=auth(ADMIN_KEY))
    assert any(d["_id"] == "dead-1" for d in r.json()["dead"])
    r2 = await client.post("/api/v1/admin/outbox/dead/dead-1/replay", headers=auth(ADMIN_KEY))
    assert r2.status_code == 200
    doc = await database.db[OUTBOX_COLLECTION].find_one({"_id": "dead-1"})
    assert doc["status"] == "pending"


# --- API-key lifecycle ---------------------------------------------------- #


async def test_expired_key_rejected(client, database):
    await database.db[API_KEYS_COLLECTION].insert_one(
        {"name": "old", "key_hash": hash_key("expired-key-xyz"), "prefix": "expired-",
         "scopes": [SCOPE_ADMIN], "active": True, "created_at": utcnow(),
         "expires_at": utcnow() - timedelta(hours=1)}
    )
    r = await client.get("/api/v1/types", headers={"X-API-Key": "expired-key-xyz"})
    assert r.status_code == 401


async def test_rotate_invalidates_old_secret(client):
    r = await client.post("/api/v1/admin/api-keys", json={"name": "r", "role": "reader"}, headers=auth(ADMIN_KEY))
    kid, old = r.json()["id"], r.json()["api_key"]
    assert (await client.get("/api/v1/types", headers={"X-API-Key": old})).status_code == 200

    rot = await client.post(f"/api/v1/admin/api-keys/{kid}/rotate", headers=auth(ADMIN_KEY))
    new = rot.json()["api_key"]
    assert (await client.get("/api/v1/types", headers={"X-API-Key": old})).status_code == 401
    assert (await client.get("/api/v1/types", headers={"X-API-Key": new})).status_code == 200


async def test_revoke_key(client):
    r = await client.post("/api/v1/admin/api-keys", json={"name": "r", "role": "reader"}, headers=auth(ADMIN_KEY))
    kid, key = r.json()["id"], r.json()["api_key"]
    assert (await client.get("/api/v1/types", headers={"X-API-Key": key})).status_code == 200
    assert (await client.delete(f"/api/v1/admin/api-keys/{kid}", headers=auth(ADMIN_KEY))).status_code == 204
    assert (await client.get("/api/v1/types", headers={"X-API-Key": key})).status_code == 401


# --- JWT ------------------------------------------------------------------ #


async def test_jwt_provider_hs256():
    settings = make_settings(jwt_secret="topsecret", jwt_algorithm="HS256")
    provider = JwtAuthProvider(settings)
    token = jwtlib.encode({"sub": "svc", "scope": "ci:read ci:write"}, "topsecret", algorithm="HS256")
    p = await provider.authenticate(token)
    assert p.id == "svc" and p.has_scope("ci:write")
    assert await provider.authenticate("garbage") is None
    assert await provider.authenticate(
        jwtlib.encode({"sub": "x"}, "wrong", algorithm="HS256")
    ) is None


async def test_jwt_provider_rs256():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    settings = make_settings(jwt_algorithm="RS256", jwt_public_key=pub.decode())
    provider = JwtAuthProvider(settings)
    token = jwtlib.encode({"sub": "u", "scope": "ci:read"}, priv, algorithm="RS256")
    p = await provider.authenticate(token)
    assert p is not None and p.has_scope("ci:read")


async def test_jwt_mode_end_to_end():
    settings = make_settings(auth_mode="jwt", jwt_secret="s3", jwt_algorithm="HS256")
    async with _app_client(settings) as (ac, _db):
        token = jwtlib.encode({"sub": "svc", "scope": "types:read"}, "s3", algorithm="HS256")
        assert (await ac.get("/api/v1/types", headers={"Authorization": f"Bearer {token}"})).status_code == 200
        assert (await ac.get("/api/v1/types")).status_code == 401
        assert (await ac.get("/api/v1/types", headers={"Authorization": "Bearer bad"})).status_code == 401


# --- rate limit / body size / headers ------------------------------------- #


async def test_rate_limiter_unit():
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=60)
    assert limiter.check("k")[0] is True
    assert limiter.check("k")[0] is True
    allowed, retry = limiter.check("k")
    assert allowed is False and retry > 0
    assert limiter.check("other")[0] is True  # independent bucket


async def test_rate_limit_429():
    settings = make_settings(rate_limit_enabled=True, rate_limit_requests=2, rate_limit_window_seconds=60)
    async with _app_client(settings, seed=[(ADMIN_KEY, [SCOPE_ADMIN])]) as (ac, _db):
        h = {"X-API-Key": ADMIN_KEY}
        assert (await ac.get("/api/v1/types", headers=h)).status_code == 200
        assert (await ac.get("/api/v1/types", headers=h)).status_code == 200
        r = await ac.get("/api/v1/types", headers=h)
        assert r.status_code == 429
        assert "Retry-After" in r.headers


async def test_body_size_413():
    settings = make_settings(max_body_bytes=50)
    async with _app_client(settings, seed=[(ADMIN_KEY, [SCOPE_ADMIN])]) as (ac, _db):
        r = await ac.post("/api/v1/types", json=SERVER_TYPE, headers={"X-API-Key": ADMIN_KEY})
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "payload_too_large"


async def test_security_headers_present(client):
    r = await client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
