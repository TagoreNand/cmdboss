"""Discovery: provider run + reconciliation (create/unchanged/mark-missing/stale edges)."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_KEY, SERVER_TYPE, auth

pytestmark = pytest.mark.asyncio

DEPENDS_ON = {
    "name": "depends_on",
    "from_types": ["server"],
    "to_types": ["server"],
    "dependency": True,
}


async def _setup(client):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    await client.post("/api/v1/relationship-types", json=DEPENDS_ON, headers=auth(ADMIN_KEY))


def _config(items, rels):
    return {
        "items": [
            {"type": "server", "external_id": x, "data": d} for x, d in items
        ],
        "relationships": rels,
    }


async def _run(client, items, rels, on_missing=None):
    body = {"provider": "static", "source": "aws", "config": _config(items, rels)}
    if on_missing:
        body["on_missing"] = on_missing
    r = await client.post("/api/v1/discovery/run", json=body, headers=auth(ADMIN_KEY))
    assert r.status_code == 200, r.text
    return r.json()


async def _ci_id(client, external_id):
    r = await client.get("/api/v1/ci/server?limit=100", headers=auth(ADMIN_KEY))
    for it in r.json()["items"]:
        if it["_meta"].get("external_id") == external_id:
            return it["id"]
    return None


async def test_providers_listed(client):
    r = await client.get("/api/v1/discovery/providers", headers=auth(ADMIN_KEY))
    assert "static" in r.json()["providers"]


async def test_unknown_provider_is_404(client):
    r = await client.post(
        "/api/v1/discovery/run", json={"provider": "ghost", "config": {}}, headers=auth(ADMIN_KEY)
    )
    assert r.status_code == 404


async def test_full_reconcile_lifecycle(client):
    await _setup(client)
    edge = {"rel_type": "depends_on", "from": {"type": "server", "external_id": "i-1"},
            "to": {"type": "server", "external_id": "i-2"}}

    # Run 1: create two CIs + one dependency edge
    out = await _run(
        client,
        [("i-1", {"hostname": "disc-1", "environment": "production"}),
         ("i-2", {"hostname": "disc-2", "environment": "staging"})],
        [edge],
    )
    assert out["report"]["ci"]["created"] == 2
    assert out["report"]["relationships"]["created"] == 1

    # provenance + impact graph
    i1 = await _ci_id(client, "i-1")
    i2 = await _ci_id(client, "i-2")
    impact = (await client.get(f"/api/v1/ci/server/{i2}/impact", headers=auth(ADMIN_KEY))).json()
    assert any(n["id"] == i1 for n in impact["impacted"])

    # Run 2: identical -> everything unchanged (cheap re-sync)
    out2 = await _run(
        client,
        [("i-1", {"hostname": "disc-1", "environment": "production"}),
         ("i-2", {"hostname": "disc-2", "environment": "staging"})],
        [edge],
    )
    assert out2["report"]["ci"]["unchanged"] == 2
    assert out2["report"]["relationships"]["unchanged"] == 1

    # Run 3: i-2 disappears, no edges -> i-2 marked missing, stale edge deleted
    out3 = await _run(
        client, [("i-1", {"hostname": "disc-1", "environment": "production"})], [], on_missing="mark"
    )
    assert out3["report"]["ci"]["marked_missing"] == 1
    assert out3["report"]["relationships"]["deleted"] == 1

    # the run is retrievable
    run = await client.get(f"/api/v1/discovery/runs/{out3['run_id']}", headers=auth(ADMIN_KEY))
    assert run.json()["status"] == "completed"
