from __future__ import annotations
from dataclasses import dataclass
from web3 import Web3
from mev.models import BundleCandidate, BundleSimulation
from mev.policy import ChainMEVPolicy
from mev.simulator import BundleSimulator, SimulationBackend
from config import execution_enabled
from .errors import ExecutionRefused
from .signer import sign_transaction
from .relay import relay_transaction, RelayResult

@dataclass(frozen=True)
class MEVExecutionDecision:
    allowed: bool
    reason: str
    simulation: BundleSimulation | None = None

class MEVExecutionService:
    """Fail-closed mempool backrun execution coordinator.

    DSS-WO-ENG-015 R4: this service is part of the EXISTING architecture —
    it builds and signs the searcher transaction through the single
    strengthened path (builder → signer) and submits it through the single
    relay path, which enforces the master execution switch, the chain
    identity and the signed-payload provenance at submission time. The
    chain-specific MEV policy (``mev.policy.ChainMEVPolicy``) decides
    whether an atomic backrun is supported at all.

    It never turns an atomic backrun into a public transaction when the
    private path is unavailable or simulation fails (the relay's private
    path is fail-closed: no public-mempool fallback).
    """
    def __init__(
        self,
        w3: Web3,
        simulator: BundleSimulator | None = None,
    ):
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

    def submit(
        self,
        candidate: BundleCandidate,
        *,
        unsigned_searcher_tx: dict,
    ) -> RelayResult:
        """Submit a backrun candidate through the single execution path.

        The searcher transaction must be an UNSIGNED dict; it is signed
        here through ``executor.signer.sign_transaction`` — the only
        signing path (master switch, chain verification, wallet custody) —
        and submitted through ``executor.relay.relay_transaction`` with the
        third-party trigger transaction in front, gated by the chain MEV
        policy. A private-submission failure never falls back to the
        public mempool.
        """
        # 1. Master execution switch (fail closed).
        if not execution_enabled():
            return RelayResult(False, "", "refused", "execution disabled (EXECUTE_ONCHAIN)")

        # 2. MEV policy preflight (read-only: chain policy, target block,
        #    profit ceiling, optional bundle simulation evidence).
        decision = self.preflight(candidate)
        if not decision.allowed:
            return RelayResult(False, "", "mev-preflight", decision.reason)

        # 3. Sign the searcher transaction through the single signer path.
        try:
            raw_tx = sign_transaction(
                self.w3, unsigned_searcher_tx, chain_id=candidate.chain_id,
            )
        except ExecutionRefused as exc:
            return RelayResult(False, "", "refused", str(exc))

        # 4. Private submission via the relay (no public fallback). The
        #    relay re-verifies the switch, the chain identity and the
        #    signed payload provenance immediately before broadcasting.
        return relay_transaction(
            self.w3,
            raw_tx,
            expected_chain_id=candidate.chain_id,
            use_flashbots=True,
            target_tx=bytes.fromhex(candidate.trigger_raw_tx[2:])
            if candidate.trigger_raw_tx and candidate.trigger_raw_tx.startswith("0x")
            else (bytes.fromhex(candidate.trigger_raw_tx) if candidate.trigger_raw_tx else b""),
            target_block=candidate.target_block,
        )
