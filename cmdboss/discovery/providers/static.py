"""
Static discovery provider.

The reference adapter: it returns whatever desired state is handed to it in the
run config. This is what powers bulk inventory imports (a CSV/JSON snapshot
pushed through the discovery pipeline) and is the deterministic provider used in
tests. Real cloud providers follow the exact same contract — see
:class:`cmdboss.discovery.base.DiscoveryProvider`.
"""

from __future__ import annotations

from ..base import DiscoveryProvider, DiscoveryResult


class StaticProvider(DiscoveryProvider):
    name = "static"

    async def discover(self, config: dict) -> DiscoveryResult:
        # config mirrors DiscoveryResult: {"items": [...], "relationships": [...]}
        return DiscoveryResult.model_validate(
            {
                "items": config.get("items", []),
                "relationships": config.get("relationships", []),
            }
        )
