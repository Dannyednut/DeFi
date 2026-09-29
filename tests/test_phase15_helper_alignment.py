from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
EXEC = ROOT / "contracts" / "Exec.sol"
HELPER = ROOT / "contracts" / "libraries" / "Helper.sol"
OPTIMAL = ROOT / "optimizer" / "optimal.py"


def test_helper_is_present_and_fee_aware_profit_quote_is_canonical():
    text = HELPER.read_text()
    assert "function getAmountOutV2WithFee" in text
    assert "function getAmountInV2WithFee" in text
    assert "current = tokens[i] == _token0(pools[i])" in text
    assert "getAmountOutV2WithFee(current, r0, r1, fees[i])" in text
    assert "getAmountOutV2(current, r0, r1)" not in text


def test_exec_delegates_v2_fee_math_to_helper():
    text = EXEC.read_text()
    assert "function _getAmountOutV2WithFee" not in text
    assert "function _getAmountInV2WithFee" not in text
    assert text.count("Helper.getAmountOutV2WithFee") >= 3
    assert "Helper.getAmountInV2WithFee" in text
    assert "Helper._validatePoolTokens(arb.tokens, arb.pools)" in text
    assert "arb.fees[i] >= 1_000_000" in text


def test_optimizer_documents_and_implements_helper_sort_semantics():
    text = OPTIMAL.read_text()
    assert "high-price pool" in text
    assert "_get_pool_price_q96" in text
    assert "sorted_tokens = [t0, t1, t0] if borrow_token_smaller else [t1, t0, t1]" in text
    assert "_helper_scaling_factor" in text
    ast.parse(text)


def test_exec_mode_one_uses_far_side_flashswap_and_v2_fee_repayment():
    text = EXEC.read_text()
    assert "if (arb.mode != 0) revert BadPath();" in text
    assert "address borrowToken = arb.tokens[1];" in text
    assert "bool z = (borrowToken == Helper._token0(pool0));" in text
    assert "bool isT0 = borrowToken == Helper._token0(address(pair));" in text
    assert "Helper.getAmountInV2WithFee(borrowed, r0, r1, arb.fees[0])" in text
