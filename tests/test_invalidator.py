"""Cross-worker cache invalidation: changed types are evicted from the local cache."""

from __future__ import annotations

import datetime as dt

import pytest

from cmdboss.invalidator import RegistryCacheInvalidator

from ._helpers import build_repo

pytestmark = pytest.mark.asyncio


async def test_poll_once_invalidates_changed_types():
    db, settings, registry, repo, outbox = await build_repo()
    # Prime the per-worker cache.
    assert await registry.get_type("server") is not None
    assert "server" in registry._type_cache

    inv = RegistryCacheInvalidator(db.db, registry, settings)
    # Back-date the watermark so the existing type counts as "changed since".
    inv._watermark = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)

    count = await inv.poll_once()
    assert count == 1
    assert "server" not in registry._type_cache  # evicted -> next read goes to Mongo


async def test_poll_once_noop_when_nothing_changed():
    db, settings, registry, repo, outbox = await build_repo()
    inv = RegistryCacheInvalidator(db.db, registry, settings)  # watermark = now
    assert await inv.poll_once() == 0
