from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "contracts" / "Exec.sol").read_text()


def test_callback_guard_and_operator_compatibility():
    assert "msg.sender != owner() && !operators[msg.sender] && msg.sender != address(this)" in SRC
    assert "_expectedCallback = BALANCER_VAULT" in SRC and "msg.sender != _expectedCallback" in SRC
    assert "msg.sender != _expectedCallback || msg.sender != AAVE_POOL" in SRC


def test_aave_repayment_is_checked_before_approval():
    assert "if (afterBalance < startBalance + owed + minProfit) revert NoProfit();" in SRC
    assert "_safeApprove(asset, AAVE_POOL, owed);" in SRC


def test_generic_calls_are_target_and_selector_allowlisted():
    assert "if (!allowedTargets[c.target] || c.data.length < 4) revert InvalidTarget();" in SRC
    assert "if (!allowedSelectors[c.target][selector]) revert SelectorNotAllowed();" in SRC
