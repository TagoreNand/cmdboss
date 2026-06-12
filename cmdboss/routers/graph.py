"""
Relationship graph endpoints: relationship-type contracts, edge CRUD, and
CI-scoped traversal (neighbors / dependencies / impact).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ..deps import get_rel_registry, get_rel_repository
from ..errors import BadRequestError
from ..graph import RelationshipRegistry, RelationshipRepository
from ..graph_schema import RelationshipTypeDefinition
from ..observability import get_request_id
from ..security import (
    SCOPE_CI_READ,
    SCOPE_CI_WRITE,
    SCOPE_TYPES_READ,
    SCOPE_TYPES_WRITE,
    Principal,
    require,
)

router = APIRouter(tags=["Relationships"])


# --- relationship type contracts ----------------------------------------- #


@router.post(
    "/relationship-types",
    status_code=status.HTTP_201_CREATED,
    summary="Define a relationship type",
)
async def create_rel_type(
    definition: RelationshipTypeDefinition,
    registry: RelationshipRegistry = Depends(get_rel_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_WRITE)),
):
    return await registry.create_type(definition, actor=principal.name)


@router.get("/relationship-types", summary="List relationship types")
async def list_rel_types(
    include_inactive: bool = Query(False),
    registry: RelationshipRegistry = Depends(get_rel_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_READ)),
):
    return {"relationship_types": await registry.list_types(include_inactive=include_inactive)}


@router.get("/relationship-types/{name}", summary="Get a relationship type")
async def get_rel_type(
    name: str,
    registry: RelationshipRegistry = Depends(get_rel_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_READ)),
):
    from ..graph import _serialize_rel_type

    return _serialize_rel_type(await registry.get_type_strict(name))


@router.delete(
    "/relationship-types/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate a relationship type",
)
async def delete_rel_type(
    name: str,
    registry: RelationshipRegistry = Depends(get_rel_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_WRITE)),
):
    await registry.deactivate_type(name, actor=principal.name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- edges ---------------------------------------------------------------- #


class NodeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    id: str


class RelationshipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    rel_type: str
    from_: NodeRef = Field(alias="from")
    to: NodeRef
    attributes: dict | None = None


def _parse_if_match(value: str | None) -> int | None:
    if not value:
        return None
    cleaned = value.strip().strip('"')
    if cleaned == "*":
        return None
    try:
        return int(cleaned)
    except ValueError:
        raise BadRequestError("If-Match must be an integer revision.") from None


@router.post(
    "/relationships", status_code=status.HTTP_201_CREATED, summary="Create a relationship edge"
)
async def create_edge(
    body: RelationshipCreate,
    repo: RelationshipRepository = Depends(get_rel_repository),
    principal: Principal = Depends(require(SCOPE_CI_WRITE)),
):
    return await repo.create_edge(
        rel_type=body.rel_type,
        from_type=body.from_.type,
        from_id=body.from_.id,
        to_type=body.to.type,
        to_id=body.to.id,
        attributes=body.attributes,
        actor=principal.name,
        request_id=get_request_id(),
    )


@router.get("/relationships", summary="List relationship edges")
async def list_edges(
    rel_type: str | None = Query(None),
    from_type: str | None = Query(None),
    from_id: str | None = Query(None),
    to_type: str | None = Query(None),
    to_id: str | None = Query(None),
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
    repo: RelationshipRepository = Depends(get_rel_repository),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return await repo.list_edges(
        rel_type=rel_type,
        from_type=from_type,
        from_id=from_id,
        to_type=to_type,
        to_id=to_id,
        limit=limit,
        offset=offset,
    )


@router.get("/relationships/{edge_id}", summary="Get a relationship edge")
async def get_edge(
    edge_id: str,
    repo: RelationshipRepository = Depends(get_rel_repository),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return await repo.get_edge(edge_id)


@router.delete(
    "/relationships/{edge_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a relationship edge",
)
async def delete_edge(
    edge_id: str,
    request: Request,
    repo: RelationshipRepository = Depends(get_rel_repository),
    principal: Principal = Depends(require(SCOPE_CI_WRITE)),
):
    expected = _parse_if_match(request.headers.get("if-match"))
    await repo.delete_edge(
        edge_id, expected_revision=expected, actor=principal.name, request_id=get_request_id()
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- CI-scoped traversal -------------------------------------------------- #


@router.get("/ci/{type_name}/{item_id}/relationships", summary="Direct neighbors of a CI")
async def ci_neighbors(
    type_name: str,
    item_id: str,
    direction: str = Query("both", pattern="^(in|out|both)$"),
    rel_type: str | None = Query(None),
    repo: RelationshipRepository = Depends(get_rel_repository),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return await repo.neighbors(type_name, item_id, direction=direction, rel_type=rel_type)


@router.get("/ci/{type_name}/{item_id}/dependencies", summary="Transitive dependencies of a CI")
async def ci_dependencies(
    type_name: str,
    item_id: str,
    repo: RelationshipRepository = Depends(get_rel_repository),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return await repo.dependencies(type_name, item_id)


@router.get("/ci/{type_name}/{item_id}/impact", summary="Blast radius (what depends on a CI)")
async def ci_impact(
    type_name: str,
    item_id: str,
    repo: RelationshipRepository = Depends(get_rel_repository),
    principal: Principal = Depends(require(SCOPE_CI_READ)),
):
    return await repo.impact(type_name, item_id)
