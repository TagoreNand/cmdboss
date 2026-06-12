"""
Discovery service: orchestrates a provider run + reconciliation + run record.

A run resolves the named provider, fetches its desired state, reconciles it, and
persists an immutable run report in ``_discovery_runs``. Runs are synchronous
here for simplicity and testability; wrapping ``run`` in a background task or a
scheduled job is a drop-in change since it returns a serializable report.
"""

from __future__ import annotations

import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..config import Settings
from ..db import DISCOVERY_RUNS_COLLECTION
from ..errors import NotFoundError
from ..observability import get_logger, metrics
from ..utils import utcnow
from .reconciler import Reconciler
from .registry import ProviderRegistry

logger = get_logger("cmdboss.discovery")


class DiscoveryService:
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        registry: ProviderRegistry,
        reconciler: Reconciler,
        settings: Settings,
    ) -> None:
        self._runs = db[DISCOVERY_RUNS_COLLECTION]
        self._registry = registry
        self._reconciler = reconciler
        self._settings = settings

    def providers(self) -> list[str]:
        return self._registry.names()

    async def run(
        self,
        *,
        provider_name: str,
        config: dict | None,
        source: str | None,
        on_missing: str | None,
        actor: str,
    ) -> dict[str, Any]:
        provider = self._registry.get(provider_name)
        if provider is None:
            raise NotFoundError(
                f"Discovery provider '{provider_name}' is not registered.",
                details={"available": self._registry.names()},
            )
        source = source or provider_name
        on_missing = on_missing or self._settings.discovery_default_on_missing
        run_id = uuid.uuid4().hex

        run_doc: dict[str, Any] = {
            "_id": run_id,
            "provider": provider_name,
            "source": source,
            "on_missing": on_missing,
            "status": "running",
            "started_at": utcnow(),
            "actor": actor,
        }
        await self._runs.insert_one(run_doc)
        logger.info("discovery run started id=%s provider=%s source=%s", run_id, provider_name, source)

        try:
            result = await provider.discover(config or {})
            report = await self._reconciler.reconcile(
                result, source=source, run_id=run_id, on_missing=on_missing, actor=actor
            )
        except Exception as exc:
            await self._runs.update_one(
                {"_id": run_id},
                {"$set": {"status": "failed", "finished_at": utcnow(), "error": str(exc)[:500]}},
            )
            metrics.incr("ci_operations_total", {"type": f"discovery:{provider_name}", "operation": "failed"})
            logger.error("discovery run %s failed: %s", run_id, exc)
            raise

        await self._runs.update_one(
            {"_id": run_id},
            {"$set": {"status": "completed", "finished_at": utcnow(), "report": report}},
        )
        metrics.incr("ci_operations_total", {"type": f"discovery:{provider_name}", "operation": "completed"})
        logger.info("discovery run completed id=%s report=%s", run_id, report)
        return {"run_id": run_id, "provider": provider_name, "source": source,
                "status": "completed", "report": report}

    async def get_run(self, run_id: str) -> dict[str, Any]:
        doc = await self._runs.find_one({"_id": run_id})
        if doc is None:
            raise NotFoundError(f"Discovery run '{run_id}' not found.", details={"run_id": run_id})
        return _serialize_run(doc)

    async def list_runs(self, *, source: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = {"source": source} if source else {}
        limit = max(1, min(limit, 200))
        cursor = self._runs.find(query).sort("started_at", -1).limit(limit)
        return [_serialize_run(d) async for d in cursor]


def _serialize_run(doc: dict) -> dict:
    out = dict(doc)
    out["run_id"] = out.pop("_id")
    for k in ("started_at", "finished_at"):
        if hasattr(out.get(k), "isoformat"):
            out[k] = out[k].isoformat()
    return out
