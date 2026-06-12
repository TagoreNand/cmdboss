"""
Runtime configuration.

Resolution order (highest priority first):
    1. Environment variables prefixed ``CMDBOSS_`` (and a few well-known unprefixed ones).
    2. A ``config.json`` file (legacy compatibility) discovered next to the repo root.
    3. Hard-coded safe defaults.

Settings are immutable once loaded and cached process-wide via :func:`get_settings`.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = parent of this package directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_CONFIG_PATH = _REPO_ROOT / "config.json"


def _load_legacy_config() -> dict[str, Any]:
    """Load legacy config.json if present. Never raises on a missing file."""
    path_str = os.getenv("CMDBOSS_CONFIG_PATH")
    path = Path(path_str) if path_str else _LEGACY_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    # Drop comment / annotation keys used in the legacy file.
    return {k: v for k, v in raw.items() if not k.startswith("_")}


class Settings(BaseSettings):
    """Strongly-typed application settings."""

    model_config = SettingsConfigDict(
        env_prefix="CMDBOSS_",
        env_file=os.getenv("CMDBOSS_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Service identity ---
    app_name: str = "CMDBoss"
    environment: str = Field(default="development", description="deployment environment label")

    # --- Network ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Mongo ---
    mongo_uri: str = "mongodb://localhost:27017"
    db_name: str = "cmdboss"
    mongo_max_pool_size: int = 100
    mongo_min_pool_size: int = 0
    mongo_server_selection_timeout_ms: int = 5000

    # --- Logging / observability ---
    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True

    # --- Security ---
    # When True (default) every non-public route requires a valid API key.
    auth_enabled: bool = True
    # Optional bootstrap admin key, created on first startup if no keys exist.
    bootstrap_admin_key: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Registry / CI behaviour ---
    schema_cache_ttl_seconds: float = 30.0
    default_page_size: int = 50
    max_page_size: int = 500

    # --- Event bus ---
    event_bus_max_queue: int = 10_000

    # --- Reliability / hardening ---
    # Attempt MongoDB multi-document transactions when the deployment supports
    # them (replica set). Falls back to sequential writes otherwise.
    use_transactions: bool = True
    # Transactional outbox -> durable, atomic event emission.
    outbox_enabled: bool = True
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 100
    # Cross-worker schema-cache invalidation.
    cache_invalidation_enabled: bool = True
    cache_invalidation_poll_seconds: float = 2.0
    # Idempotency-Key support for safe create retries.
    idempotency_enabled: bool = True
    idempotency_ttl_seconds: int = 86_400

    # --- Relationship graph ---
    graph_max_depth: int = 10

    # --- Discovery / webhooks ---
    discovery_default_on_missing: str = "mark"  # "mark" | "delete"
    webhooks_enabled: bool = True
    webhook_timeout_seconds: float = 5.0
    webhook_max_attempts: int = 3

    # --- Outbox reliability ---
    outbox_max_attempts: int = 5
    outbox_backoff_base_seconds: float = 2.0
    outbox_backoff_cap_seconds: float = 300.0

    # --- Auth (extended) ---
    auth_mode: str = "api_key"  # api_key | jwt | both
    auth_last_used_throttle_seconds: float = 60.0
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"  # HS256 | RS256
    jwt_public_key: str | None = None  # PEM for RS256
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_scope_claim: str = "scope"  # space-delimited string or list
    jwt_name_claim: str = "sub"

    # --- Rate limiting (per principal, in-process / per worker) ---
    rate_limit_enabled: bool = False
    rate_limit_requests: int = 600
    rate_limit_window_seconds: float = 60.0

    # --- Request hardening ---
    max_body_bytes: int = 1_048_576  # 1 MiB
    security_headers_enabled: bool = True
    hsts_enabled: bool = False  # enable when served over HTTPS

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: Any) -> Any:
        # Allow comma-separated origins from a single env var.
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [o.strip() for o in stripped.split(",") if o.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Legacy ``config.json`` values are used as defaults but are always
    overridden by environment variables.
    """
    legacy = _load_legacy_config()
    # Map a few legacy keys onto Settings field names. Init kwargs have the
    # *highest* precedence in pydantic-settings, so to preserve "env wins over
    # config.json" we only forward a legacy value when its env var is unset.
    mapped: dict[str, Any] = {}
    for key in ("mongo_uri", "db_name", "host", "port", "log_level", "cors_origins"):
        env_name = f"CMDBOSS_{key.upper()}"
        if key in legacy and env_name not in os.environ:
            mapped[key] = legacy[key]
    return Settings(**mapped)


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests)."""
    get_settings.cache_clear()
