"""Active-contract tests for the authoritative Exec.sol (DSS-WO-ENG-002).

These tests replace the retired Phase 12 / Phase 14 contract-existence and
string-presence checks. They verify real behaviour:

  1. Source provenance: every local contract copy re-hashes to the exact
     git blob recorded in contracts/PROVENANCE.md for the pinned EVM commit.
  2. Compilation: the source compiles with the pinned, checksum-verified
     solc (0.8.28+commit.7893614a) against pinned OpenZeppelin 5.7.0.
  3. ABI conformance: the committed executor/exec_abi.json equals the
     compiler-generated ABI, and config.ARB_EXEC_ABI (the Python-side
     contract interface) conforms to the generated interface.
  4. Python encoding: the adapter builds a swap envelope with the correct
     entrypoint selector and tuple layout, decoded back against the
     generated ABI.
  5. Fail-closed behaviour: invalid routes, missing configuration and chain
     mismatches never produce an envelope.

No test here deploys, signs or broadcasts anything.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import decode as abi_decode
from eth_utils import keccak
from web3.providers import BaseProvider

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import exec_build  # noqa: E402  (scripts/exec_build.py helper)

EVM_PINNED_COMMIT = "898fe82f29a5695a01278365eb442ab3a26811b5"
PROVENANCE = ROOT / "contracts" / "PROVENANCE.md"
GENERATED_ABI_PATH = ROOT / "executor" / "exec_abi.json"

A = "0x" + "aa" * 20   # token A
B = "0x" + "bb" * 20   # token B
P1 = "0x" + "c1" * 20  # pool 1
P2 = "0x" + "c2" * 20  # pool 2
SENDER = "0x" + "11" * 20
EXEC_ADDR = "0x" + "ee" * 20


# ---------------------------------------------------------------- helpers --
def _blob_sha(path: Path) -> str:
    """Git blob hash (SHA-1 over 'blob <len>\\0<content>')."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def _canon(param: dict) -> str:
    t = param["type"]
    if t.startswith("tuple"):
        inner = ",".join(_canon(c) for c in param["components"])
        return f"({inner}){t[5:]}"
    return t


def _sig(fn: dict) -> str:
    return f"{fn['name']}({','.join(_canon(i) for i in fn['inputs'])})"


def _fn(abi: list, name: str) -> dict:
    return next(x for x in abi if x.get("type") == "function" and x.get("name") == name)


def _ev(abi: list, name: str) -> dict:
    return next(x for x in abi if x.get("type") == "event" and x.get("name") == name)


@pytest.fixture(scope="module")
def generated() -> dict:
    """Compile the committed source with the pinned toolchain (once)."""
    return exec_build.compile_exec()


class _FakeProvider(BaseProvider):
    """Canned offline JSON-RPC responses — no network, deterministic values."""

    is_async = False

    def make_request(self, method, params):
        canned = {
            "eth_chainId": "0x1",
            "eth_blockNumber": "0x64",
            "eth_gasPrice": hex(10**9),
            "eth_maxPriorityFeePerGas": hex(10**9),
            "eth_estimateGas": hex(300_000),
            "eth_getTransactionCount": "0x0",
            "eth_call": "0x",
            "eth_getBlockByNumber": {"number": "0x64", "baseFeePerGas": hex(10**9)},
        }
        if method not in canned:
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32601, "message": f"unhandled: {method}"}}
        return {"jsonrpc": "2.0", "id": 1, "result": canned[method]}

    def is_connected(self):
        return True


def _w3():
    """Real Web3 over a canned offline provider: real encoders, no network."""
    from web3 import Web3
    return Web3(_FakeProvider())


def _plan(legs, chain_id: int = 1, amount: int = 1000, min_profit: int = 7, **kw):
    from execution.route_ir import ExecutionPlan
    return ExecutionPlan(chain_id, A, amount, tuple(legs), 999,
                         min_profit=min_profit, **kw)


def _leg(kind, protocol, pool, a, b, fee_bps=30, chain_id: int = 1):
    from execution.route_ir import ExecutionKind, ExecutionLeg
    return ExecutionLeg(chain_id, kind, protocol, "dex", pool, a, b, 100, fee_bps=fee_bps)


from execution.route_ir import ExecutionKind as _EK  # noqa: E402
V2, V3, CURVE = _EK.V2_SWAP, _EK.V3_SWAP, _EK.CURVE_EXCHANGE


# ------------------------------------------------------------ 1. provenance --
def test_source_provenance_matches_pinned_evm_commit():
    text = PROVENANCE.read_text()
    assert EVM_PINNED_COMMIT in text, "provenance must record the pinned EVM commit"
    rows = re.findall(r"\|\s*(contracts/[^\s|]+)\s*\|\s*`([0-9a-f]{40})`\s*\|", text)
    assert len(rows) == 7, f"expected 7 pinned files in PROVENANCE.md, got {len(rows)}"
    for rel, want in rows:
        got = _blob_sha(ROOT / rel)
        assert got == want, f"{rel} drifted from the pinned EVM blob ({want}): {got}"


def test_retired_contract_sources_are_absent():
    from config import ARB_EXEC_ABI
    assert not (ROOT / "contracts" / "AtomicArbExecutor.sol").exists()
    assert not (ROOT / "executor" / "atomic_executor_abi.json").exists()
    assert not (ROOT / "executor" / "exec_extended_abi.json").exists()
    names = {x.get("name") for x in ARB_EXEC_ABI}
    assert "executeAtomic" not in names and "swapFast" not in names


# ----------------------------------------------------------- 2. compilation --
def test_source_compiles_with_pinned_solc(generated):
    assert generated["compiler"] == exec_build.SOLC_VERSION
    abi = generated["abi"]
    assert any(x.get("type") == "function" and x.get("name") == "swap" for x in abi)


def test_authoritative_constructor_has_three_arguments(generated):
    abi = generated["abi"]
    ctor = next(x for x in abi if x.get("type") == "constructor")
    assert [i["type"] for i in ctor["inputs"]] == ["address", "address", "address"]
    assert [i["name"] for i in ctor["inputs"]] == ["_w", "_q", "_b"]


# -------------------------------------------------------- 3. ABI conformance --
def test_committed_exec_abi_equals_generated_abi(generated):
    committed = json.loads(GENERATED_ABI_PATH.read_text())
    assert committed["compiler"] == generated["compiler"]
    assert committed["source_commit"] == EVM_PINNED_COMMIT
    assert committed["abi"] == generated["abi"], (
        "executor/exec_abi.json drifted from the pinned sources — regenerate "
        "with `python scripts/exec_build.py`"
    )


def test_config_arb_exec_abi_conforms_to_generated_interface(generated):
    from config import ARB_EXEC_ABI
    gen = generated["abi"]
    for entry in ARB_EXEC_ABI:
        kind = entry.get("type")
        name = entry.get("name")
        pool = gen if kind == "function" else gen
        ref = next((x for x in pool
                    if x.get("type") == kind and x.get("name") == name), None)
        assert ref is not None, f"{kind} {name} does not exist in the generated ABI"
        assert _sig(entry) == _sig(ref), f"{name}: signature drift vs authoritative source"
        if kind == "function":
            assert entry["stateMutability"] == ref["stateMutability"], f"{name}: mutability drift"
            assert [_canon(o) for o in entry["outputs"]] == [_canon(o) for o in ref["outputs"]], \
                f"{name}: output drift"
        else:  # event
            assert [i.get("indexed", False) for i in entry["inputs"]] == \
                   [i.get("indexed", False) for i in ref["inputs"]], f"{name}: indexed drift"


def test_config_abi_has_only_active_entrypoints():
    from config import ARB_EXEC_ABI
    names = {x.get("name") for x in ARB_EXEC_ABI}
    assert {"swap", "yieldOut", "multiCall", "DONE", "BATCH"} <= names
    assert names <= {"swap", "yieldOut", "multiCall", "DONE", "BATCH"}


def test_config_swap_selector_equals_generated_selector(generated):
    from config import ARB_EXEC_ABI
    cfg_swap = _fn(ARB_EXEC_ABI, "swap")
    gen_swap = _fn(generated["abi"], "swap")
    assert keccak(text=_sig(cfg_swap))[:4] == keccak(text=_sig(gen_swap))[:4]


# -------------------------------------------------------- 4. Python encoding --
def test_adapter_encodes_authoritative_swap_entrypoint_and_tuple(generated):
    from execution.atomic import AtomicExecutionService

    plan = _plan([
        _leg(V2, "uniswap_v2", P1, A, B),
        _leg(V3, "uniswap_v3", P2, B, A, 5),
    ])
    svc = AtomicExecutionService(_w3(), arb_exec_address=EXEC_ADDR)
    env = svc.build_arbexec_plan(plan, force_balancer=True, sender=SENDER)

    gen_swap = _fn(generated["abi"], "swap")
    expected_selector = keccak(text=_sig(gen_swap))[:4]
    assert bytes(env.calldata[:4]) == expected_selector, "envelope does not target Exec.swap"

    # Decode the payload against the generated ABI and verify the tuple layout.
    arb_type = "(" + ",".join(_canon(c) for c in gen_swap["inputs"][0]["components"]) + ")"
    values = abi_decode([arb_type, "bool", "uint256", "uint256"], bytes(env.calldata[4:]))
    arb, lender, builder_fee, until_block = values
    assert lender is True
    assert builder_fee == 0
    assert until_block == env.metadata["valid_until_block"]
    assert arb[0] == 1000            # amountIn
    assert arb[1] == 7               # minProfit
    assert list(arb[2]) == [A, B, A] # tokens
    assert list(arb[3]) == [P1, P2]  # pools
    assert list(arb[4]) == [3000, 500]  # fees (bps -> hundredths)
    assert arb[5] == A               # tokenIn
    assert arb[6] == 0               # mode

    # The config-side ABI must encode the exact same calldata for the same call.
    from web3 import Web3
    from config import ARB_EXEC_ABI
    cs = Web3.to_checksum_address
    w3 = Web3()
    contract = w3.eth.contract(address=env.executor, abi=ARB_EXEC_ABI)
    calldata_cfg = contract.encodeABI(
        fn_name="swap",
        args=[{"amountIn": 1000, "minProfit": 7, "tokens": [cs(A), cs(B), cs(A)],
               "pools": [cs(P1), cs(P2)], "fees": [3000, 500], "tokenIn": cs(A), "mode": 0},
              True, 0, until_block],
    )
    assert calldata_cfg == "0x" + env.calldata.hex()


# ------------------------------------------------------------ 5. fail-closed --
def test_invalid_route_protocol_fails_closed():
    from execution.arbexec_adapter import build_arbexec_route
    plan = _plan([
        _leg(CURVE, "curve", P1, A, B),
        _leg(V2, "uniswap_v2", P2, B, A),
    ])
    with pytest.raises(ValueError, match="cannot execute"):
        build_arbexec_route(plan, _w3(), mode=0)


def test_invalid_route_mode_fails_closed():
    from execution.arbexec_adapter import build_arbexec_route
    plan = _plan([
        _leg(V2, "uniswap_v2", P1, A, B),
        _leg(V2, "uniswap_v2", P2, B, A),
    ])
    with pytest.raises(ValueError):
        build_arbexec_route(plan, _w3(), mode=2)


def test_missing_contract_config_fails_closed(generated):
    from unittest.mock import patch
    from execution.atomic import AtomicExecutionService

    plan = _plan([
        _leg(V2, "uniswap_v2", P1, A, B),
        _leg(V3, "uniswap_v3", P2, B, A, 5),
    ])
    with patch("config.get_chain", return_value=SimpleNamespace(arb_exec_address=None)):
        svc = AtomicExecutionService(_w3(), arb_exec_address=None)
        assert svc.arb_exec_address is None
        result = svc.preflight(plan, sender=SENDER)
        assert result.ok is False
        assert "no ArbExec address configured" in result.reason
        with pytest.raises(ValueError, match="ArbExec address not configured"):
            svc.build_arbexec_plan(plan, force_balancer=True, sender=SENDER)


def test_chain_mismatch_fails_closed():
    from execution.atomic import AtomicExecutionService
    plan = _plan([
        _leg(V2, "uniswap_v2", P1, A, B, chain_id=999),
        _leg(V2, "uniswap_v2", P2, B, A, chain_id=999),
    ], chain_id=999)
    svc = AtomicExecutionService(_w3(), arb_exec_address=EXEC_ADDR)
    result = svc.preflight(plan, sender=SENDER)
    assert result.ok is False
    assert "chain mismatch" in result.reason


def test_unknown_backend_fails_closed():
    from execution.atomic import AtomicExecutionService
    plan = _plan([
        _leg(V2, "uniswap_v2", P1, A, B),
        _leg(V2, "uniswap_v2", P2, B, A),
    ])
    svc = AtomicExecutionService(_w3(), arb_exec_address=EXEC_ADDR)
    result = svc.preflight(plan, sender=SENDER, backend="generic")
    assert result.ok is False
    assert "unknown execution backend" in result.reason


# ------------------------------------------------------------- 6. imports --
def test_active_execution_service_imports_remain_valid():
    import execution.atomic as atomic
    import execution
    assert hasattr(atomic, "AtomicExecutionService")
    assert hasattr(atomic, "AtomicEnvelope")
    assert hasattr(atomic, "AtomicPreflight")
    assert hasattr(atomic.AtomicExecutionService, "build_arbexec_plan")
    assert hasattr(atomic.AtomicExecutionService, "preflight")
    assert hasattr(atomic.AtomicExecutionService, "build_arbexec_swap")
    for name in ("AtomicExecutionService", "AtomicEnvelope", "AtomicPreflight"):
        assert name in getattr(execution, "__all__", [])
