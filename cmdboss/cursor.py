"""
Opaque keyset (cursor) pagination tokens.

Offset/skip pagination is O(n) in the offset and collapses on collections with
millions of documents. Keyset pagination instead remembers the last seen
``(sort_value, _id)`` and asks the database for "rows after this point", which
is an index range scan regardless of depth.

A cursor is a base64url-encoded JSON triple ``{v, i, t}``:
  * ``v`` — the sort field value of the last returned row (ISO string for
    datetimes, raw JSON scalar otherwise);
  * ``i`` — the ``_id`` of the last returned row (tiebreaker for non-unique
    sort fields);
  * ``t`` — value type tag (``dt`` for datetime, ``raw`` otherwise).
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from .errors import BadRequestError


def encode_cursor(sort_value: Any, last_id: Any) -> str:
    if isinstance(sort_value, _dt.datetime):
        payload = {"v": sort_value.isoformat(), "i": str(last_id), "t": "dt"}
    else:
        payload = {"v": sort_value, "i": str(last_id), "t": "raw"}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(token: str) -> tuple[Any, ObjectId]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        payload = json.loads(raw)
        value = payload["v"]
        if payload.get("t") == "dt":
            value = _dt.datetime.fromisoformat(value)
        return value, ObjectId(payload["i"])
    except (binascii.Error, KeyError, ValueError, TypeError, InvalidId) as exc:
        raise BadRequestError("Invalid pagination cursor.", details={"cursor": token}) from exc
