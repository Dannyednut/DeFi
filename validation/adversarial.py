"""Adversarial / target-transaction simulation for mempool opportunities.

This layer is deliberately separate from discovery. Reserve math can identify a
candidate, but a backrun is promoted only when the target transaction and the
candidate transaction can be replayed sequentially against the same state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import ARB_EXEC_ABI, get_chain, get_extended_arb_exec_address
from log import get_logger

log = get_logger("adversarial_validation")

@dataclass(frozen=True)
class AdversarialResult:
    ok: bool
    status: str
    reason: str = ""
    gas_estimate: int = 0
    simulated_profit_wei: int = 0
    backend: str = ""

class AdversarialSimulator:
    """Replay target tx then exact ArbExec tx when the node supports it."""

    def __init__(self, w3, chain_id: int):
        self.w3 = w3
        self.chain_id = int(chain_id)
        chain = get_chain(self.chain_id)
        self.address = get_extended_arb_exec_address(self.chain_id) or chain.arb_exec_address
        self.contract = None
        if self.address:
            self.contract = w3.eth.contract(
                address=w3.to_checksum_address(self.address), abi=ARB_EXEC_ABI
            )

    @staticmethod
    def _hex(v: Any) -> Any:
        if isinstance(v, (bytes, bytearray)):
            return "0x" + bytes(v).hex()
        if hasattr(v, "hex") and not isinstance(v, str):
            try: return v.hex()
            except Exception: pass
        return v

    def _tx_call(self, tx: dict) -> dict:
        allowed = ("from", "to", "gas", "gasPrice", "maxFeePerGas",
                   "maxPriorityFeePerGas", "value", "data", "nonce", "type", "accessList")
        out = {}
        for k in allowed:
            if k in tx and tx[k] is not None:
                v = tx[k]
                if isinstance(v, int):
                    v = hex(v)
                else:
                    v = self._hex(v)
                out[k] = v
        return out

    def _build_candidate_tx(self, opp, sender: str, block_number: int) -> dict:
        if not self.contract:
            raise RuntimeError("no ArbExec configured")
        meta = opp.metadata
        fees = meta.get("fees_pip", [int(f * 100) for f in meta.get("fees_bps", [])])
        raw = (
            0, 0,
            [self.w3.to_checksum_address(t) for t in opp.tokens],
            [self.w3.to_checksum_address(p) for p in opp.pools],
            [int(x) for x in fees],
            self.w3.to_checksum_address(opp.tokens[0]), 0,
        )
        ad, quote = self.contract.functions.yieldOut(raw).call(block_identifier=block_number)
        if int(quote) <= 0:
            raise RuntimeError("candidate quote is non-positive")
        floor = max(1, int(quote) * 9000 // 10000)
        ad = (int(ad[0]), floor, list(ad[2]), list(ad[3]), list(ad[4]), ad[5], int(ad[6]))
        tx = self.contract.functions.swap(ad, True, 0, int(block_number) + 1).build_transaction({
            "from": sender, "chainId": self.chain_id, "value": 0,
        })
        tx["to"] = self.w3.to_checksum_address(self.address)
        for k in ("nonce", "gas", "maxFeePerGas", "maxPriorityFeePerGas", "gasPrice"):
            tx.pop(k, None)
        return tx

    def simulate(self, opp, *, target_tx_hash: str, block_number: int) -> AdversarialResult:
        if not self.contract:
            return AdversarialResult(False, "rejected", "no ArbExec configured")
        try:
            latest = int(self.w3.eth.block_number)
            if latest != int(block_number):
                return AdversarialResult(False, "rejected", f"state moved from {block_number} to {latest}")
            sender = self.w3.to_checksum_address(__import__("executor.signer", fromlist=["get_sender"]).get_sender())
            target = self.w3.eth.get_transaction(target_tx_hash)
            candidate = self._build_candidate_tx(opp, sender, block_number)
            calls = [self._tx_call(dict(target)), self._tx_call(candidate)]
            payload = [{"calls": calls}],
            # eth_simulateV1 is intentionally invoked through raw RPC because
            # web3.py versions differ in support for this extension.
            result = self.w3.provider.make_request("eth_simulateV1", [
                {"blockStateCalls": [{"calls": calls}], "validation": False, "traceTransfers": True},
                hex(int(block_number)),
            ])
            if result.get("error"):
                return AdversarialResult(False, "adversarial_rejected", str(result["error"]), backend="eth_simulateV1")
            blocks = result.get("result") or []
            call_results = blocks[0].get("calls", []) if blocks else []
            if len(call_results) < 2:
                return AdversarialResult(False, "adversarial_rejected", "incomplete simulation result", backend="eth_simulateV1")
            target_ok = call_results[0].get("status") == "0x1"
            candidate_ok = call_results[1].get("status") == "0x1"
            if not target_ok:
                return AdversarialResult(False, "target_rejected", "target transaction does not execute in simulated context", backend="eth_simulateV1")
            if not candidate_ok:
                err = call_results[1].get("error") or call_results[1].get("returnData") or "candidate reverted"
                return AdversarialResult(False, "adversarial_rejected", str(err), backend="eth_simulateV1")
            gas = int(str(call_results[1].get("gasUsed", 0)), 0)
            return AdversarialResult(True, "adversarial_simulated", gas_estimate=gas, backend="eth_simulateV1")
        except Exception as exc:
            return AdversarialResult(False, "adversarial_unavailable", str(exc), backend="eth_simulateV1")
