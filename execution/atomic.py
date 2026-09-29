from __future__ import annotations

"""Atomic execution preflight and transaction-envelope construction.

The deployed ArbExec.swap() path is preferred when a route is representable by
its native V2/V3 + Balancer/flash-swap machinery. Its multiCall() remains
rejected because it intentionally catches individual failures. The generic
AtomicArbExecutor is retained for heterogeneous protocol routes.
"""
from dataclasses import dataclass, field
from typing import Any, Iterable, TYPE_CHECKING
from pathlib import Path
import json

from execution.route_ir import ExecutionPlan
from execution.arbexec_adapter import build_arbexec_route
from utils.web3_compat import encode_call

if TYPE_CHECKING:
    from execution.route_compiler import CallSpec

ATOMIC_EXECUTOR_ABI_PATH = Path(__file__).resolve().parents[1] / "executor" / "atomic_executor_abi.json"

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "balance", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "allowance", "type": "uint256"}]},
]

@dataclass(frozen=True)
class AtomicPreflight:
    ok: bool
    reason: str = ""
    gas_estimate: int = 0
    calls: tuple[CallSpec, ...] = ()
    calldata: bytes = b""
    requires_funding: bool = False
    missing_allowances: tuple[tuple[str, str, int], ...] = ()
    errors: tuple[str, ...] = ()

@dataclass(frozen=True)
class AtomicEnvelope:
    chain_id: int
    executor: str
    target: str
    calldata: bytes
    value: int
    gas_limit: int
    atomic: bool
    metadata: dict[str, Any] = field(default_factory=dict)

class AtomicExecutionService:
    """Compile, preflight, simulate and build a single execution envelope."""

    def __init__(self, w3, executor_address: str | None = None,
                 arb_exec_address: str | None = None):
        from web3 import Web3
        from execution.route_compiler import RouteCompiler
        self.w3 = w3
        self.chain_id = int(w3.eth.chain_id)
        self.executor_address = Web3.to_checksum_address(executor_address) if executor_address else None
        self.arb_exec_extended = False
        if arb_exec_address is None:
            try:
                from config import get_chain, get_extended_arb_exec_address
                extended = get_extended_arb_exec_address(self.chain_id)
                legacy = get_chain(self.chain_id).arb_exec_address
                arb_exec_address = extended or legacy
                self.arb_exec_extended = bool(extended)
            except Exception:
                arb_exec_address = None
        self.arb_exec_address = Web3.to_checksum_address(arb_exec_address) if arb_exec_address else None
        self.compiler = RouteCompiler(w3)

    def compile(self, plan: ExecutionPlan):
        return tuple(self.compiler.compile(plan))

    @staticmethod
    def _encode_call(w3, call) -> bytes:
        # ABI encoding is performed from the protocol function signatures rather
        # than relying on a mutable contract object. This keeps compilation pure.
        signatures = {
            "swapExactTokensForTokens": "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)",
            "exactInput": "exactInput(bytes,uint256)",
            "exchange": "exchange(address[11],uint256[5][5],uint256,uint256,address[5])",
            "exchange_underlying": "exchange_underlying(int128,int128,uint256,uint256)",
            "batchSwap": "batchSwap(uint8,(bytes32,uint256,uint256,uint256,bytes)[],address[],(address,bool,address,bool),int256[],uint256)",
            "swap": "swap(bytes[],uint256,uint256)",
        }
        # web3's contract encoder is preferable where available; use eth_abi only
        # for function selectors/arguments in a small, explicit fallback.
        from eth_utils import keccak
        from eth_abi import encode
        sig = signatures.get(call.function)
        if not sig:
            raise ValueError(f"unsupported calldata function: {call.function}")
        selector = keccak(text=sig)[:4]
        # Tuple-heavy functions are easiest and safest through a temporary ABI.
        if call.function == "swapExactTokensForTokens":
            types = ["uint256","uint256","address[]","address","uint256"]
        elif call.function == "exactInput":
            types = ["bytes","uint256"]
        elif call.function == "exchange_underlying":
            types = ["int128","int128","uint256","uint256"]
        elif call.function == "exchange":
            types = ["address[11]","uint256[5][5]","uint256","uint256","address[5]"]
        elif call.function == "batchSwap":
            types = ["uint8","(bytes32,uint256,uint256,uint256,bytes)[]","address[]","(address,bool,address,bool)","int256[]","uint256"]
        else:
            # SyncSwap's exact router ABI is deployment-specific; require the
            # route compiler metadata to provide already-encoded calldata.
            raise ValueError("syncswap swap requires pre-encoded calldata metadata")
        return selector + encode(types, list(call.args))

    def encode_calls(self, calls: Iterable) -> tuple[bytes, ...]:
        return tuple(self._encode_call(self.w3, c) for c in calls)

    def _check_allowances(self, calls: tuple[CallSpec, ...], owner: str) -> tuple[tuple[str, str, int], ...]:
        if not owner:
            return ()
        from web3 import Web3
        missing: list[tuple[str, str, int]] = []
        for call in calls:
            # Approval is needed by the executor for token-in calls. Native ETH
            # and protocols with custom internal settlement are handled by their
            # own executor adapters and are not guessed here.
            token = None
            amount = 0
            if call.function == "swapExactTokensForTokens":
                amount = int(call.args[0]); token = call.args[2][0]
            if token and amount:
                c = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
                allowance = int(c.functions.allowance(owner, call.target).call())
                if allowance < amount:
                    missing.append((token, call.target, amount - allowance))
        return tuple(missing)

    def _atomic_contract(self):
        return self.w3.eth.contract(address=self.executor_address, abi=json.loads(ATOMIC_EXECUTOR_ABI_PATH.read_text()))

    def build_atomic_executor(self, plan: ExecutionPlan, *, route_id: bytes | str,
                              profit_token: str, min_profit: int, deadline: int,
                              valid_until_block: int, gas_limit: int | None = None,
                              value: int = 0) -> AtomicEnvelope:
        if not self.executor_address:
            raise ValueError("atomic executor address not configured")
        calls = self.compile(plan)
        if not calls:
            raise ValueError("cannot build an atomic envelope for an empty route")
        encoded = self.encode_calls(calls)
        if isinstance(route_id, str):
            rid = bytes.fromhex(route_id.removeprefix("0x"))
        else:
            rid = bytes(route_id)
        if len(rid) != 32:
            raise ValueError("route_id must be exactly 32 bytes")
        from web3 import Web3
        contract = self._atomic_contract()
        call_args = [(c.target, int(c.value), raw) for c, raw in zip(calls, encoded)]
        fn = contract.functions.executeAtomic(
            rid, Web3.to_checksum_address(profit_token), int(min_profit), int(deadline),
            int(valid_until_block), call_args
        )
        tx = fn.build_transaction({"from": self.executor_address, "value": int(value)})
        estimated = int(gas_limit) if gas_limit is not None else int(self.w3.eth.estimate_gas(tx))
        calldata = bytes.fromhex(tx["data"].removeprefix("0x"))
        return AtomicEnvelope(
            self.chain_id, self.executor_address, self.executor_address, calldata, int(value),
            estimated, True,
            {"mode": "atomic_executor", "route_id": "0x" + rid.hex(),
             "profit_token": Web3.to_checksum_address(profit_token),
             "min_profit": int(min_profit), "deadline": int(deadline),
             "valid_until_block": int(valid_until_block), "call_count": len(calls)}
        )

    def preflight(self, plan: ExecutionPlan, *, simulate: bool = True,
                  route_id: bytes | str = bytes(32), profit_token: str | None = None,
                  min_profit: int | None = None, deadline: int | None = None,
                  valid_until_block: int | None = None, sender: str | None = None,
                  force_balancer: bool = True, backend: str = "auto") -> AtomicPreflight:
        if plan.chain_id != self.chain_id:
            return AtomicPreflight(False, f"chain mismatch: plan={plan.chain_id}, rpc={self.chain_id}")
        if backend == "arbexec" and not self.arb_exec_address:
            return AtomicPreflight(False, "no ArbExec address configured")
        if backend == "generic" and not self.executor_address:
            return AtomicPreflight(False, "no generic atomic executor address configured")
        if backend == "auto" and not (self.arb_exec_address or self.executor_address):
            return AtomicPreflight(False, "no execution backend address configured")
        try:
            calls = self.compile(plan)
            if not calls:
                return AtomicPreflight(False, "empty compiled route")
            if plan.metadata.get("execution_mode") == "generic_multicall":
                return AtomicPreflight(False, "generic multiCall is non-atomic; refusing arb execution")
            encoded = self.encode_calls(calls)
            errors: list[str] = []
            gas = 0
            if simulate:
                try:
                    current_block = int(self.w3.eth.block_number)
                    selected = backend
                    if selected == "auto":
                        selected = "arbexec" if self.arb_exec_address and all(
                            c.protocol.lower() in {"uniswap_v2", "uniswap_v3", "sushiswap_v2", "pancakeswap_v2", "ringswap_v2"}
                            for c in calls
                        ) else "generic"
                    if selected == "arbexec":
                        if not sender:
                            return AtomicPreflight(False, "sender is required for ArbExec simulation", calls=calls)
                        env = self.build_arbexec_plan(
                            plan, force_balancer=force_balancer, sender=sender,
                            min_profit=int(plan.min_profit if min_profit is None else min_profit),
                            builder_fee_bps=int(plan.metadata.get("builder_fee_bps", 0)),
                            valid_until_block=current_block if valid_until_block is None else int(valid_until_block),
                        )
                    elif selected == "generic":
                        if not self.executor_address:
                            return AtomicPreflight(False, "no generic atomic executor address configured", calls=calls)
                        if profit_token is None:
                            return AtomicPreflight(False, "profit_token is required for generic atomic simulation", calls=calls)
                        env = self.build_atomic_executor(
                            plan, route_id=route_id, profit_token=profit_token,
                            min_profit=int(plan.min_profit if min_profit is None else min_profit),
                            deadline=int(plan.deadline if deadline is None else deadline),
                            valid_until_block=current_block if valid_until_block is None else int(valid_until_block),
                        )
                    else:
                        return AtomicPreflight(False, f"unknown execution backend: {backend}", calls=calls)
                    tx = {"from": sender or self.executor_address, "to": env.target,
                          "data": "0x" + env.calldata.hex(), "value": env.value}
                    # Simulate the exact transaction envelope, not each hop.
                    self.w3.eth.call(tx)
                    gas = int(self.w3.eth.estimate_gas(tx))
                except Exception as exc:
                    errors.append(f"{backend} simulation failed: {exc}")
            if errors:
                return AtomicPreflight(False, errors[0], gas_estimate=gas, calls=calls,
                                       calldata=b"", errors=tuple(errors))
            return AtomicPreflight(True, "atomic preflight passed", gas_estimate=gas, calls=calls,
                                   calldata=b"" if not encoded else encoded[0])
        except Exception as exc:
            return AtomicPreflight(False, str(exc))



    def build_arbexec_plan(self, plan: ExecutionPlan, *, force_balancer: bool,
                           sender: str, borrowed_amount: int | None = None,
                           min_profit: int | None = None, builder_fee_bps: int = 0,
                           valid_until_block: int | None = None,
                           gas_limit: int | None = None) -> AtomicEnvelope:
        """Build the native gas-optimized ``ArbExec.swap`` envelope.

        This is preferred over the generic ``AtomicArbExecutor`` whenever the
        route is representable by the deployed ArbExec.  The contract owns the
        flash-loan/flash-swap callbacks and performs the route internally, so
        the transaction contains one compact contract call instead of one
        calldata item per router hop.
        """
        if not self.arb_exec_address:
            raise ValueError("ArbExec address not configured")
        from web3 import Web3
        mode = int(plan.metadata.get("arbexec_mode", 0))
        route = build_arbexec_route(
            plan, self.w3, mode=mode, borrowed_amount=borrowed_amount,
            min_profit=min_profit,
        )
        until = int(valid_until_block if valid_until_block is not None
                    else self.w3.eth.block_number + 1)
        contract = self.w3.eth.contract(
            address=self.arb_exec_address,
            abi=[{
                "inputs": [{
                    "components": [
                        {"name":"amountIn","type":"uint256"},
                        {"name":"minProfit","type":"uint256"},
                        {"name":"tokens","type":"address[]"},
                        {"name":"pools","type":"address[]"},
                        {"name":"fees","type":"uint24[]"},
                        {"name":"tokenIn","type":"address"},
                        {"name":"mode","type":"uint8"},
                    ], "name":"arb", "type":"tuple"
                }, {"name":"lender","type":"bool"},
                   {"name":"builderFeeBps","type":"uint256"},
                   {"name":"validUntilBlock","type":"uint256"}],
                "name":"swap", "outputs": [], "stateMutability":"nonpayable",
                "type":"function"
            }]
        )
        swap_fn = contract.functions.swapFast if self.arb_exec_extended else contract.functions.swap
        tx = swap_fn(
            route.as_dict(), bool(force_balancer), int(builder_fee_bps), until
        ).build_transaction({
            "from": Web3.to_checksum_address(sender),
            "value": 0,
            "chainId": self.chain_id,
        })
        estimated = int(gas_limit) if gas_limit is not None else int(self.w3.eth.estimate_gas(tx))
        calldata = bytes.fromhex(tx["data"].removeprefix("0x"))
        return AtomicEnvelope(
            self.chain_id, self.arb_exec_address, self.arb_exec_address, calldata, 0,
            estimated, True,
            {"mode":"arbexec_fast" if self.arb_exec_extended else "arbexec_native", "arbexec_mode":route.mode,
             "force_balancer":bool(force_balancer), "builder_fee_bps":int(builder_fee_bps),
             "amount_in":route.amount_in,
             "valid_until_block":until, "call_count":len(route.pools)}
        )

    def build_arbexec_swap(self, arb_exec, arb_data: dict, *, force_balancer: bool,
                           builder_fee_bps: int = 0, valid_until_block: int,
                           sender: str, gas_limit: int) -> AtomicEnvelope:
        if not (self.arb_exec_address or self.executor_address):
            raise ValueError("ArbExec address not configured")
        calldata = bytes.fromhex(encode_call(
            arb_exec,
            "swap",
            [arb_data, bool(force_balancer), int(builder_fee_bps), int(valid_until_block)],
        ).removeprefix("0x"))
        target = self.arb_exec_address or self.executor_address
        return AtomicEnvelope(self.chain_id, target, target,
                              calldata, 0, int(gas_limit), True,
                              {"mode": "arbexec_swap", "valid_until_block": int(valid_until_block)})
