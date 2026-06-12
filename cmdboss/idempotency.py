"""
Idempotency-key store for safe write retries.

A client that times out on ``POST`` cannot know whether the server committed the
write. Re-sending the request would create a duplicate Configuration Item. By
attaching an ``Idempotency-Key`` header, the client makes create operations
safely retryable: the first request *claims* the key (a unique insert), performs
the write, and stores the response; any later request with the same key replays
the stored response instead of writing again.

Records carry ``created_at`` and a TTL index expires them after
``idempotency_ttl_seconds`` so the collection stays bounded.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from .db import IDEMPOTENCY_COLLECTION
from .observability import get_logger
from .utils import utcnow

logger = get_logger("cmdboss.idempotency")

STATUS_PENDING = "pending"
STATUS_DONE = "done"


def _session_kw(session: Any) -> dict:
    return {"session": session} if session is not None else {}


class IdempotencyStore:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._coll = db[IDEMPOTENCY_COLLECTION]

    @staticmethod
    def _key(scope: str, key: str) -> str:
        return f"{scope}:{key}"

    async def get(self, scope: str, key: str) -> dict | None:
        return await self._coll.find_one({"_id": self._key(scope, key)})

    async def claim(self, scope: str, key: str) -> bool:
        """Atomically claim a key. Returns False if it already exists."""
        try:
            await self._coll.insert_one(
                {"_id": self._key(scope, key), "status": STATUS_PENDING, "created_at": utcnow()}
            )
            return True
        except DuplicateKeyError:
            return False

    async def store_result(
        self, scope: str, key: str, response: dict, *, session: Any = None
    ) -> None:
        await self._coll.update_one(
            {"_id": self._key(scope, key)},
            {"$set": {"status": STATUS_DONE, "response": response, "completed_at": utcnow()}},
            **_session_kw(session),
        )

    async def release(self, scope: str, key: str) -> None:
        """Drop a claim so a failed write can be retried before the TTL elapses."""
        try:
            await self._coll.delete_one({"_id": self._key(scope, key), "status": STATUS_PENDING})
        except Exception as exc:  # pragma: no cover - best effort cleanup
            logger.warning("failed releasing idempotency claim %s:%s: %s", scope, key, exc)
