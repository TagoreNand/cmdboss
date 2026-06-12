"""Optimistic-concurrency control via If-Match / revision compare-and-swap."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_KEY, SERVER_TYPE, WRITER_KEY, auth

pytestmark = pytest.mark.asyncio


async def _seed(client):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    r = await client.post(
        "/api/v1/ci/server",
        json={"hostname": "cc-01", "environment": "staging"},
        headers=auth(WRITER_KEY),
    )
    return r.json()["id"]


async def test_update_without_if_match_is_428(client):
    cid = await _seed(client)
    r = await client.patch(
        f"/api/v1/ci/server/{cid}",
        json={"environment": "production"},
        headers=auth(WRITER_KEY),
    )
    assert r.status_code == 428
    assert r.json()["error"]["code"] == "precondition_required"


async def test_update_with_stale_revision_is_409(client):
    cid = await _seed(client)
    r = await client.patch(
        f"/api/v1/ci/server/{cid}",
        json={"environment": "production"},
        headers={**auth(WRITER_KEY), "If-Match": '"99"'},
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["current_revision"] == 1


async def test_successful_update_increments_revision(client):
    cid = await _seed(client)
    r = await client.patch(
        f"/api/v1/ci/server/{cid}",
        json={"environment": "production"},
        headers={**auth(WRITER_KEY), "If-Match": '"1"'},
    )
    assert r.status_code == 200
    assert r.json()["_meta"]["revision"] == 2
    assert r.headers["ETag"] == '"2"'


async def test_lost_update_is_prevented(client):
    """Two writers both read revision 1; only the first write wins."""
    cid = await _seed(client)
    headers = {**auth(WRITER_KEY), "If-Match": '"1"'}

    first = await client.patch(
        f"/api/v1/ci/server/{cid}", json={"environment": "production"}, headers=headers
    )
    assert first.status_code == 200

    # Second writer still thinks it's revision 1 -> rejected.
    second = await client.patch(
        f"/api/v1/ci/server/{cid}", json={"environment": "development"}, headers=headers
    )
    assert second.status_code == 409


async def test_put_full_replace_requires_if_match(client):
    cid = await _seed(client)
    r = await client.put(
        f"/api/v1/ci/server/{cid}",
        json={"hostname": "cc-01", "environment": "production"},
        headers=auth(WRITER_KEY),
    )
    assert r.status_code == 428


async def test_delete_with_wrong_if_match_is_409(client):
    cid = await _seed(client)
    r = await client.request(
        "DELETE",
        f"/api/v1/ci/server/{cid}",
        headers={**auth(WRITER_KEY), "If-Match": '"5"'},
    )
    assert r.status_code == 409


async def test_bad_if_match_format_is_400(client):
    cid = await _seed(client)
    r = await client.patch(
        f"/api/v1/ci/server/{cid}",
        json={"environment": "production"},
        headers={**auth(WRITER_KEY), "If-Match": "not-an-int"},
    )
    assert r.status_code == 400
