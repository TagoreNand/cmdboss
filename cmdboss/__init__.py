"""
CMDBoss — API-driven Configuration Management Database.

A FastAPI + MongoDB backend that exposes a *declarative* schema registry for
Configuration Items (CIs) and an auto-generated, generic CRUD surface with
strict validation, RBAC, optimistic concurrency, durable change-auditing and a
non-blocking event bus.

The package is intentionally modular. Public entrypoint is :func:`cmdboss.app.create_app`.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "2.0.0"
