"""
Reconciliation engine.

Given a provider's desired-state :class:`DiscoveryResult` for a ``source``, this
diffs it against the CMDB and converges the two — creating, updating, marking, or
deleting CIs and relationships through the validated repository write paths (so
every change is schema-checked, audited, and durably evented). Manual data and
discovered data coexist because reconciliation only ever touches records tagged
with the same ``_meta.source``.

Per-item failures are captured in the returned report rather than aborting the
whole run, so one malformed asset does not block an entire inventory sync.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..config import Settings
from ..errors import AppError, ConflictError
from ..graph import RelationshipRepository
from ..repository import CIRepository
from .base import DiscoveryResult


def _empty_report() -> dict[str, Any]:
    return {
        "ci": {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0, "marked_missing": 0, "errors": []},
        "relationships": {"created": 0, "unchanged": 0, "deleted": 0, "errors": []},
    }


class Reconciler:
    def __init__(
        self, ci_repo: CIRepository, rel_repo: RelationshipRepository, settings: Settings
    ) -> None:
        self._ci = ci_repo
        self._rel = rel_repo
        self._settings = settings

    async def reconcile(
        self, result: DiscoveryResult, *, source: str, run_id: str, on_missing: str, actor: str
    ) -> dict[str, Any]:
        report = _empty_report()
        extid_to_ciid: dict[tuple[str, str], str] = {}

        desired_by_type: dict[str, dict[str, Any]] = defaultdict(dict)
        for item in result.items:
            desired_by_type[item.type][item.external_id] = item

        for type_name, desired in desired_by_type.items():
            existing = await self._ci.iter_source(type_name, source)
            for ext_id, dci in desired.items():
                try:
                    ci_id, action = await self._ci.apply_discovery(
                        type_name, dci.data, source=source, external_id=ext_id, run_id=run_id, actor=actor
                    )
                    extid_to_ciid[(type_name, ext_id)] = ci_id
                    report["ci"][action] += 1
                except AppError as exc:
                    report["ci"]["errors"].append(
                        {"type": type_name, "external_id": ext_id, "code": exc.code, "message": exc.message}
                    )
            # stale: source-owned items no longer present in the desired set
            for ext_id, ci_id in existing.items():
                if ext_id in desired:
                    continue
                try:
                    if on_missing == "delete":
                        await self._ci.delete(
                            type_name, ci_id, expected_revision=None, actor=actor,
                            request_id=run_id, detach=True,
                        )
                        report["ci"]["deleted"] += 1
                    else:
                        await self._ci.mark_missing(type_name, ci_id, run_id=run_id, actor=actor)
                        report["ci"]["marked_missing"] += 1
                except AppError as exc:
                    report["ci"]["errors"].append(
                        {"type": type_name, "id": ci_id, "code": exc.code, "message": exc.message}
                    )

        await self._reconcile_edges(result, source, run_id, actor, extid_to_ciid, report)
        return report

    async def _reconcile_edges(self, result, source, run_id, actor, extid_to_ciid, report) -> None:
        desired_keys: set[tuple[str, str, str]] = set()
        prov = {"source": source, "discovery_run_id": run_id}
        for edge in result.relationships:
            f = extid_to_ciid.get((edge.from_ref.type, edge.from_ref.external_id))
            t = extid_to_ciid.get((edge.to_ref.type, edge.to_ref.external_id))
            if not f or not t:
                report["relationships"]["errors"].append(
                    {"rel_type": edge.rel_type, "code": "unresolved_endpoint",
                     "message": "edge endpoint not present in this discovery result"}
                )
                continue
            desired_keys.add((edge.rel_type, f, t))
            try:
                await self._rel.create_edge(
                    rel_type=edge.rel_type, from_type=edge.from_ref.type, from_id=f,
                    to_type=edge.to_ref.type, to_id=t, attributes=edge.attributes,
                    actor=actor, request_id=run_id, provenance=prov,
                )
                report["relationships"]["created"] += 1
            except ConflictError as exc:
                if exc.code == "duplicate_edge":
                    report["relationships"]["unchanged"] += 1
                else:
                    report["relationships"]["errors"].append(
                        {"rel_type": edge.rel_type, "code": exc.code, "message": exc.message}
                    )
            except AppError as exc:
                report["relationships"]["errors"].append(
                    {"rel_type": edge.rel_type, "code": exc.code, "message": exc.message}
                )

        # stale source-owned edges no longer desired
        existing_edges = await self._rel.iter_source_edges(source)
        for key, edge_id in existing_edges.items():
            if key in desired_keys:
                continue
            try:
                await self._rel.delete_edge(
                    edge_id, expected_revision=None, actor=actor, request_id=run_id
                )
                report["relationships"]["deleted"] += 1
            except AppError as exc:
                report["relationships"]["errors"].append(
                    {"id": edge_id, "code": exc.code, "message": exc.message}
                )
