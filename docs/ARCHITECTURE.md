# CMDBoss — Architecture, Workflows & Pipelines

This document is the detailed, diagram-driven view of CMDBoss after the
enterprise rebuild. Every diagram below is rendered natively by GitHub
(Mermaid). For the *why* behind each major decision, see the
[ADRs](adr/).

- [1. System architecture](#1-system-architecture)
- [2. Component & dependency map](#2-component--dependency-map)
- [3. Request lifecycle (middleware → auth → handler)](#3-request-lifecycle)
- [4. CI write path (atomic CRUD + audit + outbox)](#4-ci-write-path)
- [5. Authentication & RBAC](#5-authentication--rbac)
- [6. Event delivery & outbox dead-letter queue](#6-event-delivery--outbox-dead-letter-queue)
- [7. Relationship graph: integrity & impact analysis](#7-relationship-graph)
- [8. Discovery & reconciliation pipeline](#8-discovery--reconciliation-pipeline)
- [9. Data model (collections)](#9-data-model)
- [10. Deployment topology](#10-deployment-topology)
- [11. Build evolution (milestones)](#11-build-evolution)

---

## 1. System architecture

Every Gunicorn worker is a self-contained FastAPI app. All shared state lives in
MongoDB, so workers never diverge — the root fix for the original design, where a
model uploaded to one worker was invisible to the others.

```mermaid
flowchart TB
    subgraph clients["Clients & external systems"]
        APIC["API client<br/>(X-API-Key or Bearer JWT)"]
        SRC["Discovery sources<br/>(AWS · Azure · GCP · static/on-prem)"]
        HOOK["Webhook receivers<br/>(your services)"]
    end

    subgraph worker["FastAPI app — one per Gunicorn worker"]
        MW["HTTP middleware<br/>request-id · body-size guard · security headers"]
        AUTH["Auth & RBAC<br/>API key / JWT · scopes · rate limit"]
        subgraph routers["Routers (/api/v1)"]
            RT_T["/types"]
            RT_C["/ci"]
            RT_R["/relationships"]
            RT_D["/discovery · /webhooks"]
            RT_A["/admin · /healthz · /metrics"]
        end
    end

    subgraph services["Domain services (per worker)"]
        REG["SchemaRegistry<br/>Mongo-backed + TTL cache"]
        REPO["CIRepository<br/>CRUD · optimistic CC · keyset paging · idempotency"]
        GRAPH["RelationshipRepository<br/>typed edges · integrity · BFS traversal"]
        DISC["DiscoveryService + Reconciler<br/>provider adapters · desired-state diff"]
        AUD["AuditLog<br/>durable lineage"]
        OBX["Outbox + Dispatcher<br/>backoff · dead-letter"]
        BUS["EventBus<br/>async · bounded"]
        WHS["WebhookSubscriber<br/>HMAC-signed"]
        INV["CacheInvalidator<br/>cross-worker"]
    end

    subgraph mongo["MongoDB (shared by all workers)"]
        DB1[("_ci_types")]
        DB2[("ci_&lt;type&gt;")]
        DB3[("_rel_types<br/>_relationships")]
        DB4[("_audit")]
        DB5[("_outbox")]
        DB6[("_api_keys")]
        DB7[("_idempotency")]
        DB8[("_discovery_runs<br/>_webhooks")]
    end

    APIC --> MW --> AUTH --> routers
    SRC --> RT_D
    routers --> REG
    routers --> REPO
    routers --> GRAPH
    routers --> DISC
    REPO --> AUD
    REPO --> OBX
    REPO --> REG
    GRAPH --> AUD
    GRAPH --> OBX
    DISC --> REPO
    DISC --> GRAPH
    AUTH --- DB6
    REG --- DB1
    REPO --- DB2
    REPO --- DB7
    GRAPH --- DB3
    AUD --- DB4
    OBX --- DB5
    DISC --- DB8
    OBX --> BUS
    BUS --> WHS --> HOOK
    BUS --> INV
    INV -. "evict on schema change" .-> REG
```

**Analysis.** The API layer is thin (middleware + auth + routers). Business logic
lives in services that each own one concern and one set of collections. The two
durability lanes are deliberate: **AuditLog** is written synchronously inside the
same unit of work as the data change (never lost), while best-effort fan-out
(webhooks, cache eviction) rides the **EventBus** fed by the durable **Outbox**.

---

## 2. Component & dependency map

How the modules depend on each other — note the graph module has **no** import
dependency on the CI repository (the delete-guard is injected duck-typed), and
discovery sits on top of both repositories.

```mermaid
flowchart LR
    app["app.py<br/>(factory + lifespan)"]
    app --> security & ratelimit & errors & observability
    app --> registry & repository & graph & discovery & outbox & webhooks & invalidator & eventbus

    repository --> registry
    repository --> audit
    repository --> outbox
    repository --> schema
    repository -. "relationship_guard (duck-typed)" .-> graph
    graph --> registry_db["db (collections)"]
    graph --> audit
    graph --> outbox
    discovery --> repository
    discovery --> graph
    webhooks --> eventbus
    outbox --> eventbus
    registry --> schema
    subgraph data["storage layer"]
        db["db.py<br/>client · indexes · transaction()"]
    end
    repository --> db
    graph --> db
    registry --> db
    audit --> db
    outbox --> db
    discovery --> db
```

---

## 3. Request lifecycle

Every request passes the same gauntlet before reaching a handler.

```mermaid
flowchart TB
    A["HTTP request"] --> B["set request-id"]
    B --> C{"Content-Length over max_body_bytes?"}
    C -- yes --> C1["413 payload_too_large"]
    C -- no --> D["route to handler"]
    D --> E["dependency: require(scopes)"]
    E --> F["get_principal → authenticate"]
    F --> G{"authenticated?"}
    G -- no --> G1["401 unauthorized"]
    G -- yes --> H{"rate limit ok?"}
    H -- no --> H1["429 + Retry-After"]
    H -- yes --> I{"has required scope?"}
    I -- no --> I1["403 forbidden"]
    I -- yes --> J["handler runs"]
    J --> K["structured response<br/>+ X-Request-ID + security headers"]
    C1 --> K
    G1 --> K
    H1 --> K
    I1 --> K
```

---

## 4. CI write path

A create/update/delete is one atomic unit of work: the data change, its audit
record, and an outbox event commit together (a real transaction on a replica
set; sequential best-effort otherwise). Events are delivered *after* commit.

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant MW as Middleware
    participant Auth
    participant Router as CI Router
    participant Repo as CIRepository
    participant Mongo
    participant Outbox
    participant Disp as Dispatcher
    participant Bus as EventBus
    participant Hook as Webhooks

    Client->>MW: POST /api/v1/ci/server (Idempotency-Key?)
    MW->>MW: request-id, body-size guard
    MW->>Auth: resolve principal
    Auth->>Auth: API key or JWT, scope ci:write, rate limit
    Auth-->>Router: Principal
    Router->>Repo: create(type, payload, idempotency_key)
    Repo->>Repo: compile model, validate payload
    opt Idempotency-Key present
        Repo->>Mongo: claim key (replay if already done)
    end
    Note over Repo,Mongo: single unit of work (transaction on replica set)
    Repo->>Mongo: insert CI (_meta.revision = 1)
    Repo->>Mongo: insert audit record
    Repo->>Outbox: enqueue ci.created (status=pending)
    Repo-->>Router: serialized CI
    Router-->>Client: 201 Created (ETag "1")
    Disp->>Outbox: poll due events
    Disp->>Bus: publish ci.created
    Bus->>Hook: HMAC-signed delivery (retry/backoff)
```

**Optimistic concurrency (update).** `PUT`/`PATCH` require `If-Match: "<revision>"`.
The repository does a compare-and-swap (`find_one_and_update` filtered on the
expected revision). A mismatch is a `409 revision_conflict` carrying the current
revision — no lost updates.

---

## 5. Authentication & RBAC

```mermaid
flowchart TB
    R["Incoming request"] --> AE{"auth_enabled?"}
    AE -- no --> ANON["anonymous admin (dev/test only)"]
    AE -- yes --> M{"auth_mode"}
    M -- "api_key / both" --> AK["X-API-Key → SHA-256 lookup<br/>active? not expired?"]
    M -- "jwt / both" --> JW["Bearer → verify HS256/RS256<br/>iss/aud, scope claim"]
    AK -- ok --> P["Principal (scopes)"]
    JW -- ok --> P
    AK -- fail --> U["401"]
    JW -- fail --> U
    P --> RL{"rate limit ok?"}
    RL -- no --> T429["429 + Retry-After"]
    RL -- yes --> SC{"has required scope?"}
    SC -- no --> F403["403 (missing scope)"]
    SC -- yes --> H["handler"]
```

Scopes: `types:read/write`, `ci:read/write`, `admin` (superscope). Keys support
expiry, throttled `last_used_at`, rotation and revocation.

---

## 6. Event delivery & outbox dead-letter queue

The outbox guarantees no event is lost on crash and no poison event loops forever.

```mermaid
stateDiagram-v2
    [*] --> pending: write commits (data + audit + outbox)
    pending --> sent: dispatcher publishes to EventBus
    pending --> pending: delivery fails, attempts below max (exponential backoff)
    pending --> dead: attempts reach outbox_max_attempts
    dead --> pending: admin replay
    sent --> [*]
```

Downstream of the bus, the **WebhookSubscriber** delivers each event to
registered endpoints with an HMAC-SHA256 signature and its own bounded retry.

---

## 7. Relationship graph

CIs connect via typed, directed edges governed by declarative contracts. Edge
creation passes an integrity gate; traversal is a depth-bounded, cycle-safe BFS.

```mermaid
flowchart TB
    subgraph create["Create edge — integrity gate"]
        E1["rel_type active?"] --> E2["from/to CI types allowed?"]
        E2 --> E3["both endpoint CIs exist?"]
        E3 --> E4["cardinality satisfied?"]
        E4 --> E5["no dependency cycle introduced?"]
        E5 --> E6["unique? insert (+audit +outbox)"]
        E1 -- no --> X["4xx error"]
        E2 -- no --> X
        E3 -- no --> X
        E4 -- no --> X
        E5 -- no --> X
    end

    subgraph traverse["dependencies / impact — BFS"]
        T1["start CI"] --> T2["query dependency edges<br/>out = dependencies, in = impact"]
        T2 --> T3{"new nodes and depth under max?"}
        T3 -- yes --> T2
        T3 -- no --> T4["nodes + edges + truncated flag"]
    end
```

**Impact analysis** answers "what breaks if this fails?" by walking dependency
edges inward; **dependencies** walks outward. Cycles are rejected on insert, so
both traversals always terminate.

---

## 8. Discovery & reconciliation pipeline

A provider returns a normalized desired-state snapshot; the reconciler converges
the CMDB to it, *for that source only*, through the validated write paths.

```mermaid
flowchart LR
    A["POST /discovery/run<br/>{provider, source, config, on_missing}"] --> B["ProviderRegistry.get(provider)"]
    B --> C["provider.discover(config)"]
    C --> D["DiscoveryResult<br/>items + relationships"]
    D --> E["Reconciler.reconcile(source)"]
    E --> F{"CI exists for (source, external_id)?"}
    F -- no --> G["create (+provenance +audit +outbox)"]
    F -- "yes, changed" --> H["update (+revision bump)"]
    F -- "yes, identical" --> I["unchanged (refresh last_seen)"]
    E --> J{"source-owned but absent now?"}
    J -- "on_missing=mark" --> K["mark_missing"]
    J -- "on_missing=delete" --> L["delete + detach edges"]
    E --> Mr["reconcile edges<br/>create desired, delete stale"]
    G --> N
    H --> N
    I --> N
    K --> N
    L --> N
    Mr --> N["_discovery_runs report"]
    G --> O["events → EventBus → webhooks"]
    H --> O
    K --> O
    L --> O
```

**Provenance** (`_meta.source`, `external_id`, `discovery_run_id`, `last_seen`)
lets manual and discovered data coexist, and makes re-sync of an unchanged
inventory cheap (identical items only touch `last_seen`).

---

## 9. Data model

```mermaid
erDiagram
    CI_TYPE   ||--o{ CI            : defines
    REL_TYPE  ||--o{ RELATIONSHIP  : governs
    CI        ||--o{ RELATIONSHIP  : endpoint
    CI        ||--o{ AUDIT         : lineage
    CI        ||--o{ OUTBOX        : emits
    DISCOVERY_RUN ||--o{ CI        : provenance

    CI_TYPE {
        string name PK
        int version
        bool active
        json fields
    }
    CI {
        objectid id PK
        json user_fields
        string meta_source
        string meta_external_id
        int meta_revision
    }
    REL_TYPE {
        string name PK
        string cardinality
        bool dependency
    }
    RELATIONSHIP {
        objectid id PK
        string rel_type
        bool dependency
        objectid from_id
        objectid to_id
    }
    AUDIT {
        string id PK
        string action
        string entity_type
        json changes
    }
    OUTBOX {
        string id PK
        string status
        int attempts
        datetime next_attempt_at
    }
```

---

## 10. Deployment topology

```mermaid
flowchart LR
    LB["Load balancer / ingress"] --> G1
    LB --> G2
    LB --> G3
    subgraph gunicorn["Gunicorn — uvicorn workers"]
        G1["worker 1"]
        G2["worker 2"]
        G3["worker N"]
    end
    G1 --> RS[("MongoDB<br/>replica set enables transactions")]
    G2 --> RS
    G3 --> RS
    G1 -. webhooks .-> EXT["external receivers"]
    SRC["cloud / on-prem sources"] -. "discovery runs" .-> LB
    PROM["Prometheus"] -. "scrape /metrics" .-> gunicorn
```

Each worker creates its own Motor client after fork and probes transaction
support at startup. A replica set turns on multi-document atomicity
automatically; a standalone node degrades to sequential writes (the outbox still
prevents event loss).

---

## 11. Build evolution

```mermaid
timeline
    title CMDBoss enterprise rebuild (verified at each step)
    Foundation : Declarative registry (RCE removed) : Multi-worker fix : RBAC + audit + optimistic CC : CI/CD : 45 tests green
    Foundation hardening : Transactional outbox : Keyset pagination : Cross-worker cache invalidation : Idempotency keys : 55 tests green
    Relationship graph : Typed edges + integrity : Cardinality + cycle safety : Impact/blast-radius : delete guard : 62 tests green
    Extensible discovery : Provider/adapter framework : Reconciliation + provenance : Outbound webhooks : 69 tests green
    Production hardening : Outbox DLQ + replay : JWT/OIDC + key lifecycle : Rate limit + body cap + headers : 81 tests green
```
