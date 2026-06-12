"""Registry persistence, versioning, soft-delete and caching behaviour."""

from __future__ import annotations

import pytest
from mongomock_motor import AsyncMongoMockClient

from cmdboss.config import Settings
from cmdboss.db import Database, ensure_core_indexes
from cmdboss.errors import ConflictError, NotFoundError
from cmdboss.registry import SchemaRegistry
from cmdboss.schema import CITypeDefinition, FieldSpec


def _def(name="server"):
    return CITypeDefinition(
        name=name,
        fields={"hostname": FieldSpec(type="string", required=True)},
    )


async def _registry(ttl=5.0):
    db = Database(AsyncMongoMockClient(), "t")
    await ensure_core_indexes(db.db)
    settings = Settings(schema_cache_ttl_seconds=ttl)
    return SchemaRegistry(db.db, settings)


@pytest.mark.asyncio
async def test_create_and_get():
    reg = await _registry()
    stored = await reg.create_type(_def(), actor="tester")
    assert stored["name"] == "server"
    assert stored["version"] == 1
    fetched = await reg.get_type_strict("server")
    assert fetched["fields"]["hostname"]["required"] is True


@pytest.mark.asyncio
async def test_duplicate_create_conflicts():
    reg = await _registry()
    await reg.create_type(_def(), actor="t")
    with pytest.raises(ConflictError):
        await reg.create_type(_def(), actor="t")


@pytest.mark.asyncio
async def test_update_bumps_version():
    reg = await _registry()
    await reg.create_type(_def(), actor="t")
    new_def = CITypeDefinition(
        name="server",
        fields={
            "hostname": FieldSpec(type="string", required=True),
            "os": FieldSpec(type="string"),
        },
    )
    updated = await reg.update_type("server", new_def, actor="t")
    assert updated["version"] == 2
    assert "os" in updated["fields"]


@pytest.mark.asyncio
async def test_compile_reflects_version_change():
    reg = await _registry()
    await reg.create_type(_def(), actor="t")
    model_v1 = await reg.compile("server")
    assert "os" not in model_v1.model_fields
    new_def = CITypeDefinition(
        name="server",
        fields={
            "hostname": FieldSpec(type="string", required=True),
            "os": FieldSpec(type="string"),
        },
    )
    await reg.update_type("server", new_def, actor="t")
    model_v2 = await reg.compile("server")
    assert "os" in model_v2.model_fields


@pytest.mark.asyncio
async def test_deactivate_then_missing():
    reg = await _registry()
    await reg.create_type(_def(), actor="t")
    await reg.deactivate_type("server", actor="t")
    with pytest.raises(NotFoundError):
        await reg.get_type_strict("server")
    assert await reg.get_type("server") is None


@pytest.mark.asyncio
async def test_get_type_uses_cache_within_ttl():
    reg = await _registry(ttl=60.0)
    await reg.create_type(_def(), actor="t")
    # Prime the cache.
    assert await reg.get_type("server") is not None
    # Mutate the DB directly behind the registry's back; cache should still serve.
    await reg._coll.update_one({"name": "server"}, {"$set": {"active": False}})
    assert await reg.get_type("server") is not None  # served from cache
    reg.invalidate("server")
    assert await reg.get_type("server") is None  # now reads through


@pytest.mark.asyncio
async def test_multi_worker_share_via_persistence():
    """Two registries over the same DB (simulating two workers) see each other's writes."""
    db = Database(AsyncMongoMockClient(), "shared")
    await ensure_core_indexes(db.db)
    settings = Settings(schema_cache_ttl_seconds=0.0)
    worker_a = SchemaRegistry(db.db, settings)
    worker_b = SchemaRegistry(db.db, settings)
    await worker_a.create_type(_def(), actor="a")
    # Worker B never received an "upload" yet resolves the type from Mongo.
    model = await worker_b.compile("server")
    assert "hostname" in model.model_fields
