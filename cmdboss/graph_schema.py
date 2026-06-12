"""
Declarative relationship-type contracts for the CI dependency graph.

Relationships are first-class, typed, directed edges between Configuration
Items. Like CI types, the *types* of relationship are declared as data and
validated by Pydantic, so the graph enforces a contract rather than allowing
arbitrary edges. The ``dependency`` flag marks edges that participate in
dependency/impact analysis (``from`` depends on ``to``).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]

_REL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TYPE_TOKEN_RE = re.compile(r"^([a-z][a-z0-9_]{0,62}|\*)$")
ANY_TYPE = "*"


class RelationshipTypeDefinition(BaseModel):
    """Declarative definition of a relationship (edge) type."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    # Allowed source / target CI types. ["*"] means any type.
    from_types: list[str] = Field(default_factory=lambda: [ANY_TYPE])
    to_types: list[str] = Field(default_factory=lambda: [ANY_TYPE])
    cardinality: Cardinality = "many_to_many"
    # When True, an edge from A to B means "A depends on B"; drives impact analysis.
    dependency: bool = False
    # Optional human label for the reverse direction (e.g. "hosted_by" for "hosts").
    inverse_name: str | None = None
    # Whether an edge may connect a CI to itself.
    allow_self: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not _REL_NAME_RE.match(v):
            raise ValueError("relationship type name must match ^[a-z][a-z0-9_]{0,62}$")
        return v

    @field_validator("from_types", "to_types")
    @classmethod
    def _valid_type_tokens(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("from_types/to_types must list at least one type or '*'")
        cleaned = []
        for t in v:
            t = t.strip().lower()
            if not _TYPE_TOKEN_RE.match(t):
                raise ValueError(f"invalid type token '{t}'")
            cleaned.append(t)
        if ANY_TYPE in cleaned and len(cleaned) > 1:
            raise ValueError("'*' cannot be combined with explicit types")
        return cleaned

    def allows_from(self, ci_type: str) -> bool:
        return ANY_TYPE in self.from_types or ci_type in self.from_types

    def allows_to(self, ci_type: str) -> bool:
        return ANY_TYPE in self.to_types or ci_type in self.to_types
