# ADR-0001: Declarative schema registry replaces runtime code upload

- Status: Accepted
- Date: 2026-06-11
- Deciders: Platform / CMDB architecture

## Context

The original CMDBoss exposed `POST /models/upload`, which accepted an uploaded
`.py` file, wrote it to `/tmp`, and executed it with
`importlib.util.spec_from_file_location(...).loader.exec_module(module)` to
discover a Pydantic `BaseModel`. For each discovered model it then **mutated the
live FastAPI app** by adding CRUD routes and stored the class in an in-process
`registered_models` dict.

This design had three disqualifying problems for an enterprise deployment:

1. **Remote code execution.** The upload endpoint executed arbitrary
   attacker-controlled Python in the server process. There was no authentication
   anywhere and CORS was `*`. This is an unauthenticated RCE by design.
2. **Incompatible with multi-worker runtime.** `gunicorn.conf.py` runs
   `(2 × CPU) + 1` worker processes. A model uploaded to worker A registered
   routes and dict entries only in worker A's memory. A subsequent request
   load-balanced to worker B returned `404`. Restarts wiped the in-memory
   registry entirely while orphaning the MongoDB collections.
3. **Runtime route mutation.** Adding Starlette routes after startup is not a
   supported, thread-safe operation and bypasses the cached OpenAPI schema.

## Decision

Replace runtime code upload with a **declarative schema registry**:

- CI types are submitted as **data** — a `CITypeDefinition` of typed
  `FieldSpec`s (type, constraints, enum, array items, references, indexes),
  validated by Pydantic. No code is uploaded or executed.
- Definitions are **persisted in MongoDB** (`_ci_types`), guarded by a unique
  index on `name`. Every worker reads the same registry; registration is a
  deterministic data write.
- A single **generic CRUD surface** (`/api/v1/ci/{type_name}`) serves all types.
  Per-request, the registry **compiles** the stored definition into a strict
  Pydantic model via `pydantic.create_model` (data → class, still no code
  execution) and validates the payload. Compiled models are cached per worker by
  `(name, version)`; the hot lookup path uses a short TTL cache.

The upload-derived example assets (`server.py`, `server_hook.py`) are retired to
`examples/legacy/` and replaced by `examples/server_type.json`.

## Consequences

**Positive**

- The RCE surface is eliminated; ingestion is pure data.
- Correct under any worker count and across restarts — shared state lives in
  Mongo, not per-process memory.
- No runtime route mutation: the route table is fixed; only data changes.
- Strict schemas (`extra="forbid"`) and JSON-Schema emission for clients.
- Natural home for change-auditing, optimistic concurrency and RBAC, which the
  old per-route generation could not host cleanly.

**Negative / trade-offs**

- The field DSL is intentionally less expressive than arbitrary Python models
  (no custom validators yet). Mitigation: the DSL covers constraints, enums,
  arrays and references; custom server-side validators can be added as a
  vetted, named-validator registry later — never as uploaded code.
- A bounded staleness window (`schema_cache_ttl_seconds`, default 5s) exists for
  schema *changes* propagating to all workers' caches. Management reads bypass
  the cache for read-your-writes consistency; only CI write validation tolerates
  the window. Acceptable because schema edits are rare and forward-only.

## Follow-ups

- CI relationship/dependency graph modelling on top of `reference` fields
  (the next milestone).
- Pluggable event-bus backend (Kafka/NATS) behind the existing `EventBus`
  abstraction for the extensible-discovery milestone.
