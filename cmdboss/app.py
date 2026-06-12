"""
Application factory and lifespan wiring.

``create_app`` builds a fully-wired FastAPI application. All stateful services
(Mongo client, registry, repository, audit, outbox + dispatcher, cache
invalidator, event bus, auth provider) are constructed in the lifespan so they
bind to the running worker's event loop and are cleanly torn down on shutdown.

For tests, an already-constructed :class:`~cmdboss.db.Database` can be injected,
which lets the suite run against ``mongomock_motor`` without a live MongoDB.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .audit import AuditLog
from .config import Settings, get_settings
from .db import (
    Database,
    create_client,
    ensure_core_indexes,
    supports_transactions,
)
from .discovery.reconciler import Reconciler
from .discovery.registry import default_registry
from .discovery.service import DiscoveryService
from .errors import install_exception_handlers
from .events import Event, InMemoryEventBus
from .graph import RelationshipRegistry, RelationshipRepository
from .idempotency import IdempotencyStore
from .invalidator import RegistryCacheInvalidator
from .observability import configure_logging, get_logger, metrics, set_request_id
from .outbox import Outbox, OutboxDispatcher
from .ratelimit import FixedWindowRateLimiter
from .registry import SchemaRegistry
from .repository import CIRepository
from .routers import ci as ci_router
from .routers import discovery as discovery_router
from .routers import graph as graph_router
from .routers import system as system_router
from .routers import types as types_router
from .security import ApiKeyAuthProvider, JwtAuthProvider, ensure_bootstrap_admin
from .webhooks import HttpSender, WebhookRegistry, WebhookSubscriber

logger = get_logger("cmdboss.app")

API_PREFIX = "/api/v1"


def _apply_security_headers(response, settings: Settings) -> None:
    if not settings.security_headers_enabled:
        return
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if settings.hsts_enabled:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    if settings.metrics_enabled:
        metrics.enable()

    owns_db = database is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = database
        if db is None:
            db = Database(create_client(settings), settings.db_name)
        try:
            await ensure_core_indexes(db.db, settings.idempotency_ttl_seconds)
        except Exception:
            logger.error("startup index creation failed; continuing for readiness reporting")
        await ensure_bootstrap_admin(db.db, settings)

        txn_enabled = False
        if settings.use_transactions:
            txn_enabled = await supports_transactions(db.client, settings.db_name)
            logger.info(
                "multi-document transactions %s",
                "enabled" if txn_enabled else "unavailable — using sequential writes",
            )

        registry = SchemaRegistry(db.db, settings)
        audit = AuditLog(db.db)
        bus = InMemoryEventBus(max_queue=settings.event_bus_max_queue)
        outbox = Outbox(db.db)
        idempotency = IdempotencyStore(db.db) if settings.idempotency_enabled else None
        rel_registry = RelationshipRegistry(db.db, settings)
        rel_repository = RelationshipRepository(
            db.db, db.client, rel_registry, audit, outbox, settings, txn_enabled
        )
        repository = CIRepository(
            db.db, db.client, registry, audit, outbox, settings, idempotency, txn_enabled,
            relationship_guard=rel_repository,
        )
        provider_registry = default_registry()
        reconciler = Reconciler(repository, rel_repository, settings)
        discovery_service = DiscoveryService(db.db, provider_registry, reconciler, settings)
        webhook_registry = WebhookRegistry(db.db)

        async def _log_event(event: Event) -> None:
            logger.debug("event %s entity=%s/%s", event.type, event.entity_type, event.entity_id)

        bus.subscribe(_log_event)
        if settings.webhooks_enabled:
            webhook_subscriber = WebhookSubscriber(
                webhook_registry, HttpSender(settings.webhook_timeout_seconds), settings
            )
            bus.subscribe(webhook_subscriber.handle)

        app.state.database = db
        app.state.settings = settings
        app.state.registry = registry
        app.state.audit = audit
        app.state.event_bus = bus
        app.state.repository = repository
        app.state.rel_registry = rel_registry
        app.state.rel_repository = rel_repository
        app.state.discovery_service = discovery_service
        app.state.webhook_registry = webhook_registry
        app.state.auth = ApiKeyAuthProvider(db.db, settings.auth_last_used_throttle_seconds)
        app.state.jwt_auth = (
            JwtAuthProvider(settings) if settings.auth_mode in ("jwt", "both") else None
        )
        app.state.rate_limiter = (
            FixedWindowRateLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds)
            if settings.rate_limit_enabled
            else None
        )
        app.state.outbox = outbox

        await bus.start()
        dispatcher = OutboxDispatcher(db.db, bus, settings) if settings.outbox_enabled else None
        invalidator = (
            RegistryCacheInvalidator(db.db, registry, settings)
            if settings.cache_invalidation_enabled
            else None
        )
        if dispatcher is not None:
            await dispatcher.start()
        if invalidator is not None:
            await invalidator.start()
        app.state.outbox_dispatcher = dispatcher
        app.state.invalidator = invalidator

        logger.info("CMDBoss %s started (env=%s)", __version__, settings.environment)
        try:
            yield
        finally:
            if invalidator is not None:
                await invalidator.aclose()
            if dispatcher is not None:
                await dispatcher.aclose()
            await bus.aclose()
            if owns_db:
                db.close()
            logger.info("CMDBoss stopped")

    app = FastAPI(
        title="CMDBoss",
        version=__version__,
        description=(
            "Declarative, API-driven Configuration Management Database. "
            "Define CI types as data (no code execution), get a strict, audited, "
            "RBAC-protected CRUD surface with optimistic concurrency."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    install_exception_handlers(app)

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        rid = set_request_id(request.headers.get("X-Request-ID"))
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                oversized = int(content_length) > settings.max_body_bytes
            except ValueError:
                oversized = False
            if oversized:
                payload = {
                    "error": {
                        "code": "payload_too_large",
                        "message": f"Request body exceeds {settings.max_body_bytes} bytes.",
                        "details": {"max_body_bytes": settings.max_body_bytes},
                        "request_id": rid,
                    }
                }
                resp = JSONResponse(status_code=413, content=payload)
                resp.headers["X-Request-ID"] = rid
                _apply_security_headers(resp, settings)
                return resp
        start = time.perf_counter()
        status_code = 500
        response = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - start
            if response is not None:
                response.headers["X-Request-ID"] = rid
                _apply_security_headers(response, settings)
            route = request.scope.get("route")
            path_label = getattr(route, "path", request.url.path)
            metrics.incr(
                "http_requests_total",
                {"method": request.method, "path": path_label, "status": str(status_code)},
            )
            metrics.observe(
                "http_request_duration_seconds",
                elapsed,
                {"method": request.method, "path": path_label},
            )
            logger.info(
                "%s %s -> %s (%.1fms)",
                request.method,
                request.url.path,
                status_code,
                elapsed * 1000,
            )

    @app.get("/", tags=["System"], summary="Service banner")
    async def root():
        return {"service": "CMDBoss", "version": __version__, "docs": "/docs", "api": API_PREFIX}

    app.include_router(types_router.router, prefix=API_PREFIX)
    app.include_router(ci_router.router, prefix=API_PREFIX)
    app.include_router(graph_router.router, prefix=API_PREFIX)
    app.include_router(discovery_router.router, prefix=API_PREFIX)
    app.include_router(system_router.router, prefix=API_PREFIX)

    return app


# Module-level ASGI app for Gunicorn/Uvicorn: `gunicorn cmdboss.app:app`.
app = create_app()
