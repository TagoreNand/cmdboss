# ADR-0002: First-class CI relationship / dependency graph

- Status: Accepted
- Date: 2026-06-11
- Deciders: Platform / CMDB architecture

## Context

CMDBoss stored Configuration Items as flat, isolated collections. A CMDB's
defining capability — modelling how assets depend on one another and answering
"what breaks if this fails?" — was missing. The CI schema had a `reference`
field type, but it was an unenforced string with no integrity, traversal, or
impact analysis.

## Decision

Introduce relationships as **first-class, typed, directed edges** governed by
declarative contracts, alongside the existing CI subsystem.

- **Relationship-type registry.** `RelationshipTypeDefinition` (declarative,
  Pydantic-validated, Mongo-backed, cached) defines each edge type: permitted
  `from_types`/`to_types`, `cardinality`, a `dependency` flag (does *from*
  depend on *to*?), `allow_self`, and an optional `inverse_name`.
- **Edge repository with enforced integrity.** Edges live in `_relationships`
  and are written through the same transaction + outbox + audit path as CIs, so
  edge changes are atomic and durably published (`relationship.created/deleted`).
  Creation enforces: relationship type exists; both endpoint CIs exist
  (referential integrity); endpoint types are permitted; cardinality; edge
  uniqueness; no self-loop unless allowed; and — for dependency edges — **no
  cycle**, keeping the dependency graph a DAG.
- **Traversal.** Depth-bounded, cycle-safe BFS (`_bfs`) over the edge collection
  powers `neighbors` (1 hop), `dependencies` (transitive, follow dependency
  edges out), and `impact` / blast-radius (transitive, follow dependency edges
  in). One indexed query per level; a `truncated` flag signals the depth cap.
- **CI delete guard.** Deleting a CI that participates in relationships is
  blocked (`409 has_relationships`) unless `?detach=true`, which removes its
  edges atomically within the same transaction as the CI delete.

The `dependency` flag is denormalized onto each edge so traversal does not need
a registry lookup and historical edges keep their semantics if a type changes.

## Consequences

**Positive**

- Real dependency modelling and impact analysis — the core CMDB value.
- Edges are contracts, not free-form: integrity, cardinality and acyclicity are
  enforced at write time.
- Reuses the hardened write path (atomic, audited, durably evented).
- No coupling cost on CIs: the delete guard is injected duck-typed, so
  `CIRepository` has no import dependency on the graph module.

**Negative / trade-offs**

- Traversal is application-side BFS. It is portable (works on standalone Mongo
  and mongomock) and indexed per level, but very deep/wide graphs would benefit
  from server-side `$graphLookup`; that is a drop-in optimization behind the
  same method signatures.
- Cardinality and cycle checks read before the write. Under concurrent writers
  they are best-effort unless transactions are enabled (replica set); the unique
  edge index is always the final guard against duplicates.

## Follow-ups

- Optional auto-derivation of edges from CI `reference` fields.
- `$graphLookup`-backed traversal for very large graphs.
- Edge attribute updates (PATCH) and weighted/temporal edges.
