"""
Mongo-backed CI-type schema registry.

This replaces the previous in-process ``registered_models`` dict and the
runtime route mutation that made the original design incompatible with a
multi-worker deployment. Type definitions are now **persisted in MongoDB**, so:

  * every Gunicorn worker reads the same registry — no "upload hits worker A,
    request hits worker B → 404" race;
  * definitions survive restarts;
  * registration is a plain data write guarded by a unique index, so concurrent
    creates resolve deterministically.

Each worker keeps a small TTL cache for the hot validation path plus a
compiled-model cache keyed by ``(name, version)``. Management reads
(``get_type_strict``) bypass the cache for read-your-writes consistency; only CI
write/validation traffic tolerates the bounded staleness window
(``schema_cache_ttl_seconds``).
"""

from __future__ import annotations

import time
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from .config import Settings
from .db import TYPES_COLLECTION, ensure_ci_indexes
from .errors import ConflictError, NotFoundError
from .observability import get_logger
from .schema import CITypeDefinition, FieldSpec, compile_model
from .utils import utcnow

logger = get_logger("cmdboss.registry")


def _serialize_type(doc: dict) -> dict:
    out = dict(doc)
    _id = out.pop("_id", None)
    if _id is not None:
        out["id"] = str(_id)
    return out


class SchemaRegistry:
    """Persistent CI-type registry with per-worker caching."""

    def __init__(self, db: AsyncIOMotorDatabase, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._coll = db[TYPES_COLLECTION]
        # name -> (expires_at_monotonic, doc | None)
        self._type_cache: dict[str, tuple[float, dict | None]] = {}
        # name -> (version, compiled model)
        self._model_cache: dict[str, tuple[int, type[BaseModel]]] = {}
        self._ttl = settings.schema_cache_ttl_seconds

    # --- cache helpers ---------------------------------------------------- #

    def _cache_get(self, name: str) -> tuple[bool, dict | None] | None:
        entry = self._type_cache.get(name)
        if entry is None:
            return None
        expires_at, doc = entry
        if time.monotonic() >= expires_at:
            self._type_cache.pop(name, None)
            return None
        return True, doc

    def _cache_put(self, name: str, doc: dict | None) -> None:
        self._type_cache[name] = (time.monotonic() + self._ttl, doc)

    def invalidate(self, name: str) -> None:
        self._type_cache.pop(name, None)
        self._model_cache.pop(name, None)

    # --- reads ------------------------------------------------------------ #

    async def get_type(self, name: str) -> dict | None:
        """Return the active type doc (TTL-cached) or ``None``. Hot path."""
        name = name.lower()
        cached = self._cache_get(name)
        if cached is not None:
            return cached[1]
        doc = await self._coll.find_one({"name": name, "active": True})
        self._cache_put(name, doc)
        return doc

    async def get_type_strict(self, name: str) -> dict:
        """Cache-bypassing read for management endpoints. Raises if absent."""
        name = name.lower()
        doc = await self._coll.find_one({"name": name, "active": True})
        if doc is None:
            raise NotFoundError(f"CI type '{name}' not found.", details={"type": name})
        return doc

    async def list_types(self, include_inactive: bool = False) -> list[dict]:
        query: dict[str, Any] = {} if include_inactive else {"active": True}
        cursor = self._coll.find(query).sort("name", 1)
        return [_serialize_type(d) async for d in cursor]

    async def compile(self, name: str) -> type[BaseModel]:
        """Return the compiled Pydantic model for a type. Raises if absent."""
        name = name.lower()
        doc = await self.get_type(name)
        if doc is None:
            raise NotFoundError(f"CI type '{name}' not found.", details={"type": name})
        version = int(doc.get("version", 1))
        cached = self._model_cache.get(name)
        if cached is not None and cached[0] == version:
            return cached[1]
        fields = {fname: FieldSpec(**spec) for fname, spec in doc["fields"].items()}
        model = compile_model(name, fields)
        self._model_cache[name] = (version, model)
        return model

    # --- writes ----------------------------------------------------------- #

    async def create_type(self, definition: CITypeDefinition, actor: str) -> dict:
        now = utcnow()
        doc = {
            "name": definition.name,
            "description": definition.description,
            "fields": {k: v.model_dump() for k, v in definition.fields.items()},
            "indexes": [i.model_dump() for i in definition.indexes],
            "version": 1,
            "active": True,
            "created_at": now,
            "updated_at": now,
            "created_by": actor,
            "updated_by": actor,
        }
        try:
            result = await self._coll.insert_one(doc)
        except DuplicateKeyError:
            raise ConflictError(
                f"CI type '{definition.name}' already exists.",
                details={"type": definition.name},
            ) from None
        await ensure_ci_indexes(self._db, definition.name, definition.indexes)
        self.invalidate(definition.name)
        logger.info("ci_type created name=%s by=%s", definition.name, actor)
        doc["_id"] = result.inserted_id
        return _serialize_type(doc)

    async def update_type(self, name: str, definition: CITypeDefinition, actor: str) -> dict:
        """Replace the schema for an existing type, bumping its version.

        Note: this is a forward-only schema change. Existing CI documents are not
        retro-validated; their ``_meta.schema_version`` records the version they
        were written under, preserving lineage.
        """
        name = name.lower()
        if definition.name != name:
            raise ConflictError(
                "Type name in body does not match the URL.",
                details={"url": name, "body": definition.name},
            )
        existing = await self.get_type_strict(name)
        new_version = int(existing.get("version", 1)) + 1
        update = {
            "$set": {
                "description": definition.description,
                "fields": {k: v.model_dump() for k, v in definition.fields.items()},
                "indexes": [i.model_dump() for i in definition.indexes],
                "version": new_version,
                "updated_at": utcnow(),
                "updated_by": actor,
            }
        }
        await self._coll.update_one({"name": name, "active": True}, update)
        await ensure_ci_indexes(self._db, name, definition.indexes)
        self.invalidate(name)
        logger.info("ci_type updated name=%s version=%s by=%s", name, new_version, actor)
        return await self.get_type_strict(name)

    async def deactivate_type(self, name: str, actor: str) -> None:
        """Soft-delete a type. The name stays reserved to preserve schema lineage.

        CI data in ``ci_<name>`` is intentionally retained; deactivation only
        removes the type from the active API surface.
        """
        name = name.lower()
        result = await self._coll.update_one(
            {"name": name, "active": True},
            {"$set": {"active": False, "updated_at": utcnow(), "updated_by": actor}},
        )
        if result.matched_count == 0:
            raise NotFoundError(f"CI type '{name}' not found.", details={"type": name})
        self.invalidate(name)
        logger.info("ci_type deactivated name=%s by=%s", name, actor)
