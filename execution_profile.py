"""Chain-specific execution capabilities and transaction policy.

The arb engine must not assume Ethereum's transaction/mempool/relay model on
all supported networks.  This module centralizes those differences.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any

class MempoolModel(str, Enum):
    PUBLIC = "public"
    PROVIDER_LOCAL = "provider_local"
    SEQUENCER = "sequencer"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class ChainExecutionProfile:
    chain_id: int
    name: str
    mempool_model: MempoolModel
    pending_tx_stream: bool
    atomic_bundle_supported: bool
    bundle_simulation_supported: bool
    private_submission: bool
    supports_eip1559: bool = True
    supports_replacement: bool = True
    target_block_semantics: str = "next_block"
    native_fee_model: str = "eip1559"
    notes: str = ""

    @property
    def atomic_backrun_supported(self) -> bool:
        return self.atomic_bundle_supported and self.bundle_simulation_supported and self.private_submission

PROFILES: dict[int, ChainExecutionProfile] = {
    1: ChainExecutionProfile(1, "ethereum", MempoolModel.PUBLIC, True, True, True, True,
                             notes="Public pending view is provider-local; private bundle execution is relay-dependent."),
    56: ChainExecutionProfile(56, "bsc", MempoolModel.PUBLIC, True, False, False, False,
                              supports_replacement=True, notes="Use direct/private RPC providers configured by operator; no Ethereum bundle assumptions."),
    137: ChainExecutionProfile(137, "polygon", MempoolModel.PUBLIC, True, False, False, False,
                               notes="Pending feeds are provider-local; private submission must be configured explicitly."),
    8453: ChainExecutionProfile(8453, "base", MempoolModel.SEQUENCER, True, False, False, False,
                                notes="Do not model Base as an Ethereum public-mempool bundle network; use supported private orderflow when configured."),
    42161: ChainExecutionProfile(42161, "arbitrum", MempoolModel.SEQUENCER, True, False, False, False,
                                 notes="Sequencer ordering is distinct from Ethereum public mempool ordering."),
    324: ChainExecutionProfile(324, "zksync", MempoolModel.SEQUENCER, True, False, False, False,
                               supports_replacement=True, notes="Era uses a sequencer; pending observations are not equivalent to Ethereum public mempool visibility."),
    11155111: ChainExecutionProfile(11155111, "sepolia", MempoolModel.PUBLIC, True, True, True, True,
                                    notes="Test network; private relay is for testing only."),
}

def profile_for(chain_id: int) -> ChainExecutionProfile:
    try:
        return PROFILES[int(chain_id)]
    except KeyError as exc:
        raise ValueError(f"No execution profile configured for chain {chain_id}") from exc


def validate_transaction_dict(tx: dict[str, Any], chain_id: int) -> None:
    expected = int(chain_id)
    actual = tx.get("chainId")
    if actual is not None and int(actual) != expected:
        raise ValueError(f"transaction chainId={actual} does not match execution chain={expected}")
    if not tx.get("to") and not tx.get("data"):
        raise ValueError("transaction has neither destination nor calldata")
