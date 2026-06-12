"""End-to-end CRUD + pagination + audit lineage over the HTTP surface."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_KEY, SERVER_TYPE, WRITER_KEY, auth

pytestmark = pytest.mark.asyncio


async def _register_type(client):
    r = await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    assert r.status_code == 201, r.text
    return r.json()


async def test_register_type_and_full_crud(client):
    type_doc = await _register_type(client)
    assert type_doc["name"] == "server"
    assert type_doc["json_schema"]["additionalProperties"] is False

    # create
    payload = {"hostname": "web-01", "environment": "production", "rack_unit": 10}
    r = await client.post("/api/v1/ci/server", json=payload, headers=auth(WRITER_KEY))
    assert r.status_code == 201, r.text
    created = r.json()
    cid = created["id"]
    assert created["_meta"]["revision"] == 1
    assert r.headers["ETag"] == '"1"'

    # read
    r = await client.get(f"/api/v1/ci/server/{cid}", headers=auth(WRITER_KEY))
    assert r.status_code == 200
    assert r.json()["hostname"] == "web-01"

    # list
    r = await client.get("/api/v1/ci/server", headers=auth(WRITER_KEY))
    body = r.json()
    assert body["paging"]["returned"] == 1
    assert body["items"][0]["id"] == cid

    # delete
    r = await client.delete(f"/api/v1/ci/server/{cid}", headers=auth(WRITER_KEY))
    assert r.status_code == 204
    r = await client.get(f"/api/v1/ci/server/{cid}", headers=auth(WRITER_KEY))
    assert r.status_code == 404


async def test_pagination_and_filtering(client):
    await _register_type(client)
    for i in range(7):
        env = "production" if i % 2 == 0 else "staging"
        await client.post(
            "/api/v1/ci/server",
            json={"hostname": f"h{i}", "environment": env},
            headers=auth(WRITER_KEY),
        )

    # page size cap
    r = await client.get("/api/v1/ci/server?limit=3", headers=auth(ADMIN_KEY))
    body = r.json()
    assert body["paging"]["returned"] == 3
    assert body["paging"]["next_offset"] == 3

    # filter by enum field + total
    r = await client.get(
        "/api/v1/ci/server?environment=production&include_total=true",
        headers=auth(ADMIN_KEY),
    )
    body = r.json()
    assert all(i["environment"] == "production" for i in body["items"])
    assert body["paging"]["total"] == 4  # i = 0,2,4,6


async def test_unknown_filter_field_is_rejected(client):
    await _register_type(client)
    r = await client.get("/api/v1/ci/server?nope=1", headers=auth(ADMIN_KEY))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


async def test_audit_history_records_lineage(client):
    await _register_type(client)
    r = await client.post(
        "/api/v1/ci/server",
        json={"hostname": "audit-01", "environment": "staging"},
        headers=auth(WRITER_KEY),
    )
    cid = r.json()["id"]
    await client.patch(
        f"/api/v1/ci/server/{cid}",
        json={"environment": "production"},
        headers={**auth(WRITER_KEY), "If-Match": '"1"'},
    )
    r = await client.get(f"/api/v1/ci/server/{cid}/audit", headers=auth(ADMIN_KEY))
    history = r.json()["history"]
    actions = [h["action"] for h in history]
    assert "create" in actions and "update" in actions
    update_entry = next(h for h in history if h["action"] == "update")
    assert update_entry["changes"]["environment"]["from"] == "staging"
    assert update_entry["changes"]["environment"]["to"] == "production"


async def test_ci_on_unknown_type_is_404(client):
    r = await client.get("/api/v1/ci/ghost", headers=auth(ADMIN_KEY))
    assert r.status_code == 404
