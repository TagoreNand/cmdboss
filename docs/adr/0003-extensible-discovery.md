# ADR-0003: Extensible discovery + reconciliation

- Status: Accepted
- Date: 2026-06-12
- Deciders: Platform / CMDB architecture

## Context

CMDBoss could store CIs and relationships, but every record had to be written by
hand. A real CMDB is *populated* — continuously — from the infrastructure it
describes (cloud accounts, network scanners, orchestration systems). Directive #3
calls for standardized ingestion pipelines with an adapter pattern so new sources
require minimal core change, and for not blocking the core on heavy external
synchronization.

## Decision

A provider/adapter framework plus a source-scoped reconciliation engine.

- **`DiscoveryProvider` adapter.** Each source is one subclass that returns a
  normalized `DiscoveryResult` (CIs keyed by `external_id`, plus relationships
  referencing endpoints by `(type, external_id)`). Providers are read-only with
  respect to the CMDB. A `ProviderRegistry` maps name → provider; registering one
  is the *only* core touch-point to add a source. The shipped `StaticProvider`
  (config-driven snapshot) is the reference adapter and powers bulk imports;
  cloud providers follow the identical contract (documented skeleton in
  `discovery/base.py`).
- **Reconciliation with provenance.** The engine diffs a provider's desired state
  against the CMDB *for that source only* and converges them through the validated
  repository write paths — so every discovered change is schema-checked, audited,
  optimistic-concurrency-safe, and durably evented. Every discovered record
  carries `_meta.source`, `external_id`, `discovery_run_id`, `last_seen`. Manual
  and discovered data coexist because reconciliation never touches records owned
  by a different (or no) source.
- **Change-minimizing upsert.** `apply_discovery` upserts by `(source,
  external_id)`; unchanged items only refresh `last_seen` (no revision bump, no
  event), so re-syncing a stable inventory of millions of assets is cheap.
- **Missing-item policy.** Source-owned records absent from the latest snapshot
  are, per `on_missing`, either soft-marked (`discovery_status="missing"`,
  default) or deleted (with relationship detach). Stale source-owned edges are
  removed.
- **Run records.** Each run persists an immutable `_discovery_runs` report
  (created/updated/unchanged/deleted/marked-missing counts + per-item errors), so
  one malformed asset never aborts a whole sync.
- **Outbound webhooks.** A bus subscriber — downstream of the durable outbox —
  fans events out to registered HTTP endpoints with HMAC-SHA256 signatures and
  bounded retry. The sender is injectable (testable without a network).

## Consequences

**Positive**

- CMDBoss becomes self-populating; adding AWS/Azure/GCP/on-prem is one adapter
  class, no engine/API/storage change.
- Discovered data inherits all write-path guarantees (validation, audit,
  concurrency, durable events).
- Idempotent, change-minimizing re-sync is safe to schedule frequently.
- Webhooks close the integration loop outward without blocking writes.

**Negative / trade-offs**

- Runs are synchronous in the request for now. Large syncs should move to a
  background task / scheduled job — a drop-in change since `run` returns a
  serializable report and writes a run record.
- Edge endpoints must appear in the same `DiscoveryResult` to resolve; cross-run
  references are reported as `unresolved_endpoint` rather than resolved against
  prior state (a future enhancement).
- Webhook delivery is best-effort (bounded retry) rather than a durable
  per-endpoint queue; a delivery outbox is the next step for at-least-once
  external delivery.

## Follow-ups

- Background/scheduled discovery runs and concurrency guards per source.
- First-party AWS/Azure/GCP adapters; an MCP-tool provider.
- Durable webhook delivery queue with per-endpoint DLQ.
