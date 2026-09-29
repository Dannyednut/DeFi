"""Phase-4 mempool opportunity engine.

Separates observation, classification, simulation and execution gating.  The
engine is intentionally chain-aware: a pending transaction can only influence
pools on the same chain, and a backrun plan must target the same chain and
next eligible block.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from chain_context import ChainContext
from execution_profile import profile_for


class PendingClass:
    SWAP = "swap"
    ROUTER = "router"
    AGGREGATOR = "aggregator"
    LIQUIDITY = "liquidity"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PendingObservation:
    chain_id: int
    tx_hash: str
    sender: str
    to: str
    value: int
    gas_price: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    nonce: int
    data: str
    first_seen_at: float = field(default_factory=time.time)
    source: str = "public"
    classification: str = PendingClass.UNKNOWN

    @property
    def effective_tip_upper_bound(self) -> int:
        if self.max_priority_fee_per_gas:
            return self.max_priority_fee_per_gas
        if self.gas_price:
            return self.gas_price
        if self.max_fee_per_gas:
            return self.max_fee_per_gas
        return 0


@dataclass(frozen=True)
class BackrunCandidate:
    chain_id: int
    trigger_tx_hash: str
    route: Any
    target_block: int
    gross_profit_wei: int
    gas_cost_wei: int
    builder_tip_wei: int
    net_profit_wei: int
    confidence: float
    execution_mode: str
    reason: str = ""

    @property
    def executable(self) -> bool:
        return self.net_profit_wei > 0 and self.confidence >= 0.70


class MempoolOpportunityEngine:
    """Central policy gate for pending-tx/backrun opportunities."""

    def __init__(self, chain_id: int, *, min_confidence: float = 0.70):
        self.chain = ChainContext.from_config(int(chain_id))
        self.min_confidence = min_confidence

    def validate_observation(self, obs: PendingObservation) -> None:
        if int(obs.chain_id) != self.chain.chain_id:
            raise ValueError(f"pending tx chain mismatch: {obs.chain_id} != {self.chain.chain_id}")

    def classify_execution(self, *, has_public_trigger: bool, private_relay_available: bool) -> str:
        profile = profile_for(self.chain.chain_id)
        if has_public_trigger and private_relay_available and profile.atomic_backrun_supported:
            return "atomic_backrun_bundle"
        if private_relay_available and profile.private_submission:
            return "private_single_tx"
        return "public_tx"

    def score(self, *, state_freshness_ms: float, decoded: bool, exact_quote: bool,
              route_supported: bool, trigger_seen: bool) -> float:
        score = 0.0
        score += 0.20 if decoded else 0.0
        score += 0.25 if exact_quote else 0.0
        score += 0.20 if route_supported else 0.0
        score += 0.20 if trigger_seen else 0.0
        score += 0.15 if state_freshness_ms <= 750 else (0.08 if state_freshness_ms <= 2000 else 0.0)
        return min(1.0, score)
