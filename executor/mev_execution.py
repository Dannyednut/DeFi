from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from web3 import Web3
from mev.models import BundleCandidate, BundleSimulation
from mev.policy import ChainMEVPolicy
from mev.simulator import BundleSimulator, SimulationBackend
from .relay import relay_transaction, RelayResult

@dataclass(frozen=True)
class MEVExecutionDecision:
    allowed: bool
    reason: str
    simulation: BundleSimulation | None = None

class MEVExecutionService:
    """Fail-closed mempool backrun execution coordinator.

    It never turns an atomic backrun into a public transaction when the private
    path is unavailable or simulation fails.
    """
    def __init__(self, w3: Web3, simulator: BundleSimulator | None = None):
        self.w3 = w3
        self.chain = ChainMEVPolicy.for_chain(int(w3.eth.chain_id))
        self.simulator = simulator

    def preflight(self, candidate: BundleCandidate) -> MEVExecutionDecision:
        if candidate.chain_id != self.chain.chain_id:
            return MEVExecutionDecision(False, "chain mismatch")
        current = int(self.w3.eth.block_number)
        try:
            self.chain.validate_target(current, candidate.target_block)
        except ValueError as exc:
            return MEVExecutionDecision(False, str(exc))
        if candidate.net_ceiling_profit_wei <= 0:
            return MEVExecutionDecision(False, "not profitable at max fee ceiling")
        if candidate.trigger_raw_tx and self.simulator:
            sim = self.simulator.simulate([candidate.trigger_raw_tx, candidate.searcher_raw_tx], candidate.target_block)
            if not sim.ok:
                return MEVExecutionDecision(False, f"bundle simulation failed: {sim.error}", sim)
            return MEVExecutionDecision(True, "bundle simulation passed", sim)
        if candidate.trigger_raw_tx:
            return MEVExecutionDecision(False, "atomic backrun requires bundle simulation backend")
        return MEVExecutionDecision(True, "single private transaction preflight")

    def submit(self, candidate: BundleCandidate, *, raw_searcher_tx: bytes) -> RelayResult:
        decision = self.preflight(candidate)
        if not decision.allowed:
            return RelayResult(False, "", "mev-preflight", decision.reason)
        # Atomic backruns are private-only. relay_transaction itself also refuses
        # a public fallback when use_flashbots=True.
        return relay_transaction(
            self.w3,
            raw_searcher_tx,
            target_tx=candidate.trigger_raw_tx or b"",
            target_block=candidate.target_block,
            use_flashbots=bool(candidate.trigger_raw_tx),
        )
