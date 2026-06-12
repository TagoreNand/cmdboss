"""CI relationship graph: contracts, integrity, cardinality, cycles, guard, traversal."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_KEY, SERVER_TYPE, WRITER_KEY, auth

pytestmark = pytest.mark.asyncio

DEPENDS_ON = {
    "name": "depends_on",
    "from_types": ["server"],
    "to_types": ["server"],
    "cardinality": "many_to_many",
    "dependency": True,
}
HOSTS = {
    "name": "hosts",
    "from_types": ["*"],
    "to_types": ["*"],
    "cardinality": "one_to_many",
    "dependency": False,
}


async def _setup(client, n_servers=3):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    await client.post("/api/v1/relationship-types", json=DEPENDS_ON, headers=auth(ADMIN_KEY))
    ids = []
    for i in range(n_servers):
        r = await client.post(
            "/api/v1/ci/server",
            json={"hostname": f"node-{i}", "environment": "production"},
            headers=auth(WRITER_KEY),
        )
        ids.append(r.json()["id"])
    return ids


async def _edge(client, rel_type, frm, to):
    return await client.post(
        "/api/v1/relationships",
        json={"rel_type": rel_type, "from": {"type": "server", "id": frm}, "to": {"type": "server", "id": to}},
        headers=auth(WRITER_KEY),
    )


async def test_relationship_type_crud(client):
    await client.post("/api/v1/types", json=SERVER_TYPE, headers=auth(ADMIN_KEY))
    r = await client.post("/api/v1/relationship-types", json=DEPENDS_ON, headers=auth(ADMIN_KEY))
    assert r.status_code == 201
    r = await client.get("/api/v1/relationship-types/depends_on", headers=auth(ADMIN_KEY))
    assert r.status_code == 200 and r.json()["dependency"] is True


async def test_create_edge_and_referential_integrity(client):
    a, b, _c = await _setup(client)
    # happy path
    r = await _edge(client, "depends_on", a, b)
    assert r.status_code == 201, r.text
    assert r.json()["from"]["id"] == a and r.json()["to"]["id"] == b

    # duplicate -> 409
    assert (await _edge(client, "depends_on", a, b)).status_code == 409

    # unknown rel type -> 404
    assert (await _edge(client, "ghost_rel", a, b)).status_code == 404

    # nonexistent target CI -> 404
    missing = "0123456789abcdef01234567"
    assert (await _edge(client, "depends_on", a, missing)).status_code == 404

    # self-loop disallowed -> 400
    assert (await _edge(client, "depends_on", a, a)).status_code == 400


async def test_disallowed_endpoint_type_is_400(client):
    a, b, _c = await _setup(client)
    r = await client.post(
        "/api/v1/relationships",
        json={"rel_type": "depends_on", "from": {"type": "application", "id": a}, "to": {"type": "server", "id": b}},
        headers=auth(WRITER_KEY),
    )
    assert r.status_code == 400


async def test_cardinality_one_to_many(client):
    a, b, c = await _setup(client)
    await client.post("/api/v1/relationship-types", json=HOSTS, headers=auth(ADMIN_KEY))
    # one_to_many: a target may have only one source
    r1 = await _edge(client, "hosts", a, c)
    assert r1.status_code == 201
    r2 = await _edge(client, "hosts", b, c)  # c already hosted by a
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "cardinality_violation"


async def test_dependency_cycle_rejected(client):
    a, b, c = await _setup(client)
    assert (await _edge(client, "depends_on", a, b)).status_code == 201
    assert (await _edge(client, "depends_on", b, c)).status_code == 201
    cyc = await _edge(client, "depends_on", c, a)  # c->a closes a->b->c->a
    assert cyc.status_code == 409
    assert cyc.json()["error"]["code"] == "dependency_cycle"


async def test_dependencies_and_impact_traversal(client):
    a, b, c = await _setup(client)
    await _edge(client, "depends_on", a, b)
    await _edge(client, "depends_on", b, c)

    deps = (await client.get(f"/api/v1/ci/server/{a}/dependencies", headers=auth(ADMIN_KEY))).json()
    dep_ids = {n["id"] for n in deps["depends_on"]}
    assert dep_ids == {b, c}  # transitive

    impact = (await client.get(f"/api/v1/ci/server/{c}/impact", headers=auth(ADMIN_KEY))).json()
    imp_ids = {n["id"] for n in impact["impacted"]}
    assert imp_ids == {a, b}  # blast radius

    nb = (await client.get(f"/api/v1/ci/server/{b}/relationships", headers=auth(ADMIN_KEY))).json()
    assert len(nb["edges"]) == 2  # one in (a->b), one out (b->c)


async def test_delete_guard_blocks_then_detaches(client):
    a, b, _c = await _setup(client)
    await _edge(client, "depends_on", a, b)

    blocked = await client.delete(f"/api/v1/ci/server/{a}", headers=auth(WRITER_KEY))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "has_relationships"

    detached = await client.delete(f"/api/v1/ci/server/{a}?detach=true", headers=auth(WRITER_KEY))
    assert detached.status_code == 204

    # edge is gone; b no longer has the inbound edge
    edges = (await client.get("/api/v1/relationships", params={"to_id": b}, headers=auth(ADMIN_KEY))).json()
    assert edges["paging"]["returned"] == 0
