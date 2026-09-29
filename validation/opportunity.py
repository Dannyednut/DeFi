"""Execution-grade opportunity validation.

Detection produces candidates. This module is the gate between a candidate and
an execution-ready opportunity. A quote/yieldOut result is treated as a quote,
not as proof that the state-changing transaction will succeed.
"""
from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config import ARB_EXEC_ABI, get_chain, get_extended_arb_exec_address
from log import get_logger

log = get_logger("opportunity_validation")


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    status: str
    reason: str = ""
    quote_profit_wei: int = 0
    validated_floor_wei: int = 0
    gas_estimate: int = 0
    lender_mode: int | None = None
    lender: str = ""


class OpportunityValidator:
    """Validate executable candidates against the live EVM state."""

    QUOTE_RETENTION_BPS = 9000  # exact execution must retain >= 90% of quote

    def __init__(self, w3, chain_id: int):
        self.w3 = w3
        self.chain_id = int(chain_id)
        chain = get_chain(self.chain_id)
        address = get_extended_arb_exec_address(self.chain_id) or chain.arb_exec_address
        self.address = address
        self.contract = None
        if address:
            self.contract = w3.eth.contract(
                address=w3.to_checksum_address(address),
                abi=ARB_EXEC_ABI,
            )

    def validate(self, opp, *, block_number: int | None = None) -> ValidationResult:
        """Run quote confirmation followed by full state-changing eth_call.

        The transaction is never broadcast. ``eth_estimateGas`` and ``eth_call``
        both execute the complete contract route in the node's simulated EVM.
        """
        if not self.contract:
            return self._reject("no ArbExec configured")
        if int(getattr(opp, "chain_id", -1)) != self.chain_id:
            return self._reject("chain mismatch")
        if not opp.metadata.get("execution_supported", False):
            return self._reject("executor route unsupported")
        if len(opp.tokens) != len(opp.pools) + 1 or not opp.pools:
            return self._reject("invalid route shape")

        try:
            from executor.signer import get_sender
            sender = self.w3.to_checksum_address(get_sender())
        except Exception as exc:
            return self._reject(f"sender unavailable: {exc}")

        latest = self.w3.eth.get_block("latest")
        latest_number = int(latest.get("number", self.w3.eth.block_number))
        context_block = latest_number if block_number is None else int(block_number)
        # The candidate graph and detector quotes are block-scoped. Never silently
        # validate an older candidate against a newer state.
        if block_number is not None and latest_number != int(block_number):
            return self._reject(f"state moved from candidate block {block_number} to head {latest_number}")
        valid_until = context_block + 1

        # Try both native contract lender modes. Mode 0 is useful when the
        # deployed contract has Balancer; mode 1 exercises the pool flash-swap
        # path and does not require an external lender.
        attempts = []
        for mode, lender, force_balancer in (
            (0, "balancer", True),
            (1, "pool_flash_swap", False),
        ):
            result = self._validate_mode(
                opp, sender, mode, lender, force_balancer, valid_until, context_block
            )
            if result.ok:
                return result
            attempts.append(f"{lender}: {result.reason}")

        return self._reject("; ".join(attempts))

    def _validate_mode(self, opp, sender, mode, lender, force_balancer, valid_until, context_block):
        try:
            raw = (
                0,
                0,
                [self.w3.to_checksum_address(t) for t in opp.tokens],
                [self.w3.to_checksum_address(p) for p in opp.pools],
                [int(x) for x in opp.metadata.get("fees_pip", [int(f * 100) for f in opp.metadata.get("fees_bps", [])])],
                self.w3.to_checksum_address(opp.tokens[0]),
                mode,
            )
            ad, quote_profit = self.contract.functions.yieldOut(raw).call(block_identifier=context_block)
            quote_profit = int(quote_profit)
            if quote_profit <= 0:
                return ValidationResult(False, "quote_rejected", "yieldOut returned non-positive profit")

            # Preserve the contract's own profitability guard while allowing a
            # controlled amount of state drift between quote and full execution.
            floor = max(1, quote_profit * self.QUOTE_RETENTION_BPS // 10_000)
            ad_with_floor = (
                int(ad[0]), floor, list(ad[2]), list(ad[3]), list(ad[4]), ad[5], int(ad[6])
            )

            tx_builder = self.contract.functions.swap(
                ad_with_floor,
                force_balancer,
                0,  # validation deliberately excludes builder tip
                valid_until,
            )
            tx = tx_builder.build_transaction({
                "from": sender,
                "chainId": self.chain_id,
                "value": 0,
            })
            tx["to"] = self.w3.to_checksum_address(self.address)
            tx.pop("nonce", None)
            tx.pop("gas", None)
            tx.pop("maxFeePerGas", None)
            tx.pop("maxPriorityFeePerGas", None)
            tx.pop("gasPrice", None)

            gas_estimate = int(self.w3.eth.estimate_gas(tx, block_identifier=context_block))
            tx["gas"] = gas_estimate
            # A second full eth_call is intentional: estimate_gas is a gate,
            # while eth_call is the explicit final simulation of the exact data.
            self.w3.eth.call(tx, block_identifier=context_block)

            return ValidationResult(
                True,
                "execution_simulated",
                quote_profit_wei=quote_profit,
                validated_floor_wei=floor,
                gas_estimate=gas_estimate,
                lender_mode=mode,
                lender=lender,
            )
        except Exception as exc:
            return ValidationResult(False, "execution_rejected", str(exc))

    @staticmethod
    def _reject(reason: str) -> ValidationResult:
        return ValidationResult(False, "rejected", reason)


def validate_many(w3, opportunities: list, chain_id: int, *, block_number: int | None = None, max_workers: int = 4) -> list:
    """Validate candidates concurrently while preserving input order."""
    if not opportunities:
        return []
    validator = OpportunityValidator(w3, chain_id)
    try:
        ctx_block = int(w3.eth.block_number if block_number is None else block_number)
        ctx_header = w3.eth.get_block(ctx_block)
        ctx_hash = (ctx_header.get("hash") if hasattr(ctx_header, "get") else getattr(ctx_header, "hash", None))
        ctx_hash = ctx_hash.hex() if hasattr(ctx_hash, "hex") else str(ctx_hash)
    except Exception:
        ctx_block, ctx_hash = block_number, ""
    out = [None] * len(opportunities)

    def one(i, opp):
        return i, validator.validate(opp, block_number=block_number)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(one, i, opp) for i, opp in enumerate(opportunities)]
        for future in as_completed(futures):
            i, result = future.result()
            opp = opportunities[i]
            meta = dict(opp.metadata or {})
            meta["validation_status"] = result.status
            meta["validation_block"] = ctx_block
            meta["validation_block_hash"] = ctx_hash
            meta["validation_reason"] = result.reason
            meta["execution_simulated"] = result.ok
            meta["quote_profit_wei"] = result.quote_profit_wei
            meta["validated_profit_floor_wei"] = result.validated_floor_wei
            meta["validated_gas_units"] = result.gas_estimate
            meta["validated_lender_mode"] = result.lender_mode
            meta["validated_lender"] = result.lender
            opp.metadata = meta
            if result.ok:
                opp.simulated_via_contract = True
                opp.simulation_profit_wei = result.validated_floor_wei
                opp.metadata["requires_exact_validation"] = False
                opp.metadata["execution_ready"] = True
            else:
                opp.metadata["execution_ready"] = False
            out[i] = opp
    return out
