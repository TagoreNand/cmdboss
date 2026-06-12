# ADR-0004: Production hardening (auth, rate limiting, outbox DLQ, request safety)

- Status: Accepted
- Date: 2026-06-12
- Deciders: Platform / CMDB architecture

## Context

With the four functional directives built, the gap to "safe to expose" was
operational hardening: the outbox could retry a poison event forever; API keys
had no lifecycle; there was no SSO path, no rate limiting, no request-body cap,
and no security headers.

## Decision

- **Outbox dead-letter queue.** Failed deliveries now back off exponentially via
  ``next_attempt_at``; after ``outbox_max_attempts`` an event moves to a terminal
  ``dead`` status and increments a metric instead of looping. Operators can list
  (`GET /admin/outbox/dead`) and replay (`POST …/replay`) dead events. Due-time is
  filtered in the dispatcher so behaviour is identical on MongoDB and mongomock.
- **JWT / OIDC auth.** A `JwtAuthProvider` validates Bearer tokens (HS256 secret
  or RS256 public key), with issuer/audience checks and configurable scope/name
  claims, behind the existing `AuthProvider` seam. `auth_mode` (`api_key | jwt |
  both`) selects active schemes; `get_principal` tries each. (JWKS-URL key
  rotation is the documented next step.)
- **API-key lifecycle.** Optional `expires_at` enforced at auth; throttled
  `last_used_at` tracking (one write per key per `auth_last_used_throttle_seconds`
  to bound write load); admin **rotate** (new secret, old invalidated) and
  **revoke** endpoints; TTL on creation.
- **Rate limiting.** A per-principal fixed-window limiter enforced inside
  `get_principal`, returning `429` + `Retry-After`. In-process per worker; the
  `check()` contract matches a Redis backend for cross-worker limiting.
- **Request safety.** A body-size guard rejects oversized requests with `413`
  before the handler reads the body, and a security-headers middleware sets
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` (and HSTS when
  served over HTTPS).

## Consequences

**Positive**

- No more infinite poison-event retries; failures are observable and replayable.
- SSO-ready auth without rewriting routes; keys are revocable, rotatable, expiring.
- Basic abuse protection (rate limit, body cap) and standard browser-hardening
  headers are on by default.

**Negative / trade-offs**

- Rate limiting and `last_used_at` are per-worker/in-process; cross-worker
  accuracy and bounded memory at extreme cardinality want a Redis backend.
- JWT validation uses statically configured keys; production OIDC with key
  rotation needs a JWKS fetcher (cache + background refresh).
- The body-size guard trusts `Content-Length`; streaming/chunked uploads without
  it would need a counting wrapper.

## Follow-ups

- JWKS-URL key source with caching; Redis-backed limiter; durable webhook DLQ;
  OpenTelemetry tracing.
