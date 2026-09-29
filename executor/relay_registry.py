"""Chain-aware private/direct relay registry.

No relay is assumed to exist merely because a chain is EVM compatible.
"""
from __future__ import annotations
from dataclasses import dataclass
from execution_profile import profile_for

@dataclass(frozen=True)
class RelayEndpoint:
    name: str
    chain_id: int
    url: str
    supports_bundle: bool = False
    supports_simulation: bool = False
    enabled: bool = True

class RelayRegistry:
    def __init__(self, endpoints: list[RelayEndpoint] | None = None):
        self._items = list(endpoints or [])

    def for_chain(self, chain_id: int, *, bundles: bool = False) -> list[RelayEndpoint]:
        items = [x for x in self._items if x.enabled and x.chain_id == int(chain_id)]
        if bundles:
            items = [x for x in items if x.supports_bundle]
        return sorted(items, key=lambda x: (not x.supports_simulation, x.name))

    def require_atomic_backrun(self, chain_id: int) -> list[RelayEndpoint]:
        profile = profile_for(chain_id)
        if not profile.atomic_backrun_supported:
            return []
        return self.for_chain(chain_id, bundles=True)
