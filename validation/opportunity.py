"""Execution-grade opportunity validation.

Detection produces candidates. This module is the gate between a candidate and
an execution-ready opportunity. A quote/yieldOut result is treated as a quote,
not as proof that the state-changing transaction will succeed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config import ARB_EXEC_ABI, get_chain
from executor.signer import recorded_agent_address
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
    # DSS-WO-MI-105 WS-C + R1: real quote evidence, preserved at the point it
    # becomes available. `quote_attempted` = the yieldOut call was invoked;
    # `quote_returned` = it returned without reverting (a quote was actually
    # OBTAINED). Amount evidence is populated ONLY when the quote returned.
    # Denomination comes from the RETURNED arb struct's token path — the
    # contract may reorder a 2-pool route in _determineBorrow — and follows
    # the authoritative Exec.sol semantics (`// 0 = borrow token in,
    # 1 = borrow other token`): mode 0 → tokens[0] (trade input in the first
    # route token), mode 1 → tokens[1] (the BORROWED ROUTE AMOUNT in the
    # second route token that feeds pools[1..] and is matched against the
    # pool-0 debt — NOT a profit target in the first token).
    quote_attempted: bool = False
    quote_returned: bool = False
    amount_in_raw: int | None = None
    amount_in_token: str = ""
    amount_in_mode: int | None = None
    quote_method: str = ""
    # Which validation stage failed when ok is False:
    # quote_encode | quote_call | profit_gate | tx_build | gas_estimate |
    # simulation_call. Empty on success.
    stage_failed: str = ""
    # Per-lender-mode attempt evidence, filled by validate() when both modes
    # are tried — identifies which quote actually returned and which stage
    # each attempt failed at.
    mode_attempts: tuple = ()


class OpportunityValidator:
    """Validate executable candidates against the live EVM state."""

    QUOTE_RETENTION_BPS = 9000  # exact execution must retain >= 90% of quote

    def __init__(self, w3, chain_id: int):
        self.w3 = w3
        self.chain_id = int(chain_id)
        chain = get_chain(self.chain_id)
        address = chain.arb_exec_address
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
            # DSS-WO-ENG-015 R4: the sender address is the recorded PUBLIC
            # agent identity from the single wallet-custody path
            # (executor/signer.py — no key material). When no secret store
            # is provisioned this fails closed exactly like the removed
            # legacy signer did, so a candidate can never be validated for
            # execution without a recorded agent identity.
            sender = self.w3.to_checksum_address(
                recorded_agent_address()
            )
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
        mode_evidence: list[dict] = []
        for mode, lender, force_balancer in (
            (0, "balancer", True),
            (1, "pool_flash_swap", False),
        ):
            result = self._validate_mode(
                opp, sender, mode, lender, force_balancer, valid_until, context_block
            )
            mode_evidence.append({
                "mode": mode,
                "lender": lender,
                "quote_attempted": result.quote_attempted,
                "quote_returned": result.quote_returned,
                "amount_in_raw": result.amount_in_raw,
                "amount_in_token": result.amount_in_token or None,
                "quote_profit_wei": result.quote_profit_wei or None,
                "stage_failed": result.stage_failed or None,
                "status": result.status,
            })
            if result.ok:
                return replace(result, mode_attempts=tuple(mode_evidence))
            attempts.append(f"{lender}: {result.reason}")

        # MI-105-R1 WS-C + R2 §2: aggregate the attempted/obtained evidence
        # across both lender modes — a quote that was ATTEMPTED (even if it
        # reverted) means validation WAS performed, the amount evidence only
        # exists for a quote that actually RETURNED, and the most recent
        # RETURNED quote's evidence — INCLUDING its quote_method — survives on
        # the final result even when a later simulation step failed (per-mode
        # detail is preserved in mode_attempts). When no quote was returned,
        # quote_method stays empty so the recorder never reports a method for
        # a quote that was not obtained.
        final_quote = next((e for e in reversed(mode_evidence) if e["quote_returned"]), None)
        return ValidationResult(False, "rejected", "; ".join(attempts),
                                quote_attempted=any(e["quote_attempted"] for e in mode_evidence),
                                quote_returned=final_quote is not None,
                                amount_in_raw=final_quote["amount_in_raw"] if final_quote else None,
                                amount_in_token=(final_quote["amount_in_token"] or "") if final_quote else "",
                                amount_in_mode=final_quote["mode"] if final_quote else None,
                                quote_profit_wei=final_quote["quote_profit_wei"] or 0 if final_quote else 0,
                                quote_method="exec_yieldOut" if final_quote else "",
                                stage_failed=(mode_evidence[-1]["stage_failed"] or "") if mode_evidence else "",
                                mode_attempts=tuple(mode_evidence))

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
        except Exception as exc:
            # Encoding failure — the quote was never attempted.
            return ValidationResult(False, "execution_rejected", str(exc),
                                    amount_in_mode=mode, stage_failed="quote_encode")

        # Stage 1 — the quote call itself. Evidence is preserved at the exact
        # point it becomes available (MI-105-R1 WS-C): a quote that REVERTED is
        # "attempted", never "obtained", and carries no amount evidence.
        try:
            ad, quote_profit = self.contract.functions.yieldOut(raw).call(block_identifier=context_block)
        except Exception as exc:
            return ValidationResult(False, "quote_rejected", str(exc),
                                    quote_attempted=True,
                                    quote_returned=False,
                                    amount_in_mode=mode,
                                    quote_method="exec_yieldOut",
                                    stage_failed="quote_call")

        quote_profit = int(quote_profit)
        # ad[0] is the resolved Data.amountIn: either the caller-supplied
        # amount or, when the candidate passed 0, the contract-computed optimal
        # borrow (_determineBorrow in the authoritative Exec.sol).
        amount_in_raw = int(ad[0])
        # Denomination from the RETURNED arb struct's token path — not from
        # opp.tokens[0] (the contract may reorder a 2-pool route). Mode 0: the
        # trade input in tokens[0]; mode 1: the borrowed route amount in
        # tokens[1] (see the Data.mode comment in Exec.sol).
        ad_tokens = [str(t) for t in ad[2]] if len(ad) > 2 and ad[2] is not None else []
        if mode == 1:
            denomination = ad_tokens[1] if len(ad_tokens) > 1 else ""
        else:
            denomination = ad_tokens[0] if ad_tokens else ""

        if quote_profit <= 0:
            return ValidationResult(False, "quote_rejected", "yieldOut returned non-positive profit",
                                    quote_attempted=True,
                                    quote_returned=True,
                                    quote_profit_wei=quote_profit,
                                    amount_in_raw=amount_in_raw,
                                    amount_in_token=denomination,
                                    amount_in_mode=mode,
                                    quote_method="exec_yieldOut",
                                    stage_failed="profit_gate")

        # Preserve the contract's own profitability guard while allowing a
        # controlled amount of state drift between quote and full execution.
        floor = max(1, quote_profit * self.QUOTE_RETENTION_BPS // 10_000)
        ad_with_floor = (
            int(ad[0]), floor, list(ad[2]), list(ad[3]), list(ad[4]), ad[5], int(ad[6])
        )
        quote_evidence = dict(
            quote_attempted=True,
            quote_returned=True,
            quote_profit_wei=quote_profit,
            amount_in_raw=amount_in_raw,
            amount_in_token=denomination,
            amount_in_mode=mode,
            quote_method="exec_yieldOut",
            lender_mode=mode,
            lender=lender,
        )

        # Stage 2 — transaction build. A failure here still carries the full
        # quote evidence obtained above.
        try:
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
        except Exception as exc:
            return ValidationResult(False, "execution_rejected", str(exc),
                                    stage_failed="tx_build", **quote_evidence)

        # Stage 3 — gas estimation gate.
        try:
            gas_estimate = int(self.w3.eth.estimate_gas(tx, block_identifier=context_block))
        except Exception as exc:
            return ValidationResult(False, "execution_rejected", str(exc),
                                    stage_failed="gas_estimate", **quote_evidence)
        tx["gas"] = gas_estimate

        # Stage 4 — explicit final simulation of the exact data.
        try:
            # A second full eth_call is intentional: estimate_gas is a gate,
            # while eth_call is the explicit final simulation of the exact data.
            self.w3.eth.call(tx, block_identifier=context_block)
        except Exception as exc:
            return ValidationResult(False, "execution_rejected", str(exc),
                                    gas_estimate=gas_estimate,
                                    stage_failed="simulation_call", **quote_evidence)

        return ValidationResult(
            True,
            "execution_simulated",
            validated_floor_wei=floor,
            gas_estimate=gas_estimate,
            **quote_evidence,
        )

    @staticmethod
    def _reject(reason: str) -> ValidationResult:
        """Pre-check rejection: NO validation was performed (the quote and
        simulation stages were never reached — MI-105-R1 WS-B)."""
        return ValidationResult(False, "rejected", reason)

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
            # MI-105-R1 WS-B: EXPLICIT validation-performed signal — True only
            # when the quote/simulation stages were actually reached. A
            # pre-check rejection (unsupported route, chain mismatch, no
            # ArbExec, stale block, sender unavailable) never claims validation
            # happened, and the discovery-default status is never interpreted
            # as evidence of validation.
            meta["validation_performed"] = bool(result.quote_attempted)
            meta["validation_stage_failed"] = result.stage_failed or None
            meta["validation_block"] = ctx_block
            meta["validation_block_hash"] = ctx_hash
            meta["validation_reason"] = result.reason
            meta["execution_simulated"] = result.ok
            meta["quote_profit_wei"] = result.quote_profit_wei
            meta["validated_profit_floor_wei"] = result.validated_floor_wei
            meta["validated_gas_units"] = result.gas_estimate
            meta["validated_lender_mode"] = result.lender_mode
            meta["validated_lender"] = result.lender
            # DSS-WO-MI-105 WS-C + R1: real quote-size evidence when the quote
            # RETURNED (None = no quote obtained — never an inferred amount),
            # plus the attempted/obtained distinction and the per-mode attempt
            # evidence identifying which quote actually returned.
            meta["quote_attempted"] = bool(result.quote_attempted)
            meta["quote_returned"] = bool(result.quote_returned)
            meta["quote_amount_in_raw"] = result.amount_in_raw
            meta["quote_amount_in_denomination"] = result.amount_in_token or None
            meta["quote_amount_in_mode"] = result.amount_in_mode
            meta["quote_method"] = result.quote_method or None
            meta["validation_mode_attempts"] = (
                [dict(e) for e in result.mode_attempts] if result.mode_attempts else None
            )
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
