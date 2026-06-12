#!/usr/bin/env bash
#
# Publish CMDBoss to GitHub with a readable, milestone-by-milestone history.
# Creates one narrative commit per build step, then force-pushes to `main`
# (replacing the original exec-based baseline on the remote).
#
# Usage:   bash scripts/git_push.sh
# Dry run: PUSH=0 bash scripts/git_push.sh   # create commits but do not push
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/TagoreNand/cmdboss.git}"
PUSH="${PUSH:-1}"

# Move to the repo root (this script lives in scripts/).
cd "$(dirname "$0")/.."

git init -q
git branch -M main 2>/dev/null || true
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REPO_URL"
else
  git remote add origin "$REPO_URL"
fi

# commit "<message>" <path>...   — stages the given paths and commits if non-empty.
commit() {
  local msg="$1"; shift
  git add -- "$@" 2>/dev/null || true
  if ! git diff --cached --quiet; then
    git commit -q -m "$msg"
    echo "  ✓ $msg"
  fi
}

echo "Building narrative history..."

commit "feat(foundation): declarative schema registry (remove RCE), RBAC, durable audit, optimistic concurrency, Docker/CI" \
  cmdboss/__init__.py cmdboss/config.py cmdboss/observability.py cmdboss/errors.py \
  cmdboss/db.py cmdboss/schema.py cmdboss/registry.py cmdboss/repository.py cmdboss/audit.py \
  cmdboss/events.py cmdboss/security.py cmdboss/deps.py cmdboss/utils.py cmdboss/main.py cmdboss/app.py \
  cmdboss/routers/__init__.py cmdboss/routers/types.py cmdboss/routers/ci.py cmdboss/routers/system.py \
  tests/__init__.py tests/conftest.py tests/test_schema.py tests/test_registry.py tests/test_ci_crud.py \
  tests/test_auth_rbac.py tests/test_concurrency.py tests/test_validation.py \
  pyproject.toml requirements.txt requirements-dev.txt .env.example dockerfile docker-compose.yml \
  gunicorn.conf.py log-config.yml config.json .gitignore CONTRIBUTING.md LICENSE \
  .github docs/adr/0001-declarative-schema-registry.md examples

commit "feat(reliability): transactional outbox, keyset pagination, cross-worker cache invalidation, idempotency keys" \
  cmdboss/outbox.py cmdboss/idempotency.py cmdboss/invalidator.py cmdboss/cursor.py \
  tests/_helpers.py tests/test_outbox.py tests/test_pagination_keyset.py tests/test_idempotency.py tests/test_invalidator.py

commit "feat(graph): typed relationship edges, integrity + cardinality, cycle-safe impact analysis, CI delete guard" \
  cmdboss/graph.py cmdboss/graph_schema.py cmdboss/routers/graph.py tests/test_graph.py \
  docs/adr/0002-ci-relationship-graph.md

commit "feat(discovery): provider/adapter framework, source-scoped reconciliation with provenance, outbound webhooks" \
  cmdboss/discovery cmdboss/webhooks.py cmdboss/routers/discovery.py \
  tests/test_discovery.py tests/test_webhooks.py docs/adr/0003-extensible-discovery.md

commit "feat(hardening): outbox dead-letter queue, JWT/OIDC + API-key lifecycle, rate limiting, body cap, security headers" \
  cmdboss/ratelimit.py tests/test_hardening.py docs/adr/0004-production-hardening.md

commit "docs: Mermaid architecture, workflow & pipeline diagrams, engineering log" \
  README.md docs/ARCHITECTURE.md scripts/git_push.sh

# Safety net: catch anything not explicitly grouped above.
git add -A
if ! git diff --cached --quiet; then
  git commit -q -m "chore: remaining project files"
  echo "  ✓ chore: remaining project files"
fi

echo ""
git log --oneline

if [ "$PUSH" = "1" ]; then
  echo ""
  echo "Pushing to $REPO_URL (force — replaces the old baseline)..."
  git push -u origin main --force
  echo "Done."
else
  echo ""
  echo "PUSH=0 set — commits created locally, not pushed."
fi
