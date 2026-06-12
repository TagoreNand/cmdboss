"""
MongoDB connection lifecycle, index management, and transaction support.

The Motor client is created **per worker process** inside the application
lifespan (see :mod:`cmdboss.app`). This matters under Gunicorn: each Uvicorn
worker is a separate process with its own event loop, and an
``AsyncIOMotorClient`` must be created after the fork so its socket pool binds
to the correct loop. We therefore never instantiate the client at import time.

Multi-document transactions require a replica set. :func:`supports_transactions`
probes the live deployment once at startup; :func:`transaction` then yields a
real session when supported and ``None`` (sequential best-effort) otherwise, so
the same repository code runs on a replica set, a standalone node, or mongomock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel

from .config import Settings
from .observability import get_logger

logger = get_logger("cmdboss.db")

# Collection names for system bookkeeping. CI data lives in ``ci_<type>``.
TYPES_COLLECTION = "_ci_types"
AUDIT_COLLECTION = "_audit"
API_KEYS_COLLECTION = "_api_keys"
OUTBOX_COLLECTION = "_outbox"
IDEMPOTENCY_COLLECTION = "_idempotency"
REL_TYPES_COLLECTION = "_rel_types"
RELATIONSHIPS_COLLECTION = "_relationships"
DISCOVERY_RUNS_COLLECTION = "_discovery_runs"
WEBHOOKS_COLLECTION = "_webhooks"

CI_COLLECTION_PREFIX = "ci_"


def ci_collection_name(type_name: str) -> str:
    return f"{CI_COLLECTION_PREFIX}{type_name}"


def create_client(settings: Settings) -> AsyncIOMotorClient:
    """Construct a Motor client from settings. Does not perform I/O yet."""
    return AsyncIOMotorClient(
        settings.mongo_uri,
        maxPoolSize=settings.mongo_max_pool_size,
        minPoolSize=settings.mongo_min_pool_size,
        serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
        appname=settings.app_name,
        tz_aware=True,
    )


async def ensure_core_indexes(
    db: AsyncIOMotorDatabase, idempotency_ttl_seconds: int = 86_400
) -> None:
    """Create indexes for the system collections. Idempotent.

    Wrapped defensively: a transient Mongo hiccup at startup should be logged
    and surfaced by readiness checks rather than crash every worker in a loop.
    """
    try:
        await db[TYPES_COLLECTION].create_indexes(
            [
                IndexModel([("name", ASCENDING)], name="uq_type_name", unique=True),
                IndexModel([("active", ASCENDING)], name="ix_type_active"),
                IndexModel([("updated_at", ASCENDING)], name="ix_type_updated_at"),
            ]
        )
        await db[AUDIT_COLLECTION].create_indexes(
            [
                IndexModel(
                    [("entity_type", ASCENDING), ("entity_id", ASCENDING), ("ts", DESCENDING)],
                    name="ix_audit_entity",
                ),
                IndexModel([("ts", DESCENDING)], name="ix_audit_ts"),
            ]
        )
        await db[API_KEYS_COLLECTION].create_indexes(
            [
                IndexModel([("key_hash", ASCENDING)], name="uq_key_hash", unique=True),
                IndexModel([("active", ASCENDING)], name="ix_key_active"),
            ]
        )
        await db[OUTBOX_COLLECTION].create_indexes(
            [
                IndexModel([("status", ASCENDING), ("next_attempt_at", ASCENDING)], name="ix_outbox_pending"),
            ]
        )
        await db[IDEMPOTENCY_COLLECTION].create_indexes(
            [
                IndexModel(
                    [("created_at", ASCENDING)],
                    name="ttl_idempotency",
                    expireAfterSeconds=idempotency_ttl_seconds,
                ),
            ]
        )
        await db[REL_TYPES_COLLECTION].create_indexes(
            [
                IndexModel([("name", ASCENDING)], name="uq_rel_type_name", unique=True),
                IndexModel([("active", ASCENDING)], name="ix_rel_type_active"),
            ]
        )
        await db[RELATIONSHIPS_COLLECTION].create_indexes(
            [
                IndexModel(
                    [
                        ("rel_type", ASCENDING),
                        ("from_type", ASCENDING),
                        ("from_id", ASCENDING),
                        ("to_type", ASCENDING),
                        ("to_id", ASCENDING),
                    ],
                    name="uq_edge",
                    unique=True,
                ),
                IndexModel(
                    [("from_type", ASCENDING), ("from_id", ASCENDING), ("dependency", ASCENDING)],
                    name="ix_edge_out",
                ),
                IndexModel(
                    [("to_type", ASCENDING), ("to_id", ASCENDING), ("dependency", ASCENDING)],
                    name="ix_edge_in",
                ),
                IndexModel([("rel_type", ASCENDING)], name="ix_edge_rel_type"),
            ]
        )
        await db[DISCOVERY_RUNS_COLLECTION].create_indexes(
            [
                IndexModel([("source", ASCENDING), ("started_at", DESCENDING)], name="ix_runs_source"),
                IndexModel([("started_at", DESCENDING)], name="ix_runs_started"),
            ]
        )
        await db[WEBHOOKS_COLLECTION].create_indexes(
            [IndexModel([("active", ASCENDING)], name="ix_webhook_active")]
        )
        logger.info("core indexes ensured")
    except Exception as exc:  # pragma: no cover - depends on live Mongo
        logger.error("failed ensuring core indexes: %s", exc, exc_info=True)
        raise


async def ensure_ci_indexes(
    db: AsyncIOMotorDatabase, type_name: str, indexes: list
) -> None:
    """Create per-type indexes declared in a CI type definition.

    Always ensures a ``_meta.updated_at`` index for efficient listing/sorting and
    keyset pagination at scale, then layers on user-declared indexes.
    """
    coll = db[ci_collection_name(type_name)]
    models = [
        IndexModel([("_meta.updated_at", DESCENDING), ("_id", DESCENDING)], name="ix_meta_updated_at"),
        IndexModel([("_meta.source", ASCENDING), ("_meta.external_id", ASCENDING)], name="ix_meta_source"),
    ]
    for i, idx in enumerate(indexes):
        fields = idx["fields"] if isinstance(idx, dict) else idx.fields
        unique = idx.get("unique", False) if isinstance(idx, dict) else idx.unique
        keys = [(f, ASCENDING) for f in fields]
        models.append(IndexModel(keys, name=f"ix_user_{i}", unique=unique))
    try:
        await coll.create_indexes(models)
    except Exception as exc:  # pragma: no cover - depends on live Mongo
        logger.error("failed ensuring CI indexes for %s: %s", type_name, exc, exc_info=True)
        raise


async def supports_transactions(client: AsyncIOMotorClient, db_name: str) -> bool:
    """Probe whether the live deployment supports multi-document transactions.

    Returns True only if a trivial transaction commits. Standalone MongoDB and
    mongomock return False (they raise on sessions/transactions).
    """
    probe = db_name
    session = None
    try:
        session = await client.start_session()
    except Exception:
        return False
    try:
        async with session.start_transaction():
            await client[probe]["_txn_probe"].insert_one({"_id": ObjectId()}, session=session)
        await client[probe]["_txn_probe"].delete_many({})
        return True
    except Exception:
        return False
    finally:
        try:
            await session.end_session()
        except Exception:  # pragma: no cover
            pass


@asynccontextmanager
async def transaction(
    client: AsyncIOMotorClient, *, enabled: bool = True
) -> AsyncIterator[Any | None]:
    """Yield a session bound to a transaction when ``enabled``, else ``None``.

    ``enabled`` should already reflect :func:`supports_transactions`, so this
    context manager never attempts an unsupported transaction.
    """
    if not enabled:
        yield None
        return
    session = await client.start_session()
    try:
        async with session.start_transaction():
            yield session
    finally:
        await session.end_session()


async def ping(db: AsyncIOMotorDatabase) -> bool:
    """Liveness probe for the database. Returns True if reachable."""
    try:
        await db.command("ping")
        return True
    except Exception:
        return False


class Database:
    """Thin holder bundling the client + database handle for the app lifespan."""

    def __init__(self, client: AsyncIOMotorClient, db_name: str) -> None:
        self.client = client
        self.db: AsyncIOMotorDatabase = client[db_name]

    def close(self) -> None:
        self.client.close()
