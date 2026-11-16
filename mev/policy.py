from __future__ import annotations
from dataclasses import dataclass
from execution_profile import profile_for

# The unused relay-endpoint record dataclass that used to live here was
# removed by DSS-WO-ENG-017 §3: it had no consumer — relay selection uses
# the chain's FLASHBOTS_RPCS configuration plus this policy
# (executor/relay.py), not a registry of endpoint objects.

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
