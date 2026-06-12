"""Keyset (cursor) pagination returns every row exactly once, no skip/limit drift."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_KEY, SERVER_TYPE, WRITER_KEY, auth

pytestmark = pytest.mark.asyncio


async def _seed(client, n):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    for i in range(n):
        r = await client.post(
            "/api/v1/ci/server",
            json={"hostname": f"h{i:03d}", "environment": "production"},
            headers=auth(WRITER_KEY),
        )
        assert r.status_code == 201


async def _page_all(client, limit=2):
    ids, cursor, guard = [], None, 0
    while True:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/ci/server", params=params, headers=auth(ADMIN_KEY))
        body = r.json()
        ids += [i["id"] for i in body["items"]]
        cursor = body["paging"]["next_cursor"]
        guard += 1
        if not cursor or guard > 50:
            break
    return ids


async def test_cursor_pages_cover_all_rows_once(client):
    await _seed(client, 7)
    ids = await _page_all(client, limit=2)
    assert len(ids) == 7
    assert len(set(ids)) == 7  # no duplicates, no skips


async def test_cursor_and_offset_agree_on_membership(client):
    await _seed(client, 5)
    cursor_ids = set(await _page_all(client, limit=2))
    r = await client.get("/api/v1/ci/server?limit=100", headers=auth(ADMIN_KEY))
    offset_ids = {i["id"] for i in r.json()["items"]}
    assert cursor_ids == offset_ids


async def test_bad_cursor_is_400(client):
    await _seed(client, 1)
    r = await client.get("/api/v1/ci/server", params={"cursor": "!!!notbase64"}, headers=auth(ADMIN_KEY))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"
