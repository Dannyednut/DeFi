"""
executor/executor.py
====================
Top-level arbitrage execution orchestrator.

AUTHORIZATION (DSS-WO-ENG-015): every transaction-producing route of this
module is forced through the unified execution authorization boundary
(``executor/authorization.py``), whose signing and submission capabilities
are the ``executor/testnet_gate.py`` orchestrations. This module CANNOT sign
or broadcast on its own:

  * the legacy ``PRIVATE_KEY`` signer (executor/signer.py) was REMOVED —
    there is no import-time key loading, no direct-signing implementation
    and no fallback when the boundary refuses;
  * the executor's private Flashbots relay-identity provisioning
    (relay_signer.json + flashbot middleware attach) was REMOVED — the
    Flashbots transport is the gate-controlled capability/session model
    only;
  * signed raw transaction bytes never exist outside the gate: transactions
    are handed to the boundary as UNSIGNED intents, and the gate signs,
    policy-checks, chain-verifies (before signing AND before submission),
    submits and audits.

Flow for each Opportunity:
  1. authorize_execution through the unified boundary (fail closed)
  2. Extract arb_data and optimal_amount_in from metadata
     - 2-pool: arb_data comes from getProfit() via simulate_cycle()
     - 3+ pool: build arb_data from raw opp fields + golden section amount
  3. Refine optimal amount off-chain (may improve on on-chain estimate)
  4. Select lender (Aave → Balancer → pool flash swap)
  5. Profit gate: verify net profit > gas cost + min threshold
  6. Build EIP-1559 transaction (UNSIGNED)
  7. Submit through the boundary: sign_and_submit (direct RPC) or the
     gate's Flashbots session (prepare → simulate → submit)
  8. Log result

Thread safety: ArbExecutor is designed for single-threaded use within
process_block(). Nonce is fetched fresh per execution via "pending" tag.
"""
from __future__ import annotations

from dataclasses import dataclass

from web3 import Web3
from eth_abi import decode
from eth_utils import to_bytes

from config import (
    CHAIN_ID, DEFAULT_GAS_UNITS, MIN_PROFIT_USD,
    ARB_EXEC_ABI, FLASHBOTS_RPCS, get_chain,
)
from logger import Opportunity
from tvl import token_price
from optimizer import get_optimal_amount, _get_amount_out
from .lender import select_lender, get_balancer_vault
from .builder import (
    build_arb_data, build_execute_tx,
    build_batch_calls, build_execute_batch_tx,
    is_profitable_after_gas,
    MIN_EXECUTION_PROFIT_USD,
)
from .authorization import AuthorizedExecution, ExecutionAuthorizationBoundary
from .testnet_gate import TestnetSafetyError
from .relay import wait_for_receipt
from log import get_logger

log = get_logger("executor")

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="web3")

@dataclass
class ExecutionResult:
    success: bool
    tx_hash: str
    method: str           # relay method used: "direct" | "flashbots" | "refused"
    lender: str           # lender used
    amount_in: int        # actual amount borrowed
    profit_wei: int       # expected profit
    gas_cost_usd: float
    error: str = ""
    profit_usd: int | None = None      # expected profit usd
    summary: dict | None = None # for batched tx: number of failed tx
    receipt: dict | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "tx_hash": self.tx_hash,
            "method": self.method,          # relay method used
            "lender": self.lender,           # lender used
            "amount_in": self.amount_in,        # actual amount borrowed
            "profit_wei": self.profit_wei,       # expected profit
            "gas_cost_usd": self.gas_cost_usd,
            "error": self.error,
            "receipt": self.receipt,
        }


class ArbExecutor:
    """
    Consumes Opportunity objects and executes profitable arbitrage on-chain —
    EXCLUSIVELY through the unified execution authorization boundary.

    Args:
        w3:            Web3 HTTP instance (used for building transactions and
                       reading receipts; signing/submission NEVER happen here).
        chain_id:      Current chain ID (network SELECTION only — it never
                       authorizes execution).
        authorization: The unified execution authorization boundary. Defaults
                       to a fresh environment-resolved boundary; main.py wires
                       ONE shared instance into the executor and the router.
    """

    def __init__(
        self,
        w3: Web3,
        chain_id: int = CHAIN_ID,
        authorization: ExecutionAuthorizationBoundary | None = None,
    ):
        self.w3       = w3
        self.chain_id = chain_id
        self._chain   = get_chain(chain_id)
        self._authorization = (
            authorization
            if authorization is not None
            else ExecutionAuthorizationBoundary()
        )

        self._arb_exec = None
        selected_address = self._chain.arb_exec_address
        if selected_address:
            self._arb_exec = w3.eth.contract(
                address=w3.to_checksum_address(selected_address),
                abi=ARB_EXEC_ABI,
            )
            log.info(f"ArbExecutor ready | contract={selected_address}")

            # Transport SELECTION only (which private-relay capability this
            # chain is configured for). Actual transport USE still requires
            # an authorized execution session from the boundary: the
            # Flashbots path below goes exclusively through the gate's
            # capability/session model, never through locally attached relay
            # middleware or a locally held relay identity.
            relay_uri_available = self.chain_id in FLASHBOTS_RPCS
            try:
                from flashbots import FlashbotsWeb3, flashbot  # noqa: F401
                flashbot_available = True
            except ImportError:
                log.warning("Flashbots package not found; bundle relay disabled")
                flashbot_available = False
            self.use_flashbots = relay_uri_available and flashbot_available

        else:
            log.warning("ArbExecutor: no arb_exec_address configured — execution disabled")

    # ── authorization ────────────────────────────────────────────────────

    def _authorize(self, purpose: str) -> AuthorizedExecution:
        """Authorize one execution context through the unified boundary.

        Raises ``TestnetSafetyError`` (refusal) — callers must treat any
        refusal as a failed execution attempt, never as a success."""
        return self._authorization.authorize_execution(
            purpose=purpose, chain_id=self.chain_id,
        )

    @staticmethod
    def _refused_result(error: str) -> ExecutionResult:
        """A refused attempt is an explicit FAILED result — never a success
        and never a silent skip."""
        return ExecutionResult(
            success=False, tx_hash="", method="refused", lender="",
            amount_in=0, profit_wei=0, gas_cost_usd=0.0, error=error,
        )

    # ── public entry points ──────────────────────────────────────────────

    def submit(self, opp: Opportunity) -> ExecutionResult | None:
        """
        Attempt to execute an arbitrage opportunity.

        Returns ExecutionResult on attempt, None if skipped (pre-flight fail).
        Every signing/submission attempt is authorized through the unified
        boundary first; a refusal yields a failed (success=False) result.
        """
        if not self._arb_exec:
            return None

        try:
            auth = self._authorize("arb_execution")
        except TestnetSafetyError as e:
            log.warning(f"Execution refused by the authorization boundary: {e}")
            return self._refused_result(f"authorization refused: {e}")

        try:
            return self._execute(opp, auth=auth)
        except TestnetSafetyError as e:
            log.warning(f"Execution refused at the authorization boundary: {e}")
            return self._refused_result(f"authorization refused: {e}")
        except Exception as e:
            log.error(f"Executor error: {e}")
            return ExecutionResult(
                success=False, tx_hash="", method="", lender="",
                amount_in=0, profit_wei=0, gas_cost_usd=0.0, error=str(e),
            )

    def submit_batch(self, opps: list[Opportunity], block_number: int) -> ExecutionResult | None:
        """
        Attempt to execute a batch of arbitrage opportunities as ONE combined
        transaction. The batch route uses the SAME authorization boundary as
        the single-opportunity route.
        """
        if not self._arb_exec:
            return None

        try:
            auth = self._authorize("arb_batch_execution")
        except TestnetSafetyError as e:
            log.warning(f"Batch execution refused by the authorization boundary: {e}")
            return self._refused_result(f"authorization refused: {e}")

        try:
            return self._execute_batch(opps, block_number, auth=auth)
        except TestnetSafetyError as e:
            log.warning(f"Batch execution refused at the authorization boundary: {e}")
            return self._refused_result(f"authorization refused: {e}")
        except Exception as e:
            log.error(f"Executor error: {e}")
            return ExecutionResult(
                success=False, tx_hash="", method="", lender="",
                amount_in=0, profit_wei=0, gas_cost_usd=0.0, error=str(e),
            )

    # ── execution pipeline ───────────────────────────────────────────────

    def _execute(self, opp: Opportunity, *, auth: AuthorizedExecution, for_batch = False) -> ExecutionResult | None:
        meta          = opp.metadata or {}
        ad_tuple      = meta.get("arb_data")       # from getProfit() for 2-pool
        fees_pip      = meta.get("fees_pip", [f * 100 for f in meta.get("fees_bps", [])])
        from_mempool  = meta.get("is_mempool", False)
        trigger_tx    = meta.get("trigger_tx", "")
        hop_count     = meta.get("hop_count", len(opp.pools))
        gas_units     = meta.get("gas_units", DEFAULT_GAS_UNITS)
        min_profit    = meta.get("min_profit", 0)
        gas_cost_usd  = opp.gas_cost_usd

        comp_map   = {"low": 1000, "medium": 2000, "high": 3000}
        builder_tip_bps = comp_map.get(opp.competition_level, 1000)

        token_in  = opp.tokens[0] if opp.tokens else ""
        pools     = opp.pools
        tokens    = opp.tokens

        if not token_in or not pools:
            log.debug("Skipping: missing token_in or pools")
            return None

        if from_mempool:
            # ── Step 2: Lender selection ──────────────────────────────────────────
            amount_in = 0
            lender = select_lender(self.w3, self.chain_id, token_in, amount_in, for_mempool=True)

            arb_data = build_arb_data(
                ad_tuple=ad_tuple,
                opp_tokens=list(tokens),
                opp_pools=list(pools),
                opp_fees_pip=fees_pip,
                amount_in=amount_in,
                token_in=token_in,
                mode=lender.mode,
                min_profit=min_profit,
            )
            log.info(f"Executing mempool arb | lender={lender.lender_name} mode={lender.mode} token_in={token_in} hops={hop_count}")
        else:
            # ── Step 1: Determine optimal amount ─────────────────────────────────
            on_chain_amount = int(ad_tuple[0]) if ad_tuple else 0

            if on_chain_amount > 0:
                # 2-pool: use on-chain amount
                amount_in = on_chain_amount
                profit_wei    = meta.get("profit_wei", 0)
            else:
                # 3+ pool: golden section only
                try:
                    off_chain = get_optimal_amount(
                        self.w3, list(pools), list(tokens), fees_pip, mode=0
                    )
                    amount_in  = off_chain.amount_in
                    profit_wei = off_chain.expected_profit
                except Exception as e:
                    log.debug(f"Off-chain optimizer failed for {hop_count}-hop: {e}")
                    return None

            if amount_in <= 0:
                log.debug("Skipping: amount_in=0 after optimization")
                return None

            if profit_wei <= 0:
                log.debug("Skipping: profit_wei=0")
                return None

            # ── Step 2: Lender selection ──────────────────────────────────────────
            lender = select_lender(self.w3, self.chain_id, token_in, amount_in)
            log.debug(f"Lender: {lender.lender_name} mode={lender.mode}")

            # ── Step 3: Profit gate ───────────────────────────────────────────────
            if not is_profitable_after_gas(
                profit_wei, token_in, gas_cost_usd,
                min_profit_usd=max(MIN_PROFIT_USD, MIN_EXECUTION_PROFIT_USD),
            ):
                log.debug(f"Skipping: profit below gas threshold (gas=${gas_cost_usd:.3f})")
                return None

            # ── Step 4: Build ArbData ─────────────────────────────────────────────
            # for external flash loans (mode 0), borrowPool is ignored by contract

            arb_data = build_arb_data(
                ad_tuple=ad_tuple,
                opp_tokens=list(tokens),
                opp_pools=list(pools),
                opp_fees_pip=fees_pip,
                amount_in=amount_in if not (lender.mode==1 or for_batch) else _get_amount_out(self.w3, amount_in, token_in, pools[0], fees_pip[0]),
                token_in=token_in,
                mode=1 if for_batch else lender.mode,
                min_profit=min_profit,
            )

        # ── Pre-flight simulation through the boundary's gate session ────
        sim_result: dict = {}
        if self.use_flashbots and not for_batch:
            probe_tx = build_execute_tx(
                w3=self.w3,
                arb_exec=self._arb_exec,
                arb_data=arb_data,
                force_balancer=lender.force_balancer,
                builder_tip_bps=builder_tip_bps,
                sender=auth.sender,
                gas_units=gas_units,
            )
            try:
                sim_result = self.execution_simulation(
                    auth,
                    probe_tx,
                    target_block=opp.block_number + 1,
                    target_tx=trigger_tx if from_mempool else "",
                )
            except TestnetSafetyError as e:
                log.debug(f"Skipping: boundary simulation refused: {e}")
                return [None] * 4 if for_batch else None
            if not sim_result or not sim_result.get("success"):
                error = sim_result.get("error") if sim_result else "Unknown simulation error"
                log.debug(f"Skipping: simulation failed | error={error}")
                return [None] * 4 if for_batch else None

            log.debug(f"Simulation success | gas_used={sim_result.get('gas_used')} gas_fee_wei={sim_result.get('gas_fee_wei')}")

            arb_data["minProfit"] = int(sim_result.get("gas_fee_wei", 0) * 1.2)  # increase minProfit to account for simulation uncertainty
            gas_units = sim_result.get("gas_used", gas_units)
        # for_batch members skip relay simulation: each member's prepared
        # payload is never submitted on its own (the batch is ONE combined
        # transaction), so preparing per-member signed payloads would mint
        # one-use authorizations for payloads that are never used.

        if for_batch:
            return arb_data, gas_units, gas_cost_usd, builder_tip_bps

        # ── Step 5: Build UNSIGNED transaction ────────────────────────────────
        tx = build_execute_tx(
            w3=self.w3,
            arb_exec = self._arb_exec,
            arb_data=arb_data,
            force_balancer=lender.force_balancer,
            builder_tip_bps=builder_tip_bps,
            sender=auth.sender,
            gas_units=gas_units,
        )

        # ── Step 6: Sign + relay EXCLUSIVELY through the boundary ────────────
        log.info(
            f"Executing arb | lender={lender.lender_name} mode={lender.mode} "
            f"amount={amount_in} profit_wei={profit_wei} "
            f"gas_usd=${gas_cost_usd:.3f} hops={hop_count}"
        )

        if self.use_flashbots:
            return self._submit_via_flashbots_session(
                auth, tx, lender=lender.lender_name, amount_in=amount_in,
                profit_wei=profit_wei, gas_cost_usd=gas_cost_usd,
            )
        return self._submit_via_boundary_direct(
            auth, tx, lender=lender.lender_name, amount_in=amount_in,
            profit_wei=profit_wei, gas_cost_usd=gas_cost_usd,
        )

    def _execute_batch(self, opps: list[Opportunity], block_number, *, auth: AuthorizedExecution) -> ExecutionResult | None:
        # ── Step 5: Build transaction (members prepared WITHOUT signing) ─────
        calls, total_gas, total_gas_usd = build_batch_calls(
            lambda opp, for_batch: self._execute(opp, auth=auth, for_batch=for_batch),
            self._arb_exec, opps,
        )
        tx = build_execute_batch_tx(
            w3=self.w3,
            arb_exec=self._arb_exec,
            calls=calls,
            sender=auth.sender,
            gas_units=total_gas,
        )

        # ── Step 6: Sign + relay EXCLUSIVELY through the boundary ────────────
        log.info(
            f"Batch execution |opps_count={len(opps)} lender=pool_flash_swap mode=1 "
            f"gas_usd=${total_gas_usd:.3f}"
        )

        if self.use_flashbots:
            session = auth.prepare_flashbots_bundle(
                [tx],
            )
            session.simulate()  # mandatory before submission (gate-enforced)
            submission = session.submit()
            audit = submission.get("audit", {}) or {}
            log.info(
                f"✅ Batch arb submitted via gate session | bundle={str(audit.get('tx_hash'))[:16]}"
            )
            return ExecutionResult(
                success=True,
                tx_hash=str(audit.get("tx_hash") or ""),
                method="flashbots",
                amount_in=None,
                lender="pool_flash_swap",
                profit_wei=0,
                profit_usd=0,
                gas_cost_usd=total_gas_usd,
                error="",
            )

        record = auth.sign_and_submit(tx)
        tx_hash = str(record.get("tx_hash") or "")
        outcome = str(record.get("outcome") or "")

        # ── Step 7: Receipt (direct only) ────────────────────────────────────
        receipt = None
        batch_result = {}
        summary = None
        success = outcome in ("mined", "submitted", "receipt_unavailable")
        if success and tx_hash:
            receipt = wait_for_receipt(self.w3, tx_hash, timeout=30)
            if receipt:
                status = receipt.get("status", 0)
                if status == 1:
                    batch_result = self.extract_batch_result(receipt) or {}
                    summary = batch_result.get('summary')
                    log.info(f"✅ Batch Arb confirmed | tx={tx_hash[:16]} | summary: ({summary.successful} successful, {summary.failed} failed)" if summary else f"✅ Batch Arb confirmed | tx={tx_hash[:16]}")
                else:
                    log.warning(f"❌ Batch Arb reverted on-chain | tx={tx_hash[:16]}")
                    success = False
        return ExecutionResult(
            success=success,
            tx_hash=tx_hash,
            method="direct",
            amount_in=None,
            lender="pool_flash_swap",
            profit_wei=batch_result.get("est_profit_wei") if batch_result else 0,
            profit_usd=batch_result.get("est_profit_usd") if batch_result else 0,
            gas_cost_usd=batch_result.get("gas_usd") if batch_result else total_gas_usd,
            summary=summary if batch_result else None,
            error=str(record.get("error") or ""),
            receipt=receipt,
        )

    # ── boundary-mediated submission ─────────────────────────────────────

    def _submit_via_boundary_direct(
        self,
        auth: AuthorizedExecution,
        tx: dict,
        *,
        lender: str,
        amount_in: int,
        profit_wei: int,
        gas_cost_usd: float,
    ) -> ExecutionResult:
        """Direct-RPC submission through the unified boundary's gate
        orchestration: chain verification before signing AND immediately
        before submission, transaction policy, wallet custody binding,
        submission, receipt and audit — all inside the gate. Signed raw
        bytes never exist outside it."""
        record = auth.sign_and_submit(tx)
        tx_hash = str(record.get("tx_hash") or "")
        outcome = str(record.get("outcome") or "")

        success = outcome in ("mined", "submitted", "receipt_unavailable")

        # ── Receipt (direct only) ────────────────────────────────────────────
        receipt = None
        if success and tx_hash:
            receipt = wait_for_receipt(self.w3, tx_hash, timeout=30)
            if receipt:
                status = receipt.get("status", 0)
                if status == 1:
                    profit = self.extract_profit(receipt)
                    log.info(f"✅ Arb confirmed | tx={tx_hash[:16]} profit_wei={profit}")
                else:
                    log.warning(f"❌ Arb reverted on-chain | tx={tx_hash[:16]}")
                    success = False

        return ExecutionResult(
            success=success,
            tx_hash=tx_hash,
            method="direct",
            lender=lender,
            amount_in=amount_in,
            profit_wei=profit_wei,
            gas_cost_usd=gas_cost_usd,
            error=str(record.get("error") or ""),
            receipt=receipt,
        )

    def _submit_via_flashbots_session(
        self,
        auth: AuthorizedExecution,
        tx: dict,
        *,
        lender: str,
        amount_in: int,
        profit_wei: int,
        gas_cost_usd: float,
    ) -> ExecutionResult | None:
        """Flashbots submission through the gate-controlled capability/
        session model: prepare (signs inside the gate) → simulate (mandatory)
        → submit (one-use authorization). The relay is a transport only.

        The submitted payload is always the exact simulated payload: the
        minProfit adjustment is baked into THIS transaction before its
        session is prepared, so there is no unsubmitted-payload gap."""
        try:
            session = auth.prepare_flashbots_bundle([tx])
        except TestnetSafetyError:
            # preparation refusal — audited by the gate; a REFUSED attempt
            raise
        try:
            session.simulate()
            submission = session.submit()
        except TestnetSafetyError as e:
            # simulation failure/refusal → skip (fail closed, audited);
            # submission failure → failed result
            log.warning(f"Gate session refused or failed: {e}")
            return None
        audit = submission.get("audit", {}) or {}
        tx_hash = str(audit.get("tx_hash") or "")
        log.info(f"✅ Arb submitted via gate session | bundle={tx_hash[:16]}")
        return ExecutionResult(
            success=True,
            tx_hash=tx_hash,
            method="flashbots",
            lender=lender,
            amount_in=amount_in,
            profit_wei=profit_wei,
            gas_cost_usd=gas_cost_usd,
            error="",
        )

    def execution_simulation(
        self,
        auth: AuthorizedExecution,
        tx: dict,
        target_block: int = None,
        target_tx: str = "") -> dict:
        """
        Simulate a BUILT transaction through the boundary's gate session
        (Flashbots transport only). The transaction is signed INSIDE the
        gate by the prepared session — this method never signs locally and
        never returns signed bytes.

        Used as a pre-flight refinement of the profit estimate. Simulation
        failures/refusals raise ``TestnetSafetyError`` (audited by the gate)
        or return a failure dict for revert-style outcomes.
        """
        if not self.use_flashbots:
            log.warning("Simulation requested but the Flashbots transport is not enabled")
            return {
                "success": False,
                "error": "Flashbots transport not enabled",
            }

        if target_tx:
            # A third-party trigger transaction cannot be carried through
            # the gate's wallet-bound bundle model: every signed transaction
            # in a prepared bundle must be signed by the verified agent
            # wallet. Mixed-signer (victim + searcher) bundles are refused
            # fail-closed; enabling them requires a separately reviewed
            # extension of the gate's capability model (DSS-WO-ENG-015).
            return {
                "success": False,
                "error": (
                    "atomic backrun bundles with third-party trigger "
                    "transactions are not executable through the "
                    "authorization boundary's wallet-bound Flashbots "
                    "session"
                ),
            }

        session = auth.prepare_flashbots_bundle([tx])
        try:
            sim = session.simulate()
        except TestnetSafetyError:
            raise  # audited by the session; fail closed
        results = sim.get("results") or []
        gas_fee_wei = 0
        if results:
            try:
                gas_fee_wei = int(results[0].get("gasFees", 0) or 0)
            except (TypeError, ValueError):
                gas_fee_wei = 0
        return {
            "success": True,
            "gas_used": sim.get("totalGasUsed"),
            "gas_fee_wei": gas_fee_wei,
            "target_block": sim.get("blockNumber"),
            "target_raw_tx": None,
        }

    def _best_amount(
        self,
        ad_tuple: tuple,
        on_chain_amount: int,
        off_chain_amount: int,
        fees_pip: list[int],
    ) -> tuple[int, int]:
        """
        Compare on-chain and off-chain optimal amounts via getProfit(),
        return (amount_in, profit_wei) for whichever is better.
        """
        results = []

        for amount in {on_chain_amount, off_chain_amount}:
            if amount <= 0:
                continue
            try:
                # Build minimal arb_data with this amount for verification
                arb_data_check = {
                    "amountIn":   amount,
                    "minProfit":  0,
                    "tokens":     [self.w3.to_checksum_address(t) for t in ad_tuple[2]],
                    "pools":      [self.w3.to_checksum_address(p) for p in ad_tuple[3]],
                    "fees":       list(ad_tuple[4]),
                    "tokenIn":    self.w3.to_checksum_address(ad_tuple[5]),
                    "mode":       0,
                }
                _, profit = self._arb_exec.functions.yieldOut(arb_data_check).call()
                if profit > 0:
                    results.append((amount, profit))
            except Exception as e:
                log.debug(f"getProfit verify failed for amount={amount}: {e}")

        if not results:
            return 0, 0

        # Pick the amount with highest verified profit
        return max(results, key=lambda x: x[1])

    def extract_profit(self, receipt):
        """Extract profit from transaction receipt"""
        try:
            event = self._arb_exec.events.DONE().process_receipt(receipt)
            if event:
                args = event[0].args
                return args.amt
            return 0
        except Exception as e:
            log.error(f"Error parsing profit event: {e}")
            return 0

    def extract_batch_result(self, receipt):
        """
        Returns a list of profits from all successful arbs in the batch.
        Each entry contains: (profit_amount, profit_token)
        """
        profits = []

        # 1. Get all DONE events emitted by ArbExec during this tx
        done_events = self._arb_exec.events.DONE().process_receipt(receipt)
        wrapped_native = self._chain.wrapped_native
        native_price = token_price(wrapped_native)
        est_profit_usd = 0
        est_profit_wei = 0
        for event in done_events:
            args = event.args
            amount_formatted = float(Web3.from_wei(args.amt, 'ether')) if args.token == wrapped_native else None
            est_profit_usd += amount_formatted * native_price
            est_profit_wei += args.amt
            profits.append({
                'profit_token': args.token,
                'amount': args.amt,           # This is the profit in wei
                'amount_formatted': amount_formatted
            })

        # 2. Get the summary from BatchResult event (optional)
        batch_event = self._arb_exec.events.BATCH().process_receipt(receipt)
        summary = None
        if batch_event:
            summary = batch_event[0].args   # successful, failed

        # Also get the gas the amount of gas consumed
        gas_used = float(receipt.get("gasUsed", 0))
        gas_price = float(receipt.get("effectiveGasPrice", 0))
        gas_usd = float(Web3.from_wei(int(gas_used * gas_price), 'ether')) * float(native_price or 0)
        return {
            'profits': profits,
            'total_successful': len(profits),
            'est_profit_usd': est_profit_usd,
            'est_profit_wei': est_profit_wei,
            'gas_usd': gas_usd,
            'summary': summary
        }
