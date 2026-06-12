"""Payload and schema validation behaviour at the API boundary."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_KEY, SERVER_TYPE, WRITER_KEY, auth

pytestmark = pytest.mark.asyncio


async def _seed_type(client):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))


async def test_invalid_enum_payload_is_422(client):
    await _seed_type(client)
    r = await client.post(
        "/api/v1/ci/server",
        json={"hostname": "h", "environment": "not-valid"},
        headers=auth(WRITER_KEY),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


async def test_missing_required_field_is_422(client):
    await _seed_type(client)
    r = await client.post(
        "/api/v1/ci/server", json={"environment": "staging"}, headers=auth(WRITER_KEY)
    )
    assert r.status_code == 422


async def test_extra_field_is_422(client):
    await _seed_type(client)
    r = await client.post(
        "/api/v1/ci/server",
        json={"hostname": "h", "environment": "staging", "rogue": 1},
        headers=auth(WRITER_KEY),
    )
    assert r.status_code == 422


async def test_invalid_type_definition_is_422(client):
    bad = {
        "name": "broken",
        "fields": {"tags": {"type": "array"}},  # array without items
    }
    r = await client.post("/api/v1/types", json=bad, headers=auth(ADMIN_KEY))
    assert r.status_code == 422


async def test_non_object_body_is_rejected(client):
    await _seed_type(client)
    r = await client.post(
        "/api/v1/ci/server", json=["not", "an", "object"], headers=auth(WRITER_KEY)
    )
    assert r.status_code in (400, 422)


async def test_error_envelope_has_request_id(client):
    r = await client.get("/api/v1/types", headers=auth("bad-key"))
    body = r.json()
    assert "error" in body
    assert "request_id" in body["error"]
    assert r.headers.get("X-Request-ID")
