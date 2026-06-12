"""
Authentication and role-based access control.

Two pluggable auth schemes behind a common :class:`AuthProvider`:

  * **API keys** — SHA-256 hashed, with optional expiry, throttled ``last_used_at``
    tracking, rotation and revocation.
  * **JWT / OIDC** — Bearer tokens validated with HS256 (shared secret) or RS256
    (public key / PEM), with issuer/audience checks and configurable scope+name
    claims. (A JWKS-URL key source is a documented follow-up.)

``auth_mode`` (api_key | jwt | both) selects which schemes are active.
Authenticated requests are additionally passed through an optional per-principal
rate limiter. ``admin`` remains a superscope implying all others.
"""

from __future__ import annotations

import abc
import datetime as _dt
import hashlib
import secrets
from collections.abc import Iterable
from typing import Any

import jwt
from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from .config import Settings
from .db import API_KEYS_COLLECTION
from .errors import ForbiddenError, NotFoundError, RateLimitedError, UnauthorizedError
from .observability import get_logger
from .utils import parse_object_id, utcnow

logger = get_logger("cmdboss.security")

API_KEY_HEADER = "X-API-Key"

SCOPE_TYPES_READ = "types:read"
SCOPE_TYPES_WRITE = "types:write"
SCOPE_CI_READ = "ci:read"
SCOPE_CI_WRITE = "ci:write"
SCOPE_ADMIN = "admin"

ALL_SCOPES = {SCOPE_TYPES_READ, SCOPE_TYPES_WRITE, SCOPE_CI_READ, SCOPE_CI_WRITE, SCOPE_ADMIN}

ROLE_PRESETS = {
    "reader": [SCOPE_TYPES_READ, SCOPE_CI_READ],
    "writer": [SCOPE_TYPES_READ, SCOPE_CI_READ, SCOPE_CI_WRITE],
    "admin": [SCOPE_ADMIN],
}


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _aware(value: Any) -> Any:
    if isinstance(value, _dt.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


class Principal(BaseModel):
    id: str
    name: str
    scopes: list[str] = Field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return SCOPE_ADMIN in self.scopes

    def has_scope(self, scope: str) -> bool:
        return self.is_admin or scope in self.scopes


class AuthProvider(abc.ABC):
    @abc.abstractmethod
    async def authenticate(self, credentials: str) -> Principal | None: ...


class ApiKeyAuthProvider(AuthProvider):
    def __init__(self, db: AsyncIOMotorDatabase, last_used_throttle_seconds: float = 60.0) -> None:
        self._coll = db[API_KEYS_COLLECTION]
        self._throttle = last_used_throttle_seconds

    async def authenticate(self, credentials: str) -> Principal | None:
        if not credentials:
            return None
        doc = await self._coll.find_one({"key_hash": hash_key(credentials), "active": True})
        if not doc:
            return None
        exp = _aware(doc.get("expires_at"))
        if exp is not None and exp <= utcnow():
            return None
        now = utcnow()
        last = _aware(doc.get("last_used_at"))
        if last is None or (now - last).total_seconds() >= self._throttle:
            await self._coll.update_one({"_id": doc["_id"]}, {"$set": {"last_used_at": now}})
        return Principal(id=str(doc["_id"]), name=doc.get("name", "unknown"), scopes=doc.get("scopes", []))


class JwtAuthProvider(AuthProvider):
    def __init__(self, settings: Settings) -> None:
        self._alg = settings.jwt_algorithm
        self._key = settings.jwt_public_key if self._alg.startswith("RS") else settings.jwt_secret
        self._issuer = settings.jwt_issuer
        self._audience = settings.jwt_audience
        self._scope_claim = settings.jwt_scope_claim
        self._name_claim = settings.jwt_name_claim

    async def authenticate(self, credentials: str) -> Principal | None:
        if not credentials or not self._key:
            return None
        options = {"verify_aud": bool(self._audience)}
        kwargs: dict[str, Any] = {"algorithms": [self._alg], "options": options}
        if self._audience:
            kwargs["audience"] = self._audience
        if self._issuer:
            kwargs["issuer"] = self._issuer
        try:
            claims = jwt.decode(credentials, self._key, **kwargs)
        except Exception as exc:
            logger.info("jwt validation failed: %s", exc)
            return None
        name = str(claims.get(self._name_claim, "jwt-user"))
        sub = str(claims.get("sub", name))
        return Principal(id=sub, name=name, scopes=self._scopes(claims))

    def _scopes(self, claims: dict) -> list[str]:
        raw = claims.get(self._scope_claim)
        if isinstance(raw, str):
            return raw.split()
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return []


async def create_api_key(
    db: AsyncIOMotorDatabase,
    name: str,
    scopes: Iterable[str],
    expires_at: _dt.datetime | None = None,
) -> tuple[str, dict]:
    """Mint a new API key. Returns ``(raw_key, stored_record)``."""
    scopes = list(dict.fromkeys(scopes))
    invalid = set(scopes) - ALL_SCOPES
    if invalid:
        raise ValueError(f"unknown scopes: {sorted(invalid)}")
    raw = secrets.token_urlsafe(32)
    record = {
        "name": name,
        "key_hash": hash_key(raw),
        "prefix": raw[:8],
        "scopes": scopes,
        "active": True,
        "created_at": utcnow(),
        "expires_at": expires_at,
    }
    result = await db[API_KEYS_COLLECTION].insert_one(record)
    record["_id"] = result.inserted_id
    logger.info("api_key created name=%s prefix=%s scopes=%s", name, record["prefix"], scopes)
    return raw, record


async def rotate_api_key(db: AsyncIOMotorDatabase, key_id: str) -> tuple[str, dict]:
    """Issue a fresh secret for an existing key; the old secret stops working."""
    oid = parse_object_id(key_id)
    existing = await db[API_KEYS_COLLECTION].find_one({"_id": oid, "active": True})
    if not existing:
        raise NotFoundError(f"API key '{key_id}' not found.", details={"id": key_id})
    raw = secrets.token_urlsafe(32)
    await db[API_KEYS_COLLECTION].update_one(
        {"_id": oid},
        {"$set": {"key_hash": hash_key(raw), "prefix": raw[:8], "rotated_at": utcnow()}},
    )
    logger.warning("api_key rotated id=%s name=%s", key_id, existing.get("name"))
    return raw, {"id": key_id, "name": existing.get("name"), "prefix": raw[:8], "scopes": existing.get("scopes", [])}


async def revoke_api_key(db: AsyncIOMotorDatabase, key_id: str) -> None:
    oid = parse_object_id(key_id)
    result = await db[API_KEYS_COLLECTION].update_one(
        {"_id": oid, "active": True}, {"$set": {"active": False, "revoked_at": utcnow()}}
    )
    if result.matched_count == 0:
        raise NotFoundError(f"API key '{key_id}' not found.", details={"id": key_id})
    logger.warning("api_key revoked id=%s", key_id)


async def ensure_bootstrap_admin(db: AsyncIOMotorDatabase, settings: Settings) -> None:
    """Create an initial admin key if none exist and a bootstrap key is configured."""
    if not settings.bootstrap_admin_key:
        return
    existing = await db[API_KEYS_COLLECTION].count_documents({"active": True}, limit=1)
    if existing:
        return
    raw = settings.bootstrap_admin_key
    record = {
        "name": "bootstrap-admin",
        "key_hash": hash_key(raw),
        "prefix": raw[:8],
        "scopes": [SCOPE_ADMIN],
        "active": True,
        "created_at": utcnow(),
        "expires_at": None,
    }
    try:
        await db[API_KEYS_COLLECTION].insert_one(record)
        logger.warning("bootstrap admin key created from CMDBOSS_BOOTSTRAP_ADMIN_KEY")
    except Exception as exc:  # pragma: no cover - duplicate race across workers
        logger.info("bootstrap admin key not created (likely already present): %s", exc)


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #


async def _authenticate(request: Request, settings: Settings) -> Principal | None:
    mode = settings.auth_mode
    if mode in ("api_key", "both"):
        key = request.headers.get(API_KEY_HEADER)
        if key:
            principal = await request.app.state.auth.authenticate(key)
            if principal is not None:
                return principal
    if mode in ("jwt", "both"):
        authz = request.headers.get("Authorization", "")
        if authz.startswith("Bearer "):
            jwt_auth = getattr(request.app.state, "jwt_auth", None)
            if jwt_auth is not None:
                principal = await jwt_auth.authenticate(authz[7:])
                if principal is not None:
                    return principal
    return None


async def get_principal(request: Request) -> Principal:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return Principal(id="anonymous", name="anonymous", scopes=[SCOPE_ADMIN])

    principal = await _authenticate(request, settings)
    if principal is None:
        raise UnauthorizedError("Missing or invalid credentials.")

    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        allowed, retry_after = limiter.check(principal.id)
        if not allowed:
            raise RateLimitedError(
                "Rate limit exceeded.", details={"retry_after": round(retry_after, 2)}
            )
    return principal


def require(*scopes: str):
    """Build a FastAPI dependency enforcing that the principal holds *all* scopes."""

    async def _dep(request: Request) -> Principal:
        principal = await get_principal(request)
        missing = [s for s in scopes if not principal.has_scope(s)]
        if missing:
            raise ForbiddenError(
                "Insufficient scope for this operation.",
                details={"required": list(scopes), "missing": missing},
            )
        return principal

    return _dep
