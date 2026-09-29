"""
executor/executor.py
====================
Top-level arbitrage execution orchestrator.

Flow for each Opportunity:
  1. Extract arb_data and optimal_amount_in from metadata
     - 2-pool: arb_data comes from getProfit() via simulate_cycle()
     - 3+ pool: build arb_data from raw opp fields + golden section amount
  2. Refine optimal amount off-chain (may improve on on-chain estimate)
  3. Select lender (Aave → Balancer → pool flash swap)
  4. Profit gate: verify net profit > gas cost + min threshold
  5. Build EIP-1559 transaction
  6. Sign
  7. Relay (direct or Flashbots bundle)
  8. Log result

Thread safety: ArbExecutor is designed for single-threaded use within
process_block(). Nonce is fetched fresh per execution via "pending" tag.
"""
from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass

from web3 import Web3
from eth_account import Account
from eth_abi import decode
from eth_utils import to_bytes

from config import (
    CHAIN_ID, DEFAULT_GAS_UNITS, MIN_PROFIT_USD,
    ARB_EXEC_ABI, FLASHBOTS_RPCS, get_chain, get_extended_arb_exec_address,
)
from logger import Opportunity
from tvl import token_price
from optimizer import get_optimal_amount, _get_amount_out
from utils.serializer import serialize
from .lender import select_lender, get_balancer_vault
from .builder import (
    build_arb_data, build_execute_tx,
    build_batch_calls, build_execute_batch_tx,
    is_profitable_after_gas,
    MIN_EXECUTION_PROFIT_USD,
)
from .signer import sign_transaction, get_sender
from .relay import relay_transaction, wait_for_receipt, RelayResult
from log import get_logger

log = get_logger("executor")

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="web3")

@dataclass
class ExecutionResult:
    success: bool
    tx_hash: str
    method: str           # relay method used
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
    Consumes Opportunity objects and executes profitable arbitrage on-chain.

    Args:
        w3:       Web3 HTTP instance (reliable for tx submission).
        chain_id: Current chain ID.
    """

    def __init__(self, w3: Web3, chain_id: int = CHAIN_ID):
        self.w3       = w3
        self.chain_id = chain_id
        self._chain   = get_chain(chain_id)

        self._arb_exec = None
        self._arb_exec_extended = False
        extended_address = get_extended_arb_exec_address(chain_id)
        selected_address = extended_address or self._chain.arb_exec_address
        if selected_address:
            self._arb_exec = w3.eth.contract(
                address=w3.to_checksum_address(selected_address),
                abi=ARB_EXEC_ABI,
            )
            self._arb_exec_extended = bool(extended_address)
            log.info(f"ArbExecutor ready | contract={selected_address} | extended={self._arb_exec_extended}")

            relay_uri_available = self.chain_id in FLASHBOTS_RPCS
            try:
                from flashbots import FlashbotsWeb3, flashbot
                flashbot_available = True
            except ImportError:
                log.warning("Flashbots package not found; bundle relay disabled")
                flashbot_available = False
            self.use_flashbots = relay_uri_available and flashbot_available

            if self.use_flashbots:
                Account.enable_unaudited_hdwallet_features()

                # Flashbots authentication key (different from the signing key for txs)
                file = os.path.join(os.path.dirname(__file__), "relay_signer.json")
                password = os.getenv("RELAY_PASSWORD", "relay")

                if os.path.exists(file):
                    with open(file, "r") as f:
                        key = Account.decrypt(json.load(f), password)
                        self.relay_signer = Account.from_key(key)
                else:
                    self.relay_signer = Account.create()
                    # save relay_signer to file
                    keystore = Account.encrypt(self.relay_signer.key, password)
                    with open(file, "w") as f:
                        json.dump(keystore, f, indent=4)

                log.info(f"Relay initialized; identity={self.relay_signer.address}")

                flashbots_rpc = FLASHBOTS_RPCS.get(chain_id, FLASHBOTS_RPCS[1])
                flashbot(w3, self.relay_signer, flashbots_rpc)
                log.info(f"Flashbots relay configured | uri={flashbots_rpc}")

        else:
            log.warning("ArbExecutor: no arb_exec_address configured — execution disabled")

    def submit(self, opp: Opportunity) -> ExecutionResult | None:
        """
        Attempt to execute an arbitrage opportunity.

        Returns ExecutionResult on attempt, None if skipped (pre-flight fail).
        """
        if not self._arb_exec:
            return None

        try:
            return self._execute(opp)
        except Exception as e:
            log.error(f"Executor error: {e}")
            return ExecutionResult(
                success=False, tx_hash="", method="", lender="",
                amount_in=0, profit_wei=0, gas_cost_usd=0.0, error=str(e),
            )

    def submit_batch(self, opps: list[Opportunity], block_number: int) -> ExecutionResult | None:
        """
        Attempt to execute an arbitrage opportunity.

        Returns ExecutionResult on attempt, None if skipped (pre-flight fail).
        """
        if not self._arb_exec:
            return None

        try:    
            return self._execute_batch(opps, block_number)
        except Exception as e:
            log.error(f"Executor error: {e}")
            return ExecutionResult(
                success=False, tx_hash="", method="", lender="",
                amount_in=0, profit_wei=0, gas_cost_usd=0.0, error=str(e),
            )

    def _execute(self, opp: Opportunity, for_batch = False) -> ExecutionResult | None:
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

        # ── Simulate transaction ─────────────────────────────────────────
        if self.use_flashbots:
            log.debug(f"Simulating arb | lender={lender.lender_name} mode={lender.mode} amount={amount_in} hops={hop_count}")
            sim_result = self.execution_simulation(
                arb_data=arb_data,
                force_balancer=lender.force_balancer,
                builder_tip=builder_tip_bps,
                gas_units=gas_units,
                target_block=opp.block_number + 1,
                target_tx=trigger_tx if from_mempool else "",
            )
            if not sim_result or not sim_result.get("success"):
                error = sim_result.get("error") if sim_result else "Unknown simulation error"
                log.debug(f"Skipping: simulation failed | error={error}")
                return [None] * 4 if for_batch else None
            
            log.debug(f"Simulation success | gas_used={sim_result.get('gas_used')} gas_fee_wei={sim_result.get('gas_fee_wei')}")

            # return [None] * 4 if for_batch else None
            arb_data["minProfit"] = int(sim_result.get("gas_fee_wei", 0) * 1.2)  # increase minProfit to account for simulation uncertainty
            gas_units = sim_result.get("gas_used", gas_units)
        else:
            sim_result = {}

        if for_batch:
            return arb_data, gas_units, gas_cost_usd, builder_tip_bps

        # ── Step 5: Build transaction ─────────────────────────────────────────
        try:
            sender = get_sender()
        except RuntimeError as e:
            log.error(f"Cannot sign: {e}")
            return None

        tx = build_execute_tx(
            w3=self.w3,
            arb_exec = self._arb_exec,
            arb_data=arb_data,
            force_balancer=lender.force_balancer,
            builder_tip_bps=builder_tip_bps,
            sender=sender,
            gas_units=gas_units,
            fast_path=self._arb_exec_extended and int(arb_data.get("amountIn", 0)) > 0,
        )

        # ── Step 6: Sign ──────────────────────────────────────────────────────
        try:
            raw_tx = sign_transaction(self.w3, tx)
        except Exception as e:
            log.error(f"Signing failed: {e}")
            return None

        # ── Step 7: Relay ─────────────────────────────────────────────────────
        log.info(
            f"Executing arb | lender={lender.lender_name} mode={lender.mode} "
            f"amount={amount_in} profit_wei={profit_wei} "
            f"gas_usd=${gas_cost_usd:.3f} hops={hop_count}"
        )

        result: RelayResult = relay_transaction(
            w3=self.w3,
            raw_tx=raw_tx,
            target_tx=sim_result.get("target_raw_tx", b"") or b"",
            target_block=sim_result.get("target_block") or opp.block_number + 1,
            use_flashbots = self.use_flashbots
        )

        # ── Step 8: Receipt (direct only) ─────────────────────────────────────
        receipt = None
        if result.success and result.method == "direct":
            receipt = wait_for_receipt(self.w3, result.tx_hash, timeout=30)
            if receipt:
                status = receipt.get("status", 0)
                if status == 1:
                    profit = self.extract_profit(receipt)
                    log.info(f"✅ Arb confirmed | tx={result.tx_hash[:16]} profit_wei={profit}")
                else:
                    log.warning(f"❌ Arb reverted on-chain | tx={result.tx_hash[:16]}")
                    result.success = False

        return ExecutionResult(
            success=result.success,
            tx_hash=result.tx_hash,
            method=result.method,
            lender=lender.lender_name,
            amount_in=amount_in,
            profit_wei=profit_wei,
            gas_cost_usd=gas_cost_usd,
            error=result.error,
            receipt=receipt,
        )

    def _execute_batch(self, opps: list[Opportunity], block_number) -> ExecutionResult | None:
        # ── Step 5: Build transaction ─────────────────────────────────────────
        calls, total_gas, total_gas_usd = build_batch_calls(self._execute, self._arb_exec, opps)
        try:
            sender = get_sender()
        except RuntimeError as e:
            log.error(f"Cannot sign: {e}")
            return None

        tx = build_execute_batch_tx(
            w3=self.w3,
            arb_exec=self._arb_exec,
            calls=calls,
            sender=sender,
            gas_units=total_gas,
        )

        # ── Step 6: Sign ──────────────────────────────────────────────────────
        try:
            raw_tx = sign_transaction(self.w3, tx)
        except Exception as e:
            log.error(f"Signing failed: {e}")
            return None

        # ── Step 7: Relay ─────────────────────────────────────────────────────
        log.info(
            f"Batch execution |opps_count={len(opps)} lender=pool_flash_swap mode=1 "
            f"gas_usd=${total_gas_usd:.3f}"
        )

        result: RelayResult = relay_transaction(
            w3=self.w3,
            raw_tx=raw_tx,
            target_block=block_number + 1,
            use_flashbots = self.use_flashbots
        )

        # ── Step 8: Receipt (direct only) ─────────────────────────────────────
        receipt = None
        batch_result = {}
        if result.success and result.method == "direct":
            receipt = wait_for_receipt(self.w3, result.tx_hash, timeout=30)
            if receipt:
                status = receipt.get("status", 0)
                if status == 1:
                    batch_result = self.extract_batch_result(receipt)
                    summary = batch_result.get('summary')
                    log.info(f"✅ Batch Arb confirmed | tx={result.tx_hash[:16]} | summary: ({summary.successful} successful, {summary.failed} failed)")
                else:
                    log.warning(f"❌ Batch Arb reverted on-chain | tx={result.tx_hash[:16]}")
                    result.success = False
        return ExecutionResult(
            success=result.success,
            tx_hash=result.tx_hash,
            method=result.method,
            amount_in=None,
            lender="pool_flash_swap",
            profit_wei=batch_result.get("est_profit_wei") if batch_result else 0,
            profit_usd=batch_result.get("est_profit_usd") if batch_result else 0,
            gas_cost_usd=batch_result.get("gas_usd") if batch_result else total_gas_usd,
            summary=summary if batch_result else None,
            error=result.error,
            receipt=receipt,
        )


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
            # amount_formatted = None
            # if args.profitToken == wrapped_native and native_price:
            #     amount_formatted = float(Web3.from_wei(args.amt, 'ether'))
            #     est_profit_usd += amount_formatted * native_price
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
    
    def execution_simulation(
            self, 
            arb_data, 
            force_balancer, 
            builder_tip, 
            gas_units,
            target_block: int = None,
            target_tx: str = "") -> int:
        """
        Simulate the execution of an opportunity to get a more accurate profit estimate.
        This can be used for pre-flight checks or for post-detection refinement.
        """
        # ── Step 5: Build transaction ─────────────────────────────────────────
        if not self.use_flashbots:
            log.warning("Simulation requested but Flashbots relay is not enabled")
            return {
                "success": False,
                "error": "Flashbots relay not enabled",
            }
        
        try:
            sender = get_sender()
        except RuntimeError as e:
            log.error(f"Cannot sign: {e}")
            return {}

        tx = build_execute_tx(
            w3=self.w3,
            arb_exec = self._arb_exec,
            arb_data=arb_data,
            force_balancer=force_balancer,
            builder_tip_bps=builder_tip,
            sender=sender,
            gas_units=gas_units,
            fast_path=self._arb_exec_extended and int(arb_data.get("amountIn", 0)) > 0,
        )

        # ── Step 6: Sign ──────────────────────────────────────────────────────
        try:
            raw_tx = sign_transaction(self.w3, tx)
        except Exception as e:
            log.error(f"Signing failed: {e}")
            return {}
        
        if target_tx:
            full_tx = self.w3.eth.get_transaction(target_tx)
            target_raw_tx = serialize(full_tx)
            log.info(f"Target tx for bundle | hash={target_tx} from={full_tx['from']} to={full_tx['to']} value={full_tx['value']}")
            bundle = [
                {"signed_transaction": target_raw_tx, "hash": target_tx},
                {"signed_transaction": raw_tx},
            ]
        else:
            bundle = [{"signed_transaction": raw_tx}]

        try:
            sim = self.w3.flashbots.simulate(bundle, max(target_block, self.w3.eth.block_number))
            if sim.get("results"):
                revert_data = sim["results"][0].get("revert")
                if revert_data:
                    error = decode(["string"], to_bytes(hexstr="0x" + revert_data[10:])) if len(revert_data) > 64 else revert_data
                    return {
                        "success": False,
                        "error": error,
                    }
                return {
                    "success": True,
                    "gas_used": sim["totalGasUsed"],
                    "gas_fee_wei": sim["results"][0].get("gasFees", 0),
                    "target_block": sim.get("blockNumber"),
                    "target_raw_tx": target_raw_tx if target_tx else None,
                }
        except Exception as e:
            log.warning(f"Simulation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }
        # sim["signedBundledTransactions"] = None
        # log.debug(f"Simulation: {json.dumps(sim, indent=2)}")