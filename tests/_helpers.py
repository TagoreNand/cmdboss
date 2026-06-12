"""Helpers for building repository-level fixtures against mongomock."""

from __future__ import annotations

from mongomock_motor import AsyncMongoMockClient

from cmdboss.audit import AuditLog
from cmdboss.config import Settings
from cmdboss.db import Database, ensure_core_indexes
from cmdboss.idempotency import IdempotencyStore
from cmdboss.outbox import Outbox
from cmdboss.registry import SchemaRegistry
from cmdboss.repository import CIRepository
from cmdboss.schema import CITypeDefinition, FieldSpec


def make_settings(**over) -> Settings:
    base = dict(
        schema_cache_ttl_seconds=0.0,
        outbox_poll_interval_seconds=3600.0,
        cache_invalidation_poll_seconds=3600.0,
        metrics_enabled=False,
        log_json=False,
    )
    base.update(over)
    return Settings(**base)


async def build_repo(settings: Settings | None = None):
    settings = settings or make_settings()
    db = Database(AsyncMongoMockClient(), "t")
    await ensure_core_indexes(db.db, settings.idempotency_ttl_seconds)
    registry = SchemaRegistry(db.db, settings)
    await registry.create_type(
        CITypeDefinition(name="server", fields={"hostname": FieldSpec(type="string", required=True)}),
        actor="t",
    )
    audit = AuditLog(db.db)
    outbox = Outbox(db.db)
    idem = IdempotencyStore(db.db)
    repo = CIRepository(db.db, db.client, registry, audit, outbox, settings, idem, txn_enabled=False)
    return db, settings, registry, repo, outbox
