"""Authentication and RBAC enforcement."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_KEY, READER_KEY, SERVER_TYPE, WRITER_KEY, auth

pytestmark = pytest.mark.asyncio


async def test_missing_key_is_401(client):
    r = await client.get("/api/v1/types")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_invalid_key_is_401(client):
    r = await client.get("/api/v1/types", headers=auth("totally-wrong"))
    assert r.status_code == 401


async def test_reader_cannot_write_type(client):
    r = await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(READER_KEY))
    assert r.status_code == 403
    body = r.json()["error"]
    assert body["code"] == "forbidden"
    assert "types:write" in body["details"]["required"]


async def test_reader_cannot_write_ci(client):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    r = await client.post(
        "/api/v1/ci/server",
        json={"hostname": "x", "environment": "staging"},
        headers=auth(READER_KEY),
    )
    assert r.status_code == 403


async def test_reader_can_read(client):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    r = await client.get("/api/v1/ci/server", headers=auth(READER_KEY))
    assert r.status_code == 200


async def test_writer_cannot_mint_keys(client):
    r = await client.post(
        "/api/v1/admin/api-keys",
        json={"name": "x", "role": "reader"},
        headers=auth(WRITER_KEY),
    )
    assert r.status_code == 403


async def test_admin_can_mint_and_use_key(client):
    r = await client.post(
        "/api/v1/admin/api-keys",
        json={"name": "ci-bot", "role": "writer"},
        headers=auth(ADMIN_KEY),
    )
    assert r.status_code == 201
    new_key = r.json()["api_key"]
    # The freshly minted key works and carries writer scope.
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    r = await client.post(
        "/api/v1/ci/server",
        json={"hostname": "bot-made", "environment": "staging"},
        headers=auth(new_key),
    )
    assert r.status_code == 201


async def test_auth_disabled_allows_anonymous(settings, database):
    """When auth is disabled the API is open with a synthetic admin principal."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from cmdboss.app import create_app

    settings.auth_enabled = False
    app = create_app(settings, database=database)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/api/v1/types")
            assert r.status_code == 200
