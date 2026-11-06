from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from web3 import Web3
from mev.models import BundleCandidate, BundleSimulation
from mev.policy import ChainMEVPolicy
from mev.simulator import BundleSimulator, SimulationBackend
from executor.authorization import (
    ExecutionAuthorizationBoundary,
    AuthorizedExecution,
)
from .relay import RelayResult
from .testnet_gate import TestnetSafetyError

@dataclass(frozen=True)
class MEVExecutionDecision:
    allowed: bool
    reason: str
    simulation: BundleSimulation | None = None

class MEVExecutionService:
    """Fail-closed mempool backrun execution coordinator.

    AUTHORIZATION (DSS-WO-ENG-015): every submission goes through the unified
    execution authorization boundary's gate-controlled Flashbots session.
    This service never signs, never broadcasts and never accepts externally
    signed transaction bytes: the searcher transaction is handed over as an
    UNSIGNED intent and is signed only inside the gate, after policy
    validation and wallet-custody binding.

    It never turns an atomic backrun into a public transaction when the private
    path is unavailable or simulation fails. Mixed-signer bundles (a third-
    party trigger transaction in front of the searcher transaction) are
    refused: the gate's session model binds every bundle transaction to the
    verified agent wallet, so carrying a victim transaction is out of policy
    until a separately reviewed extension of the capability model says
    otherwise.
    """
    def __init__(
        self,
        w3: Web3,
        simulator: BundleSimulator | None = None,
        authorization: ExecutionAuthorizationBoundary | None = None,
    ):
        self.w3 = w3
        self._authorization = (
            authorization
            if authorization is not None
            else ExecutionAuthorizationBoundary()
        )
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
        known_targets: dict[int, dict[str, str]] | None = None,
    ) -> RelayResult:
        """Submit a backrun candidate through the authorization boundary.

        The searcher transaction must be an UNSIGNED intent dict; it is
        signed exclusively inside the gate's session after the full
        authorization and policy checks. No route exists to hand in
        pre-signed bytes.
        """
        # 1. Authorize through the unified boundary FIRST (fail closed):
        #    explicit testnet opt-in, allowlisted chain, wallet custody.
        try:
            auth: AuthorizedExecution = self._authorization.authorize_execution(
                purpose="mev_backrun_execution", chain_id=candidate.chain_id,
            )
        except TestnetSafetyError as exc:
            return RelayResult(False, "", "refused", f"authorization refused: {exc}")

        # 2. Mixed-signer bundles are out of policy at the authorization
        #    boundary: the gate's wallet-bound session model cannot carry a
        #    third-party signed trigger transaction. Fail closed BEFORE any
        #    further evaluation.
        if candidate.trigger_raw_tx:
            return RelayResult(
                False, "", "refused",
                "atomic backrun with a third-party trigger transaction "
                "cannot be authorized through the wallet-bound bundle "
                "session; requires a separately reviewed gate extension",
            )

        # 3. MEV policy preflight (read-only: chain policy, target block,
        #    profit ceiling, optional bundle simulation evidence).
        decision = self.preflight(candidate)
        if not decision.allowed:
            return RelayResult(False, "", "mev-preflight", decision.reason)

        # 4. Single private transaction via the gate session:
        #    prepare (sign inside the gate) → simulate (mandatory) → submit.
        try:
            session = auth.prepare_flashbots_bundle(
                [unsigned_searcher_tx],
                known_targets=known_targets,
            )
            session.simulate()
            submission = session.submit()
        except TestnetSafetyError as exc:
            return RelayResult(False, "", "flashbots", str(exc))

        audit = submission.get("audit", {}) or {}
        return RelayResult(
            success=True,
            tx_hash=str(audit.get("tx_hash") or "pending"),
            method="flashbots",
            error="",
        )
