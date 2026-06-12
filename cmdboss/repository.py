"""
Generic Configuration Item repository.

A single repository serves *every* CI type via a type parameter, so there are no
per-model routes mutated at runtime — the validated payload shape comes from the
compiled model the :class:`~cmdboss.registry.SchemaRegistry` hands us.

Production properties implemented here:
  * **Atomicity** — each mutation writes the CI change, its audit record and an
    outbox event in one unit of work (a transaction when supported; sequential
    otherwise). Events are delivered durably by the outbox dispatcher, never
    dropped.
  * **Optimistic concurrency** via a monotonic ``_meta.revision`` and a
    compare-and-swap ``find_one_and_update`` (no lost updates).
  * **Idempotent create** via an optional ``Idempotency-Key`` so retried POSTs
    do not duplicate CIs.
  * **Keyset pagination** (plus offset for back-compat) so listing survives
    collections with millions of documents.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pydantic import BaseModel, ValidationError
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from .audit import AuditLog
from .config import Settings
from .cursor import decode_cursor, encode_cursor
from .db import ci_collection_name, transaction
from .errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PreconditionFailedError,
    ValidationFailedError,
)
from .events import Event
from .idempotency import STATUS_DONE, IdempotencyStore
from .observability import get_logger, metrics
from .outbox import Outbox
from .registry import SchemaRegistry
from .schema import FieldSpec
from .utils import jsonable, parse_object_id, utcnow

logger = get_logger("cmdboss.repository")

_META = "_meta"
_RESERVED_QUERY = {"limit", "offset", "sort", "include_total", "cursor"}


def _session_kw(session: Any) -> dict:
    return {"session": session} if session is not None else {}


def _validate(model: type[BaseModel], payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise BadRequestError("Request body must be a JSON object.")
    try:
        obj = model(**payload)
    except ValidationError as exc:
        raise ValidationFailedError(
            "Payload failed schema validation.",
            details={
                "errors": [
                    {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ]
            },
        ) from exc
    return obj.model_dump(mode="json")


def _serialize(doc: dict) -> dict:
    meta = doc.get(_META, {})
    out = {k: jsonable(v) for k, v in doc.items() if k not in ("_id", _META)}
    out["id"] = str(doc["_id"])
    out[_META] = jsonable(meta)
    return out


def _get_path(doc: dict, path: str) -> Any:
    """Resolve a possibly dotted path (e.g. ``_meta.updated_at``) from a raw doc."""
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _coerce_filter_value(spec: FieldSpec, raw: str) -> Any:
    try:
        if spec.type == "integer":
            return int(raw)
        if spec.type == "number":
            return float(raw)
        if spec.type == "boolean":
            return raw.lower() in ("1", "true", "yes")
        return raw
    except (TypeError, ValueError):
        raise BadRequestError("Invalid filter value for field.", details={"value": raw}) from None


class CIRepository:
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        client: AsyncIOMotorClient,
        registry: SchemaRegistry,
        audit: AuditLog,
        outbox: Outbox,
        settings: Settings,
        idempotency: IdempotencyStore | None = None,
        txn_enabled: bool = False,
        relationship_guard: Any = None,
    ) -> None:
        self._db = db
        self._client = client
        self._registry = registry
        self._audit = audit
        self._outbox = outbox
        self._settings = settings
        self._idem = idempotency
        self._txn = txn_enabled
        self._rel_guard = relationship_guard

    def _coll(self, type_name: str):
        return self._db[ci_collection_name(type_name)]

    async def _type_doc(self, type_name: str) -> dict:
        doc = await self._registry.get_type(type_name)
        if doc is None:
            raise NotFoundError(f"CI type '{type_name}' not found.", details={"type": type_name})
        return doc

    # --- create ----------------------------------------------------------- #

    async def create(
        self,
        type_name: str,
        payload: dict,
        *,
        actor: str,
        request_id: str | None,
        idempotency_key: str | None = None,
    ) -> dict:
        type_doc = await self._type_doc(type_name)
        model = await self._registry.compile(type_name)
        data = _validate(model, payload)

        use_idem = bool(idempotency_key) and self._idem is not None and self._settings.idempotency_enabled
        if use_idem:
            replay = await self._idempotency_precheck(type_name, idempotency_key)
            if replay is not None:
                return replay

        now = utcnow()
        data[_META] = {
            "type": type_name,
            "revision": 1,
            "schema_version": int(type_doc.get("version", 1)),
            "created_at": now,
            "updated_at": now,
            "created_by": actor,
            "updated_by": actor,
        }

        try:
            async with transaction(self._client, enabled=self._txn) as session:
                coll = self._coll(type_name)
                result = await coll.insert_one(data, **_session_kw(session))
                data["_id"] = result.inserted_id
                serialized = _serialize(data)
                await self._audit.record(
                    action="create",
                    entity_type=type_name,
                    entity_id=serialized["id"],
                    actor=actor,
                    before=None,
                    after=serialized,
                    schema_version=data[_META]["schema_version"],
                    revision=1,
                    request_id=request_id,
                    session=session,
                )
                await self._outbox.add(
                    Event(
                        type="ci.created",
                        entity_type=type_name,
                        entity_id=serialized["id"],
                        actor=actor,
                        payload=serialized,
                    ),
                    session=session,
                )
                if use_idem:
                    await self._idem.store_result(type_name, idempotency_key, serialized, session=session)
        except Exception:
            if use_idem:
                await self._idem.release(type_name, idempotency_key)
            raise

        metrics.incr("ci_operations_total", {"type": type_name, "operation": "create"})
        return serialized

    async def _idempotency_precheck(self, scope: str, key: str) -> dict | None:
        """Return a stored response to replay, raise on in-progress, else None (claimed)."""
        rec = await self._idem.get(scope, key)
        if rec is not None:
            if rec.get("status") == STATUS_DONE:
                return rec.get("response")
            raise ConflictError(
                "A request with this Idempotency-Key is already in progress.",
                code="idempotency_in_progress",
            )
        claimed = await self._idem.claim(scope, key)
        if not claimed:
            rec = await self._idem.get(scope, key)
            if rec and rec.get("status") == STATUS_DONE:
                return rec.get("response")
            raise ConflictError(
                "A request with this Idempotency-Key is already in progress.",
                code="idempotency_in_progress",
            )
        return None

    # --- read ------------------------------------------------------------- #

    async def get(self, type_name: str, item_id: str) -> dict:
        await self._type_doc(type_name)
        oid = parse_object_id(item_id)
        doc = await self._coll(type_name).find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(
                f"{type_name} '{item_id}' not found.",
                details={"type": type_name, "id": item_id},
            )
        return _serialize(doc)

    async def list(
        self,
        type_name: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        sort: str | None = None,
        cursor: str | None = None,
        filters: dict[str, str] | None = None,
        include_total: bool = False,
    ) -> dict:
        type_doc = await self._type_doc(type_name)
        fields = type_doc["fields"]

        limit = limit or self._settings.default_page_size
        limit = max(1, min(limit, self._settings.max_page_size))
        offset = max(0, offset)

        filter_query: dict[str, Any] = {}
        for key, raw in (filters or {}).items():
            if key in _RESERVED_QUERY:
                continue
            if key not in fields:
                raise BadRequestError(
                    f"Unknown filter field '{key}'.",
                    details={"allowed": sorted(fields.keys())},
                )
            spec = FieldSpec(**fields[key])
            filter_query[key] = _coerce_filter_value(spec, raw)

        sort_field, sort_dir = self._parse_sort(sort, fields)
        coll = self._coll(type_name)

        query: dict[str, Any] = dict(filter_query)
        using_cursor = cursor is not None
        if using_cursor:
            value, last_oid = decode_cursor(cursor)
            op = "$lt" if sort_dir == DESCENDING else "$gt"
            keyset = {
                "$or": [
                    {sort_field: {op: value}},
                    {sort_field: value, "_id": {op: last_oid}},
                ]
            }
            query = {"$and": [filter_query, keyset]} if filter_query else keyset

        find = coll.find(query).sort([(sort_field, sort_dir), ("_id", sort_dir)])
        if not using_cursor:
            find = find.skip(offset)
        find = find.limit(limit)

        raw_docs = [d async for d in find]
        items = [_serialize(d) for d in raw_docs]

        paging: dict[str, Any] = {"limit": limit, "returned": len(items)}
        full_page = len(items) == limit
        if using_cursor:
            paging["cursor"] = cursor
        else:
            paging["offset"] = offset
            paging["next_offset"] = offset + limit if full_page else None
        if full_page and raw_docs:
            last = raw_docs[-1]
            paging["next_cursor"] = encode_cursor(_get_path(last, sort_field), last["_id"])
        else:
            paging["next_cursor"] = None
        if include_total:
            paging["total"] = await coll.count_documents(filter_query)

        metrics.incr("ci_operations_total", {"type": type_name, "operation": "list"})
        return {"items": items, "paging": paging}

    def _parse_sort(self, sort: str | None, fields: dict) -> tuple[str, int]:
        if not sort:
            return f"{_META}.updated_at", DESCENDING
        direction = ASCENDING
        field = sort
        if sort.startswith("-"):
            direction = DESCENDING
            field = sort[1:]
        if field in ("created_at", "updated_at"):
            return f"{_META}.{field}", direction
        if field not in fields:
            raise BadRequestError(
                f"Cannot sort by unknown field '{field}'.",
                details={"allowed": sorted(list(fields.keys()) + ["created_at", "updated_at"])},
            )
        return field, direction

    # --- update ----------------------------------------------------------- #

    async def update(
        self,
        type_name: str,
        item_id: str,
        payload: dict,
        *,
        expected_revision: int,
        partial: bool,
        actor: str,
        request_id: str | None,
    ) -> dict:
        type_doc = await self._type_doc(type_name)
        model = await self._registry.compile(type_name)
        oid = parse_object_id(item_id)
        coll = self._coll(type_name)

        async with transaction(self._client, enabled=self._txn) as session:
            existing = await coll.find_one({"_id": oid}, **_session_kw(session))
            if existing is None:
                raise NotFoundError(
                    f"{type_name} '{item_id}' not found.",
                    details={"type": type_name, "id": item_id},
                )
            before = _serialize(existing)

            if partial:
                merged = {k: v for k, v in existing.items() if k not in ("_id", _META)}
                merged.update(payload)
                data = _validate(model, merged)
            else:
                data = _validate(model, payload)

            new_revision = int(existing[_META]["revision"]) + 1
            update = {
                "$set": {
                    **data,
                    f"{_META}.updated_at": utcnow(),
                    f"{_META}.updated_by": actor,
                    f"{_META}.schema_version": int(type_doc.get("version", 1)),
                    f"{_META}.revision": new_revision,
                }
            }
            # Compare-and-swap on the expected revision: the lost-update guard.
            updated = await coll.find_one_and_update(
                {"_id": oid, f"{_META}.revision": expected_revision},
                update,
                return_document=ReturnDocument.AFTER,
                **_session_kw(session),
            )
            if updated is None:
                current = await coll.find_one({"_id": oid}, **_session_kw(session))
                if current is None:
                    raise NotFoundError(
                        f"{type_name} '{item_id}' not found.",
                        details={"type": type_name, "id": item_id},
                    )
                raise PreconditionFailedError(
                    "Revision mismatch — resource was modified concurrently.",
                    details={
                        "expected_revision": expected_revision,
                        "current_revision": int(current[_META]["revision"]),
                    },
                )

            after = _serialize(updated)
            await self._audit.record(
                action="update",
                entity_type=type_name,
                entity_id=item_id,
                actor=actor,
                before=before,
                after=after,
                schema_version=int(type_doc.get("version", 1)),
                revision=new_revision,
                request_id=request_id,
                session=session,
            )
            await self._outbox.add(
                Event(
                    type="ci.updated",
                    entity_type=type_name,
                    entity_id=item_id,
                    actor=actor,
                    payload=after,
                ),
                session=session,
            )

        metrics.incr("ci_operations_total", {"type": type_name, "operation": "update"})
        return after

    # --- delete ----------------------------------------------------------- #

    async def delete(
        self,
        type_name: str,
        item_id: str,
        *,
        expected_revision: int | None,
        actor: str,
        request_id: str | None,
        detach: bool = False,
    ) -> None:
        await self._type_doc(type_name)
        oid = parse_object_id(item_id)
        coll = self._coll(type_name)

        async with transaction(self._client, enabled=self._txn) as session:
            existing = await coll.find_one({"_id": oid}, **_session_kw(session))
            if existing is None:
                raise NotFoundError(
                    f"{type_name} '{item_id}' not found.",
                    details={"type": type_name, "id": item_id},
                )
            if expected_revision is not None:
                current_rev = int(existing[_META]["revision"])
                if current_rev != expected_revision:
                    raise PreconditionFailedError(
                        "Revision mismatch — resource was modified concurrently.",
                        details={
                            "expected_revision": expected_revision,
                            "current_revision": current_rev,
                        },
                    )

            if self._rel_guard is not None:
                edge_count = await self._rel_guard.count_edges(
                    type_name, item_id, session=session
                )
                if edge_count > 0:
                    if not detach:
                        raise ConflictError(
                            f"{type_name} '{item_id}' has {edge_count} relationship(s); "
                            "delete blocked. Retry with ?detach=true to remove them.",
                            code="has_relationships",
                            details={"relationship_count": edge_count},
                        )
                    await self._rel_guard.detach_ci(
                        type_name, item_id, actor=actor, request_id=request_id, session=session
                    )

            before = _serialize(existing)
            await coll.delete_one({"_id": oid}, **_session_kw(session))
            await self._audit.record(
                action="delete",
                entity_type=type_name,
                entity_id=item_id,
                actor=actor,
                before=before,
                after=None,
                schema_version=int(existing[_META].get("schema_version", 1)),
                revision=int(existing[_META]["revision"]),
                request_id=request_id,
                session=session,
            )
            await self._outbox.add(
                Event(
                    type="ci.deleted",
                    entity_type=type_name,
                    entity_id=item_id,
                    actor=actor,
                    payload=before,
                ),
                session=session,
            )

        metrics.incr("ci_operations_total", {"type": type_name, "operation": "delete"})

    # --- history ---------------------------------------------------------- #

    async def history(self, type_name: str, item_id: str, limit: int = 50) -> list[dict]:
        await self._type_doc(type_name)
        return await self._audit.list_for(type_name, item_id, limit=limit)

    # --- discovery / reconciliation ---------------------------------------- #

    async def apply_discovery(
        self, type_name: str, data: dict, *, source: str, external_id: str, run_id: str, actor: str
    ) -> tuple[str, str]:
        """Upsert a discovered CI by ``(source, external_id)``.

        Returns ``(ci_id, action)`` where action is created/updated/unchanged.
        Unchanged items only refresh provenance (no revision bump, no event), so
        re-running discovery over a stable inventory is cheap.
        """
        type_doc = await self._type_doc(type_name)
        model = await self._registry.compile(type_name)
        coll = self._coll(type_name)
        schema_version = int(type_doc.get("version", 1))
        prov = {
            "source": source,
            "external_id": external_id,
            "discovery_run_id": run_id,
            "last_seen": utcnow(),
            "discovery_status": "present",
        }
        async with transaction(self._client, enabled=self._txn) as session:
            existing = await coll.find_one(
                {"_meta.source": source, "_meta.external_id": external_id}, **_session_kw(session)
            )
            if existing is None:
                validated = _validate(model, data)
                now = utcnow()
                doc = dict(validated)
                doc[_META] = {
                    "type": type_name,
                    "revision": 1,
                    "schema_version": schema_version,
                    "created_at": now,
                    "updated_at": now,
                    "created_by": actor,
                    "updated_by": actor,
                    **prov,
                }
                result = await coll.insert_one(doc, **_session_kw(session))
                doc["_id"] = result.inserted_id
                serialized = _serialize(doc)
                await self._audit.record(
                    action="create",
                    entity_type=type_name,
                    entity_id=serialized["id"],
                    actor=actor,
                    before=None,
                    after=serialized,
                    schema_version=schema_version,
                    revision=1,
                    request_id=run_id,
                    session=session,
                )
                await self._outbox.add(
                    Event(
                        type="ci.created",
                        entity_type=type_name,
                        entity_id=serialized["id"],
                        actor=actor,
                        payload=serialized,
                    ),
                    session=session,
                )
                metrics.incr("ci_operations_total", {"type": type_name, "operation": "discovery_create"})
                return serialized["id"], "created"

            current_user = {k: v for k, v in existing.items() if k not in ("_id", _META)}
            merged = {**current_user, **data}
            validated = _validate(model, merged)
            existing_norm = _validate(model, current_user)
            if validated == existing_norm:
                await coll.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {f"{_META}.{k}": v for k, v in prov.items()}},
                    **_session_kw(session),
                )
                return str(existing["_id"]), "unchanged"

            before = _serialize(existing)
            new_rev = int(existing[_META]["revision"]) + 1
            setdoc: dict[str, Any] = {
                **validated,
                f"{_META}.updated_at": utcnow(),
                f"{_META}.updated_by": actor,
                f"{_META}.schema_version": schema_version,
                f"{_META}.revision": new_rev,
            }
            setdoc.update({f"{_META}.{k}": v for k, v in prov.items()})
            updated = await coll.find_one_and_update(
                {"_id": existing["_id"]},
                {"$set": setdoc},
                return_document=ReturnDocument.AFTER,
                **_session_kw(session),
            )
            after = _serialize(updated)
            await self._audit.record(
                action="update",
                entity_type=type_name,
                entity_id=after["id"],
                actor=actor,
                before=before,
                after=after,
                schema_version=schema_version,
                revision=new_rev,
                request_id=run_id,
                session=session,
            )
            await self._outbox.add(
                Event(
                    type="ci.updated",
                    entity_type=type_name,
                    entity_id=after["id"],
                    actor=actor,
                    payload=after,
                ),
                session=session,
            )
            metrics.incr("ci_operations_total", {"type": type_name, "operation": "discovery_update"})
            return after["id"], "updated"

    async def iter_source(self, type_name: str, source: str) -> dict[str, str]:
        """Map external_id -> ci_id for all CIs of a type owned by ``source``."""
        out: dict[str, str] = {}
        async for d in self._coll(type_name).find(
            {"_meta.source": source}, {"_meta.external_id": 1}
        ):
            ext = d.get("_meta", {}).get("external_id")
            if ext is not None:
                out[ext] = str(d["_id"])
        return out

    async def mark_missing(self, type_name: str, item_id: str, *, run_id: str, actor: str) -> None:
        """Flag a source-owned CI no longer seen by discovery (non-destructive)."""
        oid = parse_object_id(item_id)
        coll = self._coll(type_name)
        async with transaction(self._client, enabled=self._txn) as session:
            existing = await coll.find_one({"_id": oid}, **_session_kw(session))
            if existing is None:
                return
            before = _serialize(existing)
            new_rev = int(existing[_META]["revision"]) + 1
            updated = await coll.find_one_and_update(
                {"_id": oid},
                {
                    "$set": {
                        f"{_META}.discovery_status": "missing",
                        f"{_META}.missing_run_id": run_id,
                        f"{_META}.updated_at": utcnow(),
                        f"{_META}.updated_by": actor,
                        f"{_META}.revision": new_rev,
                    }
                },
                return_document=ReturnDocument.AFTER,
                **_session_kw(session),
            )
            after = _serialize(updated)
            await self._audit.record(
                action="update",
                entity_type=type_name,
                entity_id=item_id,
                actor=actor,
                before=before,
                after=after,
                revision=new_rev,
                request_id=run_id,
                session=session,
            )
            await self._outbox.add(
                Event(
                    type="ci.missing",
                    entity_type=type_name,
                    entity_id=item_id,
                    actor=actor,
                    payload=after,
                ),
                session=session,
            )
