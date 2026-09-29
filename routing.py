"""Protocol-neutral route planning and execution capability checks.

The planner deliberately separates *quote support* from *execution support*.
A protocol can participate in opportunity discovery without being sent to a
Solidity executor that does not understand its calldata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class ArbKind(Enum):
    CYCLE = "cycle"
    CROSS_DEX_SPREAD = "cross_dex_spread"
    STABLE_IMBALANCE = "stable_imbalance"
    LIQUIDITY_FRAGMENTATION = "liquidity_fragmentation"
    BACKRUN = "backrun"
    NEW_POOL = "new_pool"
    SPLIT_ROUTE = "split_route"
    NEW_POOL_CROSS_DEX = "new_pool_cross_dex"
    LIQUIDATION = "liquidation"


@dataclass(frozen=True)
class RouteLeg:
    protocol: str
    dex: str
    pool: str
    token_in: str
    token_out: str
    metadata: dict = field(default_factory=dict)
    chain_id: int = 0


@dataclass(frozen=True)
class RoutePlan:
    kind: ArbKind
    chain_id: int
    legs: tuple[RouteLeg, ...]
    amount_in: int
    expected_amount_out: int
    gas_usd: float = 0.0
    execution_supported: bool = False
    execution_reason: str = ""

    @property
    def gross_profit_wei(self) -> int:
        return self.expected_amount_out - self.amount_in

    @property
    def protocols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(x.protocol for x in self.legs))

    def validate(self) -> None:
        from chain_context import assert_same_chain, ChainContext
        chains = [self.chain_id, *[x.chain_id for x in self.legs if x.chain_id]]
        assert_same_chain(chains)
        ctx = ChainContext.from_config(self.chain_id)
        for leg in self.legs:
            if leg.chain_id and leg.chain_id != self.chain_id:
                raise ValueError(f"route leg chain mismatch: {leg.chain_id} != {self.chain_id}")
            ctx.validate_protocol(leg.protocol, leg.dex)


# Existing ArbExec calldata understands only the current V2/V3 route format.
# Keep this explicit until protocol-specific builders are deployed.
# Protocol-specific calldata builders are now available. The deployed legacy
# ArbExec contract is still only native for V2/V3, so callers must distinguish
# "builder available" from "deployed executor available".
ARB_EXEC_NATIVE = frozenset({"uniswap_v2", "uniswap_v3"})
PROTOCOL_BUILDERS = frozenset({"uniswap_v2", "uniswap_v3", "aerodrome_v2", "curve", "balancer_v2", "syncswap"})

def execution_capability(protocols: Iterable[str]) -> tuple[bool, str]:
    unsupported = sorted(set(protocols) - PROTOCOL_BUILDERS)
    if unsupported:
        return False, "execution builder missing: " + ", ".join(unsupported)
    non_native = sorted(set(protocols) - ARB_EXEC_NATIVE)
    if non_native:
        return False, "protocol-specific builder available; deployed ArbExec adapter required: " + ", ".join(non_native)
    return True, "native ArbExec route"
