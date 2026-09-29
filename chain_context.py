"""Chain-aware runtime context and route validation.

A route is never allowed to cross EVM networks.  Chain IDs are carried on every
pool/leg and protocol configuration is validated against the active chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from config import CHAINS, ChainConfig, DexConfig


@dataclass(frozen=True)
class ChainContext:
    chain_id: int
    name: str
    native_symbol: str
    wrapped_native: str

    @classmethod
    def from_config(cls, chain_id: int) -> "ChainContext":
        chain = CHAINS.get(int(chain_id))
        if chain is None:
            raise ValueError(f"Unsupported chain_id={chain_id}")
        return cls(chain.chain_id, chain.name, chain.native_symbol, chain.wrapped_native.lower())

    @property
    def config(self) -> ChainConfig:
        return CHAINS[self.chain_id]

    def dex(self, name: str) -> DexConfig:
        needle = name.lower()
        for dex in self.config.dexes:
            if dex.name.lower() == needle:
                return dex
        raise KeyError(f"DEX {name!r} is not configured on chain {self.chain_id}")

    def protocol_enabled(self, protocol: str) -> bool:
        p = protocol.lower()
        return any(d.enabled and d.protocol.lower() == p for d in self.config.dexes)

    def validate_protocol(self, protocol: str, dex: str | None = None) -> None:
        p = protocol.lower()
        if dex is not None:
            d = self.dex(dex)
            if not d.enabled:
                raise ValueError(f"DEX {dex} is disabled on chain {self.chain_id}")
            if d.protocol.lower() != p:
                raise ValueError(
                    f"Protocol mismatch: {dex}={d.protocol}, requested={protocol}, chain={self.chain_id}"
                )
        elif not self.protocol_enabled(p):
            raise ValueError(f"Protocol {protocol} is not enabled on chain {self.chain_id}")


def assert_same_chain(chain_ids: Iterable[int]) -> int:
    ids = {int(x) for x in chain_ids}
    if not ids:
        raise ValueError("route has no chain IDs")
    if len(ids) != 1:
        raise ValueError(f"cross-chain execution route rejected: {sorted(ids)}")
    return ids.pop()
