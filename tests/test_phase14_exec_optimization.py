from pathlib import Path
import ast
import json

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "Exec.sol"


def test_extended_exec_contains_fast_path_and_fee_aware_v2():
    text = CONTRACT.read_text()
    assert "function swapFast" in text
    assert "Helper.getAmountOutV2WithFee" in text
    assert "Helper.getAmountInV2WithFee" in text
    assert "function _getAmountOutV2WithFee" not in text
    assert "function _getAmountInV2WithFee" not in text
    assert "_validateArb(arb)" in text
    assert "function flashLoanAave" in text
    assert "function executeAtomic" in text
    assert "function multiCallAtomic" in text


def test_extended_abi_matches_new_entrypoints():
    abi = json.loads((ROOT / "executor" / "exec_extended_abi.json").read_text())
    names = {x.get("name") for x in abi if isinstance(x, dict)}
    assert {"swapFast", "flashLoanAave", "executeOperation", "executeAtomic", "multiCallAtomic"} <= names


def test_config_exposes_explicit_extended_address_helper():
    tree = ast.parse((ROOT / "config.py").read_text())
    funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "get_extended_arb_exec_address" in funcs
