from pathlib import Path
import json


def test_atomic_executor_source_exists_and_is_fail_closed():
    source = Path(__file__).resolve().parents[1] / "contracts" / "AtomicArbExecutor.sol"
    text = source.read_text()
    assert "function executeAtomic(" in text
    assert "revert CallFailed" in text
    assert "InsufficientProfit" in text
    assert "allowedSelectors" in text
    assert "onlyOperator" in text


def test_atomic_executor_abi_exposes_atomic_entrypoint():
    path = Path(__file__).resolve().parents[1] / "executor" / "atomic_executor_abi.json"
    abi = json.loads(path.read_text())
    fn = next(x for x in abi if x.get("type") == "function" and x.get("name") == "executeAtomic")
    assert fn["stateMutability"] == "payable"
    assert fn["inputs"][-1]["type"] == "tuple[]"


def test_phase12_module_still_imports_without_web3():
    import execution.atomic as atomic
    assert hasattr(atomic, "AtomicExecutionService")
