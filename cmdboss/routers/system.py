"""System endpoints: health, readiness, metrics, and API-key administration."""

from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from .. import __version__
from ..db import API_KEYS_COLLECTION, ping
from ..errors import BadRequestError, NotFoundError
from ..observability import metrics
from ..security import (
    ROLE_PRESETS,
    SCOPE_ADMIN,
    Principal,
    create_api_key,
    require,
    revoke_api_key,
    rotate_api_key,
)

router = APIRouter(tags=["System"])


@router.get("/healthz", summary="Liveness probe")
async def healthz():
    return {"status": "ok", "version": __version__}


@router.get("/readyz", summary="Readiness probe (checks MongoDB)")
async def readyz(request: Request, response: Response):
    db = request.app.state.database.db
    ok = await ping(db)
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "mongo": False}
    return {"status": "ready", "mongo": True}


@router.get("/metrics", summary="Prometheus metrics exposition")
async def prometheus_metrics():
    if not metrics.enabled:
        raise NotFoundError("Metrics are not enabled.")
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    role: str | None = None
    scopes: list[str] | None = None
    ttl_seconds: int | None = Field(default=None, ge=1)


@router.post(
    "/admin/api-keys",
    status_code=status.HTTP_201_CREATED,
    summary="Mint a new API key (admin only)",
)
async def create_key(
    body: ApiKeyCreate,
    request: Request,
    principal: Principal = Depends(require(SCOPE_ADMIN)),
):
    if body.role:
        if body.role not in ROLE_PRESETS:
            raise BadRequestError(
                f"Unknown role '{body.role}'.",
                details={"allowed_roles": sorted(ROLE_PRESETS.keys())},
            )
        scopes = ROLE_PRESETS[body.role]
    elif body.scopes:
        scopes = body.scopes
    else:
        raise BadRequestError("Provide either 'role' or 'scopes'.")

    db = request.app.state.database.db
    expires_at = None
    if body.ttl_seconds:
        expires_at = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=body.ttl_seconds)
    try:
        raw, record = await create_api_key(db, body.name, scopes, expires_at=expires_at)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc

    return {
        "api_key": raw,  # shown once — store it securely
        "id": str(record["_id"]),
        "name": record["name"],
        "prefix": record["prefix"],
        "scopes": record["scopes"],
        "expires_at": expires_at.isoformat() if expires_at else None,
        "warning": "This is the only time the key is shown. Store it now.",
    }


@router.get("/admin/api-keys", summary="List API keys (admin only)")
async def list_keys(
    request: Request,
    principal: Principal = Depends(require(SCOPE_ADMIN)),
):
    db = request.app.state.database.db
    cursor = db[API_KEYS_COLLECTION].find({}, {"key_hash": 0})
    keys = []
    async for d in cursor:
        d["id"] = str(d.pop("_id"))
        if hasattr(d.get("created_at"), "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
        keys.append(d)
    return {"api_keys": keys}


@router.post("/admin/api-keys/{key_id}/rotate", summary="Rotate an API key's secret (admin)")
async def rotate_key(
    key_id: str,
    request: Request,
    principal: Principal = Depends(require(SCOPE_ADMIN)),
):
    db = request.app.state.database.db
    raw, record = await rotate_api_key(db, key_id)
    return {
        "api_key": raw,
        "id": record["id"],
        "name": record["name"],
        "prefix": record["prefix"],
        "scopes": record["scopes"],
        "warning": "The previous secret is now invalid. Store this one.",
    }


@router.delete(
    "/admin/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key (admin)",
)
async def revoke_key(
    key_id: str,
    request: Request,
    principal: Principal = Depends(require(SCOPE_ADMIN)),
):
    db = request.app.state.database.db
    await revoke_api_key(db, key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/admin/outbox/dead", summary="List dead-lettered events (admin)")
async def list_dead_events(
    request: Request,
    limit: int = 50,
    principal: Principal = Depends(require(SCOPE_ADMIN)),
):
    outbox = request.app.state.outbox
    return {"dead": await outbox.fetch_dead(limit)}


@router.post("/admin/outbox/dead/{event_id}/replay", summary="Replay a dead event (admin)")
async def replay_dead_event(
    event_id: str,
    request: Request,
    principal: Principal = Depends(require(SCOPE_ADMIN)),
):
    outbox = request.app.state.outbox
    replayed = await outbox.replay(event_id)
    if not replayed:
        raise NotFoundError(f"Dead event '{event_id}' not found.", details={"id": event_id})
    return {"replayed": event_id}
