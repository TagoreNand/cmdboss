"""CI type (schema) management endpoints — the declarative replacement for /models/upload."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from ..deps import get_registry
from ..registry import SchemaRegistry
from ..schema import CITypeDefinition, json_schema_for
from ..security import (
    SCOPE_TYPES_READ,
    SCOPE_TYPES_WRITE,
    Principal,
    require,
)

router = APIRouter(prefix="/types", tags=["Types"])


def _with_schema(definition: CITypeDefinition, stored: dict) -> dict:
    stored = dict(stored)
    stored["json_schema"] = json_schema_for(definition)
    return stored


def _definition_from_doc(doc: dict) -> CITypeDefinition:
    return CITypeDefinition(
        name=doc["name"],
        description=doc.get("description"),
        fields=doc["fields"],
        indexes=doc.get("indexes", []),
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Define a new CI type")
async def create_type(
    definition: CITypeDefinition,
    registry: SchemaRegistry = Depends(get_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_WRITE)),
):
    stored = await registry.create_type(definition, actor=principal.name)
    return _with_schema(definition, stored)


@router.get("", summary="List CI types")
async def list_types(
    include_inactive: bool = Query(False),
    registry: SchemaRegistry = Depends(get_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_READ)),
):
    return {"types": await registry.list_types(include_inactive=include_inactive)}


@router.get("/{name}", summary="Get a CI type definition + JSON Schema")
async def get_type(
    name: str,
    registry: SchemaRegistry = Depends(get_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_READ)),
):
    doc = await registry.get_type_strict(name)
    definition = _definition_from_doc(doc)
    from ..registry import _serialize_type  # local import to avoid cycle at module load

    return _with_schema(definition, _serialize_type(doc))


@router.put("/{name}", summary="Replace a CI type schema (version bump)")
async def update_type(
    name: str,
    definition: CITypeDefinition,
    registry: SchemaRegistry = Depends(get_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_WRITE)),
):
    stored = await registry.update_type(name, definition, actor=principal.name)
    from ..registry import _serialize_type

    return _with_schema(definition, _serialize_type(stored))


@router.delete(
    "/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="Deactivate a CI type"
)
async def delete_type(
    name: str,
    registry: SchemaRegistry = Depends(get_registry),
    principal: Principal = Depends(require(SCOPE_TYPES_WRITE)),
):
    await registry.deactivate_type(name, actor=principal.name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
