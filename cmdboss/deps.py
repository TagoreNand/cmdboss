"""FastAPI dependency accessors for state attached during the app lifespan."""

from __future__ import annotations

from fastapi import Request

from .audit import AuditLog
from .config import Settings
from .registry import SchemaRegistry
from .repository import CIRepository


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_registry(request: Request) -> SchemaRegistry:
    return request.app.state.registry


def get_repository(request: Request) -> CIRepository:
    return request.app.state.repository


def get_audit(request: Request) -> AuditLog:
    return request.app.state.audit


def get_rel_registry(request: Request):
    return request.app.state.rel_registry


def get_rel_repository(request: Request):
    return request.app.state.rel_repository


def get_discovery_service(request: Request):
    return request.app.state.discovery_service


def get_webhook_registry(request: Request):
    return request.app.state.webhook_registry
