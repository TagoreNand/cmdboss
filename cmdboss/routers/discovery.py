"""Discovery endpoints (run providers, inspect runs) and webhook registration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from ..deps import get_discovery_service, get_webhook_registry
from ..discovery.service import DiscoveryService
from ..security import (
    SCOPE_ADMIN,
    SCOPE_CI_READ,
    SCOPE_CI_WRITE,
    Principal,
    require,
)
from ..webhooks import WebhookRegistry

router = APIRouter(tags=["Discovery"])


class DiscoveryRunRequest(BaseModel):
    provider: str
    config: dict | None = None
    source: str | None = None
    on_missing: str | None = Field(default=None, pattern="^(mark|delete)$")


@router.post("/discovery/run", summary="Run a discovery provider and reconcile")
async def run_discovery(
    body: DiscoveryRunRequest,
    service: DiscoveryService = Depends(get_discovery_service),
    principal: Principal = Depends(require(SCOPE_CI_WRITE)),
):
    return await service.run(
        provider_name=body.provider,
        config=body.config,
        source=body.source,
        on_missing=body.on_missing,
        actor=principal.name,
    )


@router.get("/discovery/providers", summary="List registered discovery providers")
async def list_providers(
    service: DiscoveryService = Depends(get_discovery_service),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return {"providers": service.providers()}


@router.get("/discovery/runs", summary="List discovery runs")
async def list_runs(
    source: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    service: DiscoveryService = Depends(get_discovery_service),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return {"runs": await service.list_runs(source=source, limit=limit)}


@router.get("/discovery/runs/{run_id}", summary="Get a discovery run report")
async def get_run(
    run_id: str,
    service: DiscoveryService = Depends(get_discovery_service),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return await service.get_run(run_id)


class WebhookCreate(BaseModel):
    url: str
    event_types: list[str] | None = None
    secret: str | None = None


@router.post(
    "/webhooks", status_code=status.HTTP_201_CREATED, summary="Register an outbound webhook"
)
async def create_webhook(
    body: WebhookCreate,
    registry: WebhookRegistry = Depends(get_webhook_registry),
    principal: Principal = Depends(require(SCOPE_ADMIN)),
):
    return await registry.register(body.url, body.event_types, body.secret)


@router.get("/webhooks", summary="List outbound webhooks")
async def list_webhooks(
    registry: WebhookRegistry = Depends(get_webhook_registry),
    principal: Principal = Depends(require(SCOPE_ADMIN)),
):
    return {"webhooks": await registry.list()}
