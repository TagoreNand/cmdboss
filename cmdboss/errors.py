"""
Structured error handling.

Every error leaving the API is rendered as a stable envelope::

    {
      "error": {
        "code": "not_found",
        "message": "Server 'abc' not found.",
        "details": {...},
        "request_id": "..."
      }
    }

Domain code raises :class:`AppError` subclasses; framework/validation errors are
translated by the handlers registered in :func:`install_exception_handlers`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .observability import get_logger, get_request_id

logger = get_logger("cmdboss.errors")


class AppError(Exception):
    """Base class for all domain errors with a stable error code + HTTP status."""

    code: str = "internal_error"
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(self.message)

    def to_envelope(self, request_id: str | None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class NotFoundError(AppError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND
    message = "Resource not found."


class ConflictError(AppError):
    code = "conflict"
    status_code = status.HTTP_409_CONFLICT
    message = "Resource conflict."


class ValidationFailedError(AppError):
    code = "validation_failed"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "Payload failed schema validation."


class BadRequestError(AppError):
    code = "bad_request"
    status_code = status.HTTP_400_BAD_REQUEST
    message = "Bad request."


class UnauthorizedError(AppError):
    code = "unauthorized"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "Authentication required."


class ForbiddenError(AppError):
    code = "forbidden"
    status_code = status.HTTP_403_FORBIDDEN
    message = "Insufficient privileges."


class PreconditionRequiredError(AppError):
    code = "precondition_required"
    status_code = status.HTTP_428_PRECONDITION_REQUIRED
    message = "This operation requires an If-Match revision precondition."


class PreconditionFailedError(ConflictError):
    """Optimistic-concurrency mismatch — surfaced as 409 with the current revision."""

    code = "revision_conflict"
    message = "The resource was modified by another writer (revision mismatch)."


class RateLimitedError(AppError):
    code = "rate_limited"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    message = "Rate limit exceeded."


class PayloadTooLargeError(AppError):
    code = "payload_too_large"
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    message = "Request body too large."


def _json(status_code: int, payload: dict[str, Any], request_id: str | None) -> JSONResponse:
    resp = JSONResponse(status_code=status_code, content=payload)
    if request_id:
        resp.headers["X-Request-ID"] = request_id
    return resp


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers that coerce every failure into the error envelope."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        rid = get_request_id()
        if exc.status_code >= 500:
            logger.error("app_error code=%s msg=%s", exc.code, exc.message, exc_info=True)
        else:
            logger.info("app_error code=%s status=%s msg=%s", exc.code, exc.status_code, exc.message)
        resp = _json(exc.status_code, exc.to_envelope(rid), rid)
        if exc.code == "rate_limited" and "retry_after" in exc.details:
            resp.headers["Retry-After"] = str(int(exc.details["retry_after"]) + 1)
        return resp

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = get_request_id()
        err = ValidationFailedError(
            "Request validation failed.",
            details={"errors": _safe_errors(exc.errors())},
        )
        return _json(err.status_code, err.to_envelope(rid), rid)

    @app.exception_handler(ValidationError)
    async def _handle_pydantic_validation(request: Request, exc: ValidationError) -> JSONResponse:
        rid = get_request_id()
        err = ValidationFailedError(
            "Schema validation failed.",
            details={"errors": _safe_errors(exc.errors())},
        )
        return _json(err.status_code, err.to_envelope(rid), rid)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        rid = get_request_id()
        payload = {
            "error": {
                "code": f"http_{exc.status_code}",
                "message": str(exc.detail),
                "details": {},
                "request_id": rid,
            }
        }
        return _json(exc.status_code, payload, rid)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        rid = get_request_id()
        logger.error("unhandled_exception: %s", exc, exc_info=True)
        err = AppError("An unexpected error occurred.")
        return _json(err.status_code, err.to_envelope(rid), rid)


def _safe_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip non-JSON-serialisable values (e.g. exception ctx) from validation errors."""
    cleaned: list[dict[str, Any]] = []
    for e in errors:
        cleaned.append(
            {
                "loc": list(e.get("loc", [])),
                "msg": e.get("msg", ""),
                "type": e.get("type", ""),
            }
        )
    return cleaned
