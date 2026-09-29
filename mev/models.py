from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class TxFeeModel:
    chain_id: int
    gas_limit: int
    max_fee_per_gas: int = 0
    max_priority_fee_per_gas: int = 0
    gas_price: int = 0

    def effective_ceiling(self) -> int:
        return self.max_fee_per_gas or self.gas_price

@dataclass(frozen=True)
class BundleCandidate:
    chain_id: int
    trigger_hash: str
    searcher_raw_tx: str
    target_block: int
    expected_profit_wei: int
    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    builder_tip_wei: int = 0
    trigger_raw_tx: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.chain_id <= 0: raise ValueError("invalid chain_id")
        if self.target_block <= 0: raise ValueError("target_block must be positive")
        if self.expected_profit_wei < 0: raise ValueError("profit cannot be negative")
        if self.gas_limit <= 0: raise ValueError("gas_limit must be positive")

    @property
    def gas_ceiling_wei(self) -> int:
        return self.gas_limit * self.max_fee_per_gas

    @property
    def net_ceiling_profit_wei(self) -> int:
        return self.expected_profit_wei - self.gas_ceiling_wei - self.builder_tip_wei

@dataclass(frozen=True)
class BundleSimulation:
    ok: bool
    chain_id: int
    target_block: int
    gas_used: int = 0
    gas_fees_wei: int = 0
    coinbase_diff_wei: int = 0
    error: str = ""
    results: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def profitable(self) -> bool:
        return self.ok and self.coinbase_diff_wei >= 0
