"""
Generic Configuration Item CRUD endpoints.

A single set of routes serves every CI type via the ``{type_name}`` path
parameter. The request body is validated at runtime against the compiled model
the registry returns, so no per-type routes are ever added to the running app
(the root cause of the original multi-worker 404 bug).

Concurrency follows HTTP semantics: reads/writes return an ``ETag`` carrying the
resource revision, and mutating requests must supply ``If-Match``. ``POST``
accepts an optional ``Idempotency-Key`` for safe retries, and listing supports
both ``offset`` and opaque ``cursor`` (keyset) pagination.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status

from ..deps import get_repository
from ..errors import BadRequestError, PreconditionRequiredError
from ..observability import get_request_id
from ..repository import CIRepository
from ..security import SCOPE_CI_READ, SCOPE_CI_WRITE, Principal, require

router = APIRouter(prefix="/ci", tags=["Configuration Items"])

_RESERVED_QUERY = {"limit", "offset", "sort", "include_total", "cursor"}


def _set_etag(response: Response, doc: dict) -> None:
    revision = doc.get("_meta", {}).get("revision")
    if revision is not None:
        response.headers["ETag"] = f'"{revision}"'


def _parse_if_match(value: str | None, *, required: bool) -> int | None:
    if value is None or value == "":
        if required:
            raise PreconditionRequiredError(
                "This operation requires an If-Match header carrying the current revision."
            )
        return None
    cleaned = value.strip().strip('"')
    if cleaned == "*":
        return None  # wildcard: accept any current revision
    try:
        return int(cleaned)
    except ValueError:
        raise BadRequestError(
            "If-Match must be an integer revision.", details={"if_match": value}
        ) from None


@router.post("/{type_name}", status_code=status.HTTP_201_CREATED, summary="Create a CI")
async def create_ci(
    type_name: str,
    response: Response,
    request: Request,
    payload: dict = Body(...),
    repo: CIRepository = Depends(get_repository),
    principal: Principal = Depends(require(SCOPE_CI_WRITE)),
):
    idempotency_key = request.headers.get("idempotency-key")
    doc = await repo.create(
        type_name,
        payload,
        actor=principal.name,
        request_id=get_request_id(),
        idempotency_key=idempotency_key,
    )
    _set_etag(response, doc)
    return doc


@router.get("/{type_name}", summary="List CIs (offset or keyset pagination, filterable)")
async def list_ci(
    type_name: str,
    request: Request,
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    sort: str | None = Query(None),
    cursor: str | None = Query(None, description="Opaque keyset cursor (preferred at scale)"),
    include_total: bool = Query(False),
    repo: CIRepository = Depends(get_repository),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    filters = {k: v for k, v in request.query_params.items() if k not in _RESERVED_QUERY}
    return await repo.list(
        type_name,
        limit=limit,
        offset=offset,
        sort=sort,
        cursor=cursor,
        filters=filters,
        include_total=include_total,
    )


@router.get("/{type_name}/{item_id}", summary="Get a CI by id")
async def get_ci(
    type_name: str,
    item_id: str,
    response: Response,
    repo: CIRepository = Depends(get_repository),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    doc = await repo.get(type_name, item_id)
    _set_etag(response, doc)
    return doc


@router.put("/{type_name}/{item_id}", summary="Replace a CI (optimistic concurrency)")
async def replace_ci(
    type_name: str,
    item_id: str,
    response: Response,
    request: Request,
    payload: dict = Body(...),
    repo: CIRepository = Depends(get_repository),
    principal: Principal = Depends(require(SCOPE_CI_WRITE)),
):
    expected = _parse_if_match(request.headers.get("if-match"), required=True)
    doc = await repo.update(
        type_name,
        item_id,
        payload,
        expected_revision=expected,
        partial=False,
        actor=principal.name,
        request_id=get_request_id(),
    )
    _set_etag(response, doc)
    return doc


@router.patch("/{type_name}/{item_id}", summary="Partially update a CI")
async def patch_ci(
    type_name: str,
    item_id: str,
    response: Response,
    request: Request,
    payload: dict = Body(...),
    repo: CIRepository = Depends(get_repository),
    principal: Principal = Depends(require(SCOPE_CI_WRITE)),
):
    expected = _parse_if_match(request.headers.get("if-match"), required=True)
    doc = await repo.update(
        type_name,
        item_id,
        payload,
        expected_revision=expected,
        partial=True,
        actor=principal.name,
        request_id=get_request_id(),
    )
    _set_etag(response, doc)
    return doc


@router.delete(
    "/{type_name}/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a CI",
)
async def delete_ci(
    type_name: str,
    item_id: str,
    request: Request,
    detach: bool = Query(False, description="Remove the CI's relationships first"),
    repo: CIRepository = Depends(get_repository),
    principal: Principal = Depends(require(SCOPE_CI_WRITE)),
):
    expected = _parse_if_match(request.headers.get("if-match"), required=False)
    await repo.delete(
        type_name,
        item_id,
        expected_revision=expected,
        actor=principal.name,
        request_id=get_request_id(),
        detach=detach,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{type_name}/{item_id}/audit", summary="Get change history for a CI")
async def ci_history(
    type_name: str,
    item_id: str,
    limit: int = Query(50, ge=1, le=500),
    repo: CIRepository = Depends(get_repository),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return {"history": await repo.history(type_name, item_id, limit=limit)}
