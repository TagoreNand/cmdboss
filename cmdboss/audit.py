"""
Durable change-auditing and lineage.

Every CI mutation writes an audit record as part of the same unit of work as the
CI change (a transaction when the deployment supports one — see
:mod:`cmdboss.outbox`). Unlike best-effort events, audit records are the system
of record for "who changed what, when, and from what to what", satisfying the
change-auditing and lineage directive.

Records are immutable append-only documents in the ``_audit`` collection,
indexed by ``(entity_type, entity_id, ts)`` for fast per-asset history queries.
"""

from __future__ import annotations

import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from .db import AUDIT_COLLECTION
from .observability import get_logger
from .utils import jsonable, utcnow

logger = get_logger("cmdboss.audit")


def _session_kw(session: Any) -> dict:
    return {"session": session} if session is not None else {}


def _diff(before: dict | None, after: dict | None) -> dict[str, Any]:
    """Compute a shallow field-level diff for human-readable lineage."""
    changes: dict[str, Any] = {}
    before = before or {}
    after = after or {}
    keys = set(before) | set(after)
    for k in keys:
        if k in ("_meta", "id"):
            continue
        b = before.get(k)
        a = after.get(k)
        if b != a:
            changes[k] = {"from": jsonable(b), "to": jsonable(a)}
    return changes


class AuditLog:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._coll = db[AUDIT_COLLECTION]

    async def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str | None,
        actor: str,
        before: dict | None = None,
        after: dict | None = None,
        schema_version: int | None = None,
        revision: int | None = None,
        request_id: str | None = None,
        session: Any = None,
    ) -> None:
        entry = {
            "_id": uuid.uuid4().hex,
            "ts": utcnow(),
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "schema_version": schema_version,
            "revision": revision,
            "request_id": request_id,
            "before": jsonable(before) if before is not None else None,
            "after": jsonable(after) if after is not None else None,
            "changes": _diff(before, after) if action == "update" else {},
        }
        try:
            await self._coll.insert_one(entry, **_session_kw(session))
        except Exception as exc:  # pragma: no cover - depends on live Mongo
            logger.error("audit write failed action=%s entity=%s: %s", action, entity_id, exc)
            raise

    async def list_for(self, entity_type: str, entity_id: str, limit: int = 50) -> list[dict]:
        cursor = (
            self._coll.find({"entity_type": entity_type, "entity_id": entity_id})
            .sort("ts", -1)
            .limit(limit)
        )
        out: list[dict] = []
        async for d in cursor:
            d["ts"] = d["ts"].isoformat() if hasattr(d.get("ts"), "isoformat") else d.get("ts")
            out.append(d)
        return out
