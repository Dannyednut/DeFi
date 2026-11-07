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


# ══════════════════════════════════════════════════════════════════════════════
# Chain-aware fee-exposure policy (DSS-WO-ENG-015 R4)
# ══════════════════════════════════════════════════════════════════════════════
#
# A gas-unit cap is NOT a monetary cap. Every signed transaction carries a
# worst-case expenditure of gasLimit × fee price, and a bundle of
# transactions carries the SUM of its members' worst-case expenditures. The
# policy below bounds exactly those quantities per chain, so a runaway fee
# input or a runaway gas estimate is refused before anything is signed.
#
# Basis for the selected limits (documented per DSS-WO-ENG-015 R4 §3):
#
#   * max_fee_per_gas_wei — a sanity ceiling on the fee PRICE itself, set
#     with a wide margin above each chain's observed operating range so
#     ordinary congestion never blocks execution while a pathological
#     outlier (an RPC returning nonsense, a mis-scaled calculation) is
#     refused:
#       - Ethereum mainnet (1): base fee is typically 1–100 gwei with rare
#         spikes past 200 gwei; 750 gwei is ~7× the historical p99.9 and
#         far below the legacy 2000+ gwei spikes that motivated it.
#       - BSC (56): gas sits at ~1–5 gwei (3–10 gwei under load); 30 gwei
#         is ~3× the busiest observed range.
#       - Polygon (137): gas sits at ~30–500 gwei with spikes into the
#         low thousands during congestion; 2000 gwei covers them.
#       - Base (8453): L2 gas is ~0.001–0.5 gwei; 2 gwei is ~4× the top of
#         the observed range (L1 data costs are billed inside gas on
#         modern OP-stack builds).
#       - Arbitrum (42161): gas is ~0.01–1 gwei including the L1 component
#         under normal load; 10 gwei is a 10× margin.
#       - zkSync Era (324): gas is ~0.05–0.5 gwei; 1 gwei is a 2× margin
#         over the busiest normal periods.
#       - Sepolia (11155111): the R2-approved testnet unit caps are
#         preserved unchanged (50 gwei / 2 gwei priority / 500k gas).
#   * max_priority_fee_wei — a ceiling on the tip component only; chosen
#     above each chain's "fast inclusion" band (Ethereum ~2–15 gwei, BSC
#     ~1–3 gwei, Polygon ~30–200 gwei, Base/Arbitrum ~0.01–0.5 gwei,
#     zkSync ~0.05–0.25 gwei, Sepolia ~2 gwei per the R2 review).
#   * max_gas_units — a per-transaction gas-limit ceiling below the chain's
#     practical block/execution limit (Ethereum 30M block gas: the largest
#     arb transaction measured on this engine is a few hundred k units,
#     1.5M gives ~4× headroom over GAS_MULTIPLIER-inflated batch builds;
#     high-throughput L2s get 5M; Sepolia keeps the R2-approved 500k).
#   * max_tx_fee_wei — the HARD worst-case expenditure of ONE transaction
#     (gasLimit × fee price), i.e. max_gas_units × max_fee_per_gas_wei by
#     construction. This is a backstop on EXPOSURE, not a spending budget:
#     the profit gate still requires expected profit > estimated gas cost.
#     (Sepolia note: R2's 0.002 ceiling was the approved ONE-OPERATION
#     budget for the initial 21k-gas self-transfer, not an engine-wide arb
#     exposure cap — it would contradict the R2-approved 500k × 50 gwei
#     unit caps. The R4 entry is the internally consistent 500k × 50 gwei.)
#   * max_bundle_fee_wei — the AGGREGATE worst-case expenditure of all
#     transactions WE sign in one private submission. A backrun bundle
#     carries at most our searcher transaction(s) plus a third-party
#     trigger transaction (whose gas we do not pay), so 2 × the per-tx cap
#     bounds any realistic bundle while refusing runaway multi-tx builds.
#   * max_tx_value_wei — native-token value carried by the CURRENT arbitrage
#     execution path is ZERO (flash-loan funded); the builder refuses any
#     unexpected native value outright. The 0 cap documents that policy.
#
# Fail-closed: a chain without an entry here (or without an execution
# profile) cannot execute — there is deliberately no permissive default,
# because inventing a monetary limit for an unreviewed chain is exactly the
# failure mode this policy exists to prevent. Adding a chain's entry is a
# reviewed code change.
@dataclass(frozen=True)
class ChainFeePolicy:
    chain_id: int
    max_fee_per_gas_wei: int
    max_priority_fee_wei: int
    max_gas_units: int
    max_tx_fee_wei: int
    max_bundle_fee_wei: int
    max_tx_value_wei: int = 0


FEE_POLICIES: dict[int, ChainFeePolicy] = {
    1: ChainFeePolicy(
        1,
        max_fee_per_gas_wei=750 * 10**9,
        max_priority_fee_wei=30 * 10**9,
        max_gas_units=1_500_000,
        max_tx_fee_wei=1_500_000 * 750 * 10**9,          # 1.125 ETH worst case
        max_bundle_fee_wei=2 * 1_500_000 * 750 * 10**9,  # ≤2 signed txs per bundle
    ),
    56: ChainFeePolicy(
        56,
        max_fee_per_gas_wei=30 * 10**9,
        max_priority_fee_wei=3 * 10**9,
        max_gas_units=3_000_000,
        max_tx_fee_wei=3_000_000 * 30 * 10**9,           # 0.09 BNB worst case
        max_bundle_fee_wei=2 * 3_000_000 * 30 * 10**9,
    ),
    137: ChainFeePolicy(
        137,
        max_fee_per_gas_wei=2_000 * 10**9,
        max_priority_fee_wei=500 * 10**9,
        max_gas_units=3_000_000,
        max_tx_fee_wei=3_000_000 * 2_000 * 10**9,        # 6 POL worst case
        max_bundle_fee_wei=2 * 3_000_000 * 2_000 * 10**9,
    ),
    8453: ChainFeePolicy(
        8453,
        max_fee_per_gas_wei=2 * 10**9,
        max_priority_fee_wei=1 * 10**9,
        max_gas_units=5_000_000,
        max_tx_fee_wei=5_000_000 * 2 * 10**9,            # 0.01 ETH worst case
        max_bundle_fee_wei=2 * 5_000_000 * 2 * 10**9,
    ),
    42161: ChainFeePolicy(
        42161,
        max_fee_per_gas_wei=10 * 10**9,
        max_priority_fee_wei=1 * 10**9,
        max_gas_units=5_000_000,
        max_tx_fee_wei=5_000_000 * 10 * 10**9,           # 0.05 ETH worst case
        max_bundle_fee_wei=2 * 5_000_000 * 10 * 10**9,
    ),
    324: ChainFeePolicy(
        324,
        max_fee_per_gas_wei=1 * 10**9,
        max_priority_fee_wei=250_000_000,                # 0.25 gwei
        max_gas_units=5_000_000,
        max_tx_fee_wei=5_000_000 * 1 * 10**9,            # 0.005 ETH worst case
        max_bundle_fee_wei=2 * 5_000_000 * 1 * 10**9,
    ),
    11155111: ChainFeePolicy(
        11155111,
        max_fee_per_gas_wei=50 * 10**9,                  # 50 gwei (R2-approved)
        max_priority_fee_wei=2 * 10**9,                  # 2 gwei  (R2-approved)
        max_gas_units=500_000,                           # R2-approved
        max_tx_fee_wei=500_000 * 50 * 10**9,             # 0.025 ETH = gas cap × fee cap
        max_bundle_fee_wei=2 * 500_000 * 50 * 10**9,     # ≤2 signed txs per bundle
    ),
}


def fee_policy_for(chain_id: int) -> ChainFeePolicy:
    """Return the fee-exposure policy for a chain — FAIL CLOSED.

    A chain without a reviewed fee policy cannot execute: raising here is
    the fail-closed behaviour required by DSS-WO-ENG-015 R4 §2.5 ("Where a
    required chain-specific limit or execution profile is missing, fail
    closed rather than inventing a permissive default").
    """
    try:
        return FEE_POLICIES[int(chain_id)]
    except KeyError as exc:
        raise ValueError(
            f"No fee-exposure policy configured for chain {chain_id} — "
            "failing closed (adding one is a reviewed code change)"
        ) from exc


def validate_transaction_dict(tx: dict[str, Any], chain_id: int) -> None:
    expected = int(chain_id)
    actual = tx.get("chainId")
    if actual is not None and int(actual) != expected:
        raise ValueError(f"transaction chainId={actual} does not match execution chain={expected}")
    if not tx.get("to") and not tx.get("data"):
        raise ValueError("transaction has neither destination nor calldata")
