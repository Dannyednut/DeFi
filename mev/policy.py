from __future__ import annotations
from dataclasses import dataclass
from chain_context import ChainContext
from execution_profile import profile_for

@dataclass(frozen=True)
class RelayTarget:
    name: str
    url: str
    chain_ids: frozenset[int]
    supports_bundle_simulation: bool = True
    supports_backrun: bool = True

@dataclass(frozen=True)
class ChainMEVPolicy:
    chain_id: int
    pending_streams: bool
    private_backrun: bool
    bundle_simulation: bool
    max_target_blocks: int = 2
    min_profit_wei: int = 0

    @classmethod
    def for_chain(cls, chain_id: int) -> "ChainMEVPolicy":
        p = profile_for(chain_id)
        return cls(
            chain_id=p.chain_id,
            pending_streams=p.pending_tx_stream,
            private_backrun=p.private_submission,
            bundle_simulation=p.bundle_simulation_supported,
            max_target_blocks=2 if p.target_block_semantics == "next_block" else 1,
        )

    def validate_target(self, current_block: int, target_block: int) -> None:
        if target_block <= current_block or target_block > current_block + self.max_target_blocks:
            raise ValueError(f"invalid target block {target_block} for current block {current_block} on chain {self.chain_id}")
