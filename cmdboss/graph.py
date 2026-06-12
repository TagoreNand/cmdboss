"""
CI relationship graph: registry, edge repository, and traversal.

This turns CMDBoss from a flat store of typed records into a dependency graph.
Edges are first-class, typed, directed, and validated against declarative
relationship-type contracts. Writes reuse the hardened transaction + outbox +
audit path, so edge changes are atomic and durably published.

Integrity guarantees enforced on edge creation:
  * the relationship type exists and is active;
  * both endpoint CIs exist (referential integrity);
  * the endpoint CI types are permitted by the relationship type;
  * cardinality (one_to_one / one_to_many / many_to_one / many_to_many);
  * uniqueness (no duplicate edge);
  * no self-loop unless the type allows it;
  * for ``dependency`` edges, no cycle is introduced (the dependency graph stays
    a DAG, so impact/blast-radius analysis terminates).

Traversal (neighbors / dependencies / impact) is a depth-bounded, cycle-safe BFS
over the edge collection — one indexed query per level. ``$graphLookup`` is the
server-side optimization for very deep graphs; the BFS here is portable and the
same shape.
"""

from __future__ import annotations

import time
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from .audit import AuditLog
from .config import Settings
from .db import (
    REL_TYPES_COLLECTION,
    RELATIONSHIPS_COLLECTION,
    ci_collection_name,
    transaction,
)
from .errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PreconditionFailedError,
)
from .events import Event
from .graph_schema import RelationshipTypeDefinition
from .observability import get_logger, metrics
from .outbox import Outbox
from .utils import jsonable, parse_object_id, utcnow

logger = get_logger("cmdboss.graph")


def _session_kw(session: Any) -> dict:
    return {"session": session} if session is not None else {}


def _serialize_rel_type(doc: dict) -> dict:
    out = dict(doc)
    _id = out.pop("_id", None)
    if _id is not None:
        out["id"] = str(_id)
    return out


def _serialize_edge(edge: dict) -> dict:
    return {
        "id": str(edge["_id"]),
        "rel_type": edge["rel_type"],
        "dependency": bool(edge.get("dependency", False)),
        "from": {"type": edge["from_type"], "id": str(edge["from_id"])},
        "to": {"type": edge["to_type"], "id": str(edge["to_id"])},
        "attributes": edge.get("attributes", {}),
        "_meta": jsonable(edge.get("_meta", {})),
    }


class RelationshipRegistry:
    """Persistent, cached registry of relationship-type contracts."""

    def __init__(self, db: AsyncIOMotorDatabase, settings: Settings) -> None:
        self._coll = db[REL_TYPES_COLLECTION]
        self._ttl = settings.schema_cache_ttl_seconds
        self._cache: dict[str, tuple[float, dict | None]] = {}

    def invalidate(self, name: str) -> None:
        self._cache.pop(name, None)

    async def get_type(self, name: str) -> dict | None:
        name = name.lower()
        entry = self._cache.get(name)
        if entry and time.monotonic() < entry[0]:
            return entry[1]
        doc = await self._coll.find_one({"name": name, "active": True})
        self._cache[name] = (time.monotonic() + self._ttl, doc)
        return doc

    async def get_type_strict(self, name: str) -> dict:
        doc = await self._coll.find_one({"name": name.lower(), "active": True})
        if doc is None:
            raise NotFoundError(f"Relationship type '{name}' not found.", details={"rel_type": name})
        return doc

    async def list_types(self, include_inactive: bool = False) -> list[dict]:
        query: dict[str, Any] = {} if include_inactive else {"active": True}
        return [_serialize_rel_type(d) async for d in self._coll.find(query).sort("name", 1)]

    async def create_type(self, definition: RelationshipTypeDefinition, actor: str) -> dict:
        now = utcnow()
        doc = {**definition.model_dump(), "active": True, "created_at": now,
               "updated_at": now, "created_by": actor, "updated_by": actor}
        try:
            result = await self._coll.insert_one(doc)
        except DuplicateKeyError:
            raise ConflictError(
                f"Relationship type '{definition.name}' already exists.",
                details={"rel_type": definition.name},
            ) from None
        self.invalidate(definition.name)
        logger.info("rel_type created name=%s by=%s", definition.name, actor)
        doc["_id"] = result.inserted_id
        return _serialize_rel_type(doc)

    async def deactivate_type(self, name: str, actor: str) -> None:
        result = await self._coll.update_one(
            {"name": name.lower(), "active": True},
            {"$set": {"active": False, "updated_at": utcnow(), "updated_by": actor}},
        )
        if result.matched_count == 0:
            raise NotFoundError(f"Relationship type '{name}' not found.", details={"rel_type": name})
        self.invalidate(name)


class RelationshipRepository:
    """Edge CRUD, integrity enforcement, and graph traversal."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        client: AsyncIOMotorClient,
        rel_registry: RelationshipRegistry,
        audit: AuditLog,
        outbox: Outbox,
        settings: Settings,
        txn_enabled: bool = False,
    ) -> None:
        self._db = db
        self._client = client
        self._edges = db[RELATIONSHIPS_COLLECTION]
        self._rel_registry = rel_registry
        self._audit = audit
        self._outbox = outbox
        self._settings = settings
        self._txn = txn_enabled
        self._max_depth = settings.graph_max_depth

    # --- helpers ---------------------------------------------------------- #

    async def _ci_exists(self, ci_type: str, oid) -> bool:
        doc = await self._db[ci_collection_name(ci_type)].find_one({"_id": oid}, {"_id": 1})
        return doc is not None

    async def _rel_def(self, rel_type: str) -> RelationshipTypeDefinition:
        doc = await self._rel_registry.get_type(rel_type)
        if doc is None:
            raise NotFoundError(
                f"Relationship type '{rel_type}' not found.", details={"rel_type": rel_type}
            )
        fields = {k: doc[k] for k in RelationshipTypeDefinition.model_fields if k in doc}
        return RelationshipTypeDefinition(**fields)

    # --- create ----------------------------------------------------------- #

    async def create_edge(
        self,
        *,
        rel_type: str,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        attributes: dict | None,
        actor: str,
        request_id: str | None,
        provenance: dict | None = None,
    ) -> dict:
        rel_def = await self._rel_def(rel_type)
        from_type, to_type = from_type.lower(), to_type.lower()
        if not rel_def.allows_from(from_type):
            raise BadRequestError(
                f"Relationship '{rel_type}' does not allow source type '{from_type}'.",
                details={"allowed_from": rel_def.from_types},
            )
        if not rel_def.allows_to(to_type):
            raise BadRequestError(
                f"Relationship '{rel_type}' does not allow target type '{to_type}'.",
                details={"allowed_to": rel_def.to_types},
            )

        f_oid = parse_object_id(from_id)
        t_oid = parse_object_id(to_id)
        if from_type == to_type and f_oid == t_oid and not rel_def.allow_self:
            raise BadRequestError("Self-referencing relationship is not allowed for this type.")

        if not await self._ci_exists(from_type, f_oid):
            raise NotFoundError(f"Source CI {from_type}/{from_id} not found.")
        if not await self._ci_exists(to_type, t_oid):
            raise NotFoundError(f"Target CI {to_type}/{to_id} not found.")

        await self._check_cardinality(rel_def, from_type, f_oid, to_type, t_oid)
        if rel_def.dependency:
            await self._check_no_cycle(from_type, f_oid, to_type, t_oid)

        now = utcnow()
        edge = {
            "rel_type": rel_def.name,
            "dependency": rel_def.dependency,
            "from_type": from_type,
            "from_id": f_oid,
            "to_type": to_type,
            "to_id": t_oid,
            "attributes": attributes or {},
            "_meta": {
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "created_by": actor,
                "updated_by": actor,
            },
        }
        if provenance:
            edge["_meta"].update(provenance)
        try:
            async with transaction(self._client, enabled=self._txn) as session:
                result = await self._edges.insert_one(edge, **_session_kw(session))
                edge["_id"] = result.inserted_id
                serialized = _serialize_edge(edge)
                await self._audit.record(
                    action="create",
                    entity_type=f"rel:{rel_def.name}",
                    entity_id=serialized["id"],
                    actor=actor,
                    after=serialized,
                    request_id=request_id,
                    session=session,
                )
                await self._outbox.add(
                    Event(
                        type="relationship.created",
                        entity_type=f"rel:{rel_def.name}",
                        entity_id=serialized["id"],
                        actor=actor,
                        payload=serialized,
                    ),
                    session=session,
                )
        except DuplicateKeyError:
            raise ConflictError(
                "An identical relationship already exists.", code="duplicate_edge"
            ) from None

        metrics.incr("ci_operations_total", {"type": f"rel:{rel_def.name}", "operation": "create"})
        return serialized

    async def _check_cardinality(self, rel_def, ft, fid, tt, tid) -> None:
        card = rel_def.cardinality
        rt = rel_def.name
        if card in ("one_to_one", "many_to_one"):
            n = await self._edges.count_documents(
                {"rel_type": rt, "from_type": ft, "from_id": fid}
            )
            if n > 0:
                raise ConflictError(
                    f"Cardinality '{card}': source already has a '{rt}' relationship.",
                    code="cardinality_violation",
                )
        if card in ("one_to_one", "one_to_many"):
            n = await self._edges.count_documents({"rel_type": rt, "to_type": tt, "to_id": tid})
            if n > 0:
                raise ConflictError(
                    f"Cardinality '{card}': target already has a '{rt}' relationship.",
                    code="cardinality_violation",
                )

    async def _check_no_cycle(self, ft, fid, tt, tid) -> None:
        # Adding "F depends on T" cycles iff T already (transitively) depends on F.
        nodes, _edges, _trunc = await self._bfs(
            tt, tid, direction="out", dependency_only=True
        )
        if any(n["type"] == ft and n["id"] == str(fid) for n in nodes):
            raise ConflictError(
                "Relationship would create a dependency cycle.", code="dependency_cycle"
            )

    # --- read / delete ---------------------------------------------------- #

    async def get_edge(self, edge_id: str) -> dict:
        edge = await self._edges.find_one({"_id": parse_object_id(edge_id)})
        if edge is None:
            raise NotFoundError(f"Relationship '{edge_id}' not found.", details={"id": edge_id})
        return _serialize_edge(edge)

    async def list_edges(
        self,
        *,
        rel_type: str | None = None,
        from_type: str | None = None,
        from_id: str | None = None,
        to_type: str | None = None,
        to_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        query: dict[str, Any] = {}
        if rel_type:
            query["rel_type"] = rel_type.lower()
        if from_type:
            query["from_type"] = from_type.lower()
        if from_id:
            query["from_id"] = parse_object_id(from_id)
        if to_type:
            query["to_type"] = to_type.lower()
        if to_id:
            query["to_id"] = parse_object_id(to_id)
        limit = max(1, min(limit, self._settings.max_page_size))
        cursor = self._edges.find(query).sort("_id", -1).skip(max(0, offset)).limit(limit)
        items = [_serialize_edge(e) async for e in cursor]
        return {"items": items, "paging": {"limit": limit, "offset": offset, "returned": len(items)}}

    async def delete_edge(
        self, edge_id: str, *, expected_revision: int | None, actor: str, request_id: str | None
    ) -> None:
        oid = parse_object_id(edge_id)
        async with transaction(self._client, enabled=self._txn) as session:
            edge = await self._edges.find_one({"_id": oid}, **_session_kw(session))
            if edge is None:
                raise NotFoundError(f"Relationship '{edge_id}' not found.", details={"id": edge_id})
            if expected_revision is not None:
                current = int(edge["_meta"]["revision"])
                if current != expected_revision:
                    raise PreconditionFailedError(
                        "Revision mismatch.",
                        details={"expected_revision": expected_revision, "current_revision": current},
                    )
            serialized = _serialize_edge(edge)
            await self._edges.delete_one({"_id": oid}, **_session_kw(session))
            await self._audit.record(
                action="delete",
                entity_type=f"rel:{edge['rel_type']}",
                entity_id=serialized["id"],
                actor=actor,
                before=serialized,
                request_id=request_id,
                session=session,
            )
            await self._outbox.add(
                Event(
                    type="relationship.deleted",
                    entity_type=f"rel:{edge['rel_type']}",
                    entity_id=serialized["id"],
                    actor=actor,
                    payload=serialized,
                ),
                session=session,
            )

    # --- CI delete-guard interface (duck-typed; injected into CIRepository) - #

    async def count_edges(self, ci_type: str, ci_id: str, *, session: Any = None) -> int:
        oid = parse_object_id(ci_id) if isinstance(ci_id, str) else ci_id
        query = {
            "$or": [
                {"from_type": ci_type, "from_id": oid},
                {"to_type": ci_type, "to_id": oid},
            ]
        }
        return await self._edges.count_documents(query, **_session_kw(session))

    async def iter_source_edges(self, source: str) -> dict[tuple, str]:
        """Map (rel_type, from_id, to_id) -> edge_id for edges owned by ``source``."""
        out: dict[tuple, str] = {}
        async for e in self._edges.find({"_meta.source": source}):
            out[(e["rel_type"], str(e["from_id"]), str(e["to_id"]))] = str(e["_id"])
        return out

    async def detach_ci(
        self, ci_type: str, ci_id: str, *, actor: str, request_id: str | None, session: Any = None
    ) -> int:
        oid = parse_object_id(ci_id) if isinstance(ci_id, str) else ci_id
        query = {
            "$or": [
                {"from_type": ci_type, "from_id": oid},
                {"to_type": ci_type, "to_id": oid},
            ]
        }
        edges = [e async for e in self._edges.find(query, **_session_kw(session))]
        if not edges:
            return 0
        await self._edges.delete_many(query, **_session_kw(session))
        for e in edges:
            serialized = _serialize_edge(e)
            await self._audit.record(
                action="delete",
                entity_type=f"rel:{e['rel_type']}",
                entity_id=serialized["id"],
                actor=actor,
                before=serialized,
                request_id=request_id,
                session=session,
            )
            await self._outbox.add(
                Event(
                    type="relationship.deleted",
                    entity_type=f"rel:{e['rel_type']}",
                    entity_id=serialized["id"],
                    actor=actor,
                    payload=serialized,
                ),
                session=session,
            )
        return len(edges)

    # --- traversal -------------------------------------------------------- #

    async def _bfs(
        self, start_type: str, start_oid, *, direction: str, dependency_only: bool
    ) -> tuple[list[dict], list[dict], bool]:
        visited = {(start_type, str(start_oid))}
        frontier: list[tuple[str, Any]] = [(start_type, start_oid)]
        nodes: list[dict] = []
        edges: list[dict] = []
        for _ in range(self._max_depth):
            if not frontier:
                break
            if direction == "out":
                ors = [{"from_type": t, "from_id": i} for (t, i) in frontier]
            else:
                ors = [{"to_type": t, "to_id": i} for (t, i) in frontier]
            query: dict[str, Any] = {"$or": ors}
            if dependency_only:
                query["dependency"] = True
            next_frontier: list[tuple[str, Any]] = []
            async for e in self._edges.find(query):
                edges.append(_serialize_edge(e))
                if direction == "out":
                    nt, ni = e["to_type"], e["to_id"]
                else:
                    nt, ni = e["from_type"], e["from_id"]
                key = (nt, str(ni))
                if key not in visited:
                    visited.add(key)
                    nodes.append({"type": nt, "id": str(ni)})
                    next_frontier.append((nt, ni))
            frontier = next_frontier
        truncated = bool(frontier)
        return nodes, edges, truncated

    async def neighbors(
        self, ci_type: str, ci_id: str, *, direction: str = "both", rel_type: str | None = None
    ) -> dict:
        oid = parse_object_id(ci_id)
        clauses = []
        if direction in ("out", "both"):
            clauses.append({"from_type": ci_type, "from_id": oid})
        if direction in ("in", "both"):
            clauses.append({"to_type": ci_type, "to_id": oid})
        if not clauses:
            raise BadRequestError("direction must be one of: in, out, both.")
        query: dict[str, Any] = {"$or": clauses} if len(clauses) > 1 else clauses[0]
        if rel_type:
            query["rel_type"] = rel_type.lower()
        edges = [_serialize_edge(e) async for e in self._edges.find(query)]
        return {"node": {"type": ci_type, "id": ci_id}, "direction": direction, "edges": edges}

    async def dependencies(self, ci_type: str, ci_id: str) -> dict:
        oid = parse_object_id(ci_id)
        nodes, edges, truncated = await self._bfs(
            ci_type, oid, direction="out", dependency_only=True
        )
        return {
            "root": {"type": ci_type, "id": ci_id},
            "depends_on": nodes,
            "edges": edges,
            "truncated": truncated,
            "max_depth": self._max_depth,
        }

    async def impact(self, ci_type: str, ci_id: str) -> dict:
        oid = parse_object_id(ci_id)
        nodes, edges, truncated = await self._bfs(
            ci_type, oid, direction="in", dependency_only=True
        )
        return {
            "root": {"type": ci_type, "id": ci_id},
            "impacted": nodes,
            "edges": edges,
            "truncated": truncated,
            "max_depth": self._max_depth,
        }
