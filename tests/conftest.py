"""Shared pytest fixtures.

The suite runs entirely in-process against ``mongomock_motor`` (no live MongoDB
required) and drives the real application lifespan via ``asgi_lifespan`` so that
startup wiring, index creation, the event bus, the outbox dispatcher and the
cache invalidator are all exercised.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from cmdboss.app import create_app
from cmdboss.config import Settings
from cmdboss.db import API_KEYS_COLLECTION, Database
from cmdboss.security import ROLE_PRESETS, SCOPE_ADMIN, hash_key
from cmdboss.utils import utcnow

ADMIN_KEY = "k-admin-000000000000"
WRITER_KEY = "k-writer-00000000000"
READER_KEY = "k-reader-00000000000"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        auth_enabled=True,
        metrics_enabled=False,
        log_json=False,
        db_name="cmdboss_test",
        mongo_uri="mongomock://localhost",
        schema_cache_ttl_seconds=0.0,
        # Park the background pollers so tests stay deterministic; dedicated
        # tests drive dispatch_once()/poll_once() directly.
        outbox_poll_interval_seconds=3600.0,
        cache_invalidation_poll_seconds=3600.0,
    )


@pytest.fixture
def database() -> Database:
    return Database(AsyncMongoMockClient(), "cmdboss_test")


async def _seed_key(db, name: str, scopes, raw: str) -> None:
    await db[API_KEYS_COLLECTION].insert_one(
        {
            "name": name,
            "key_hash": hash_key(raw),
            "prefix": raw[:8],
            "scopes": list(scopes),
            "active": True,
            "created_at": utcnow(),
        }
    )


@pytest_asyncio.fixture
async def client(settings: Settings, database: Database):
    app = create_app(settings, database=database)
    async with LifespanManager(app):
        await _seed_key(database.db, "admin", [SCOPE_ADMIN], ADMIN_KEY)
        await _seed_key(database.db, "writer", ROLE_PRESETS["writer"], WRITER_KEY)
        await _seed_key(database.db, "reader", ROLE_PRESETS["reader"], READER_KEY)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def auth(key: str) -> dict:
    return {"X-API-Key": key}


SERVER_TYPE = {
    "name": "server",
    "description": "test server type",
    "fields": {
        "hostname": {"type": "string", "required": True, "min_length": 1},
        "environment": {
            "type": "string",
            "required": True,
            "enum": ["production", "staging", "development"],
        },
        "rack_unit": {"type": "integer", "required": False, "minimum": 1, "maximum": 48},
        "tags": {"type": "array", "items": "string", "required": False},
    },
    "indexes": [{"fields": ["hostname"], "unique": True}],
}
