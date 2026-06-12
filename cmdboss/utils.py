"""Small shared utilities."""

from __future__ import annotations

import datetime as _dt
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from .errors import BadRequestError


def utcnow() -> _dt.datetime:
    """Timezone-aware current UTC timestamp."""
    return _dt.datetime.now(_dt.timezone.utc)


def parse_object_id(value: str) -> ObjectId:
    """Parse a string into an ObjectId, raising a clean 400 on bad input."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise BadRequestError("Invalid id format.", details={"id": value}) from None


def jsonable(value: Any) -> Any:
    """Best-effort conversion of BSON/datetime values to JSON-friendly types."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value
