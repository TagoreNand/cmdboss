"""
Provider registry.

A tiny in-process plugin registry mapping provider name -> provider instance.
Registering a new adapter is the *only* core touch-point required to support a
new infrastructure source, satisfying the "minimal core code disruption"
directive. A plugin system or entry-points loader could populate this without
any change to the engine or API.
"""

from __future__ import annotations

from .base import DiscoveryProvider
from .providers import StaticProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, DiscoveryProvider] = {}

    def register(self, provider: DiscoveryProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> DiscoveryProvider | None:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return sorted(self._providers)


def default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(StaticProvider())
    return registry
