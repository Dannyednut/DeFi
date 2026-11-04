from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
OPTIMAL = ROOT / "optimizer" / "optimal.py"


# DSS-WO-ENG-002 note: the three former string-presence tests that asserted
# the RETIRED in-repo Exec variant's implementation details (fee-aware V2
# yield quoting inside Helper, fee-aware V2 math call counts in Exec, the old
# mode-guard wording and `getAmountInV2WithFee` debt repayment) were removed
# together with that variant. The authoritative Exec/Helper
# (Dannyednut/EVM @ 898fe82f) deliberately use different internals (plain
# getAmountOutV2/getAmountInV2 for V2 math); their behaviour is covered by
# tests/test_exec_contract_alignment.py, which verifies actual compilation,
# ABI conformance and Python encoding instead of source strings.


def test_optimizer_documents_and_implements_helper_sort_semantics():
    text = OPTIMAL.read_text()
    assert "high-price pool" in text
    assert "_get_pool_price_q96" in text
    assert "sorted_tokens = [t0, t1, t0] if borrow_token_smaller else [t1, t0, t1]" in text
    assert "_helper_scaling_factor" in text
    ast.parse(text)
