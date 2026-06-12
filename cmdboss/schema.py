"""
Declarative CI-type schema DSL and compiler.

This module is the security-critical replacement for the previous
``exec``-based model upload. Instead of executing uploaded Python, callers
submit a **declarative** field specification (pure data). We validate that
specification with Pydantic and then *compile* it — in memory — into a Pydantic
model via :func:`pydantic.create_model`. No user-supplied code is ever executed,
which removes the remote-code-execution surface entirely.

The compiled model is what validates Configuration Item (CI) payloads at write
time. ``extra="forbid"`` guarantees strict schemas: unknown fields are rejected,
satisfying the data-integrity directive.
"""

from __future__ import annotations

import datetime as _dt
import keyword
import re
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)

# Type names map to concrete Python types. Kept deliberately small and safe.
FieldType = Literal[
    "string", "integer", "number", "boolean", "datetime", "array", "object", "reference"
]
ScalarItemType = Literal["string", "integer", "number", "boolean", "datetime", "reference"]

_PRIMITIVES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "datetime": _dt.datetime,
    "object": dict[str, Any],
    "reference": str,  # stored as the target CI id; relationship integrity is a graph-milestone concern
}

# CI type names and field names must be safe collection/identifier tokens.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_FIELD_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$")
# Field names we reserve for system metadata.
_RESERVED_FIELDS = {"id", "_id", "_meta"}


class FieldSpec(BaseModel):
    """Declarative description of a single CI field."""

    model_config = ConfigDict(extra="forbid")

    type: FieldType
    required: bool = False
    description: str | None = None
    default: Any | None = None

    # string / reference constraints
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    pattern: str | None = None

    # numeric constraints
    minimum: float | None = None
    maximum: float | None = None

    # enum (string/integer/number)
    enum: list[Any] | None = None

    # array constraints
    items: ScalarItemType | None = None
    min_items: int | None = Field(default=None, ge=0)
    max_items: int | None = Field(default=None, ge=0)

    # reference target (which CI type this points at) — informational at foundation stage
    ref_type: str | None = None

    @field_validator("pattern")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid regex pattern: {exc}") from exc
        return v

    @model_validator(mode="after")
    def _coherent(self) -> FieldSpec:
        if self.type == "array" and self.items is None:
            raise ValueError("array fields require an 'items' scalar type")
        if self.items is not None and self.type != "array":
            raise ValueError("'items' is only valid for array fields")
        if self.enum is not None and self.type not in ("string", "integer", "number"):
            raise ValueError("'enum' is only valid for string/integer/number fields")
        if self.pattern is not None and self.type not in ("string", "reference"):
            raise ValueError("'pattern' is only valid for string/reference fields")
        if self.type == "reference" and not self.ref_type:
            raise ValueError("reference fields require 'ref_type'")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("'minimum' cannot exceed 'maximum'")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("'min_length' cannot exceed 'max_length'")
        if (
            self.min_items is not None
            and self.max_items is not None
            and self.min_items > self.max_items
        ):
            raise ValueError("'min_items' cannot exceed 'max_items'")
        if self.required and self.default is not None:
            raise ValueError("a required field cannot also declare a default")
        return self


class CITypeDefinition(BaseModel):
    """Full declarative definition of a Configuration Item type."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    fields: dict[str, FieldSpec]
    # Index hints applied to the backing collection: each is a list of field names
    # plus an optional uniqueness flag.
    indexes: list[IndexSpec] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not _NAME_RE.match(v):
            raise ValueError(
                "type name must match ^[a-z][a-z0-9_]{0,62}$ (lowercase, no leading digit)"
            )
        return v

    @model_validator(mode="after")
    def _validate_fields(self) -> CITypeDefinition:
        if not self.fields:
            raise ValueError("a CI type must declare at least one field")
        for fname in self.fields:
            if not _FIELD_RE.match(fname):
                raise ValueError(f"invalid field name '{fname}'")
            if fname in _RESERVED_FIELDS:
                raise ValueError(f"'{fname}' is a reserved field name")
            if keyword.iskeyword(fname):
                raise ValueError(f"'{fname}' is a Python keyword and cannot be a field name")
        declared = set(self.fields)
        for idx in self.indexes:
            unknown = set(idx.fields) - declared
            if unknown:
                raise ValueError(f"index references unknown field(s): {sorted(unknown)}")
        return self


class IndexSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[str] = Field(min_length=1)
    unique: bool = False


CITypeDefinition.model_rebuild()


def _annotate(spec: FieldSpec) -> Any:
    """Return the Python type annotation for a field spec (pre-optionality)."""
    if spec.enum is not None:
        # Literal of the allowed values; this also enforces the enum at validation time.
        return Literal[tuple(spec.enum)]  # type: ignore[valid-type]
    if spec.type == "array":
        item_type = _PRIMITIVES[spec.items]  # type: ignore[index]
        return list[item_type]  # type: ignore[valid-type]
    return _PRIMITIVES[spec.type]


def _constraints(spec: FieldSpec) -> dict[str, Any]:
    """Build the Field() constraint kwargs valid for this field's type.

    Constraints are gated by type because Pydantic raises at model-build time if,
    e.g., ``ge=`` is attached to a ``str`` field.
    """
    kw: dict[str, Any] = {}
    if spec.description:
        kw["description"] = spec.description
    # Enum (Literal) carries its own constraint; length/range are redundant/invalid.
    if spec.enum is not None:
        return kw
    if spec.type in ("string", "reference"):
        if spec.min_length is not None:
            kw["min_length"] = spec.min_length
        if spec.max_length is not None:
            kw["max_length"] = spec.max_length
        if spec.pattern is not None:
            kw["pattern"] = spec.pattern
    elif spec.type in ("integer", "number"):
        if spec.minimum is not None:
            kw["ge"] = spec.minimum
        if spec.maximum is not None:
            kw["le"] = spec.maximum
    elif spec.type == "array":
        if spec.min_items is not None:
            kw["min_length"] = spec.min_items
        if spec.max_items is not None:
            kw["max_length"] = spec.max_items
    return kw


def compile_model(name: str, fields: dict[str, FieldSpec]) -> type[BaseModel]:
    """Compile a declarative field map into a strict Pydantic model.

    No code execution occurs: ``create_model`` builds a class from data we have
    already validated. The resulting model rejects unknown fields and enforces
    every declared constraint.
    """
    field_definitions: dict[str, tuple[Any, Any]] = {}
    for fname, spec in fields.items():
        annotation = _annotate(spec)
        constraints = _constraints(spec)
        if spec.required:
            field_definitions[fname] = (annotation, Field(..., **constraints))
        else:
            # Optional[...] is used deliberately at runtime: it is correct for every
            # typing form we build here (Literal[...], List[...], primitives).
            annotation = Optional[annotation]  # type: ignore[assignment]  # noqa: UP007
            field_definitions[fname] = (annotation, Field(default=spec.default, **constraints))

    model_name = f"{name.capitalize()}CI"
    return create_model(  # type: ignore[call-overload]
        model_name,
        __config__=ConfigDict(extra="forbid"),
        **field_definitions,
    )


def compile_from_definition(definition: CITypeDefinition) -> type[BaseModel]:
    return compile_model(definition.name, definition.fields)


def json_schema_for(definition: CITypeDefinition) -> dict[str, Any]:
    """Return the JSON Schema for a CI type (used for docs/clients)."""
    model = compile_from_definition(definition)
    return model.model_json_schema()
