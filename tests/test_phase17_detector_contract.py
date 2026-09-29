from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_detectors_have_one_thesis_each():
    files = {
        "CycleDetector": (ROOT / "detectors" / "cycles.py").read_text(),
        "NewPoolDetector": (ROOT / "detectors" / "specialized.py").read_text(),
        "CrossProtocolDetector": (ROOT / "detectors" / "protocol_opportunities.py").read_text(),
        "StableImbalanceDetector": (ROOT / "detectors" / "protocol_opportunities.py").read_text(),
        "SplitRouteDetector": (ROOT / "detectors" / "advanced.py").read_text(),
        "LiquidationScanner": (ROOT / "detectors" / "specialized.py").read_text(),
    }
    expected = {
        "CycleDetector": "arb_kind = ArbKind.CYCLE",
        "NewPoolDetector": "arb_kind = ArbKind.NEW_POOL",
        "CrossProtocolDetector": "arb_kind = ArbKind.CROSS_DEX_SPREAD",
        "StableImbalanceDetector": "arb_kind = ArbKind.STABLE_IMBALANCE",
        "SplitRouteDetector": "arb_kind = ArbKind.SPLIT_ROUTE",
        "LiquidationScanner": "arb_kind = ArbKind.LIQUIDATION",
    }
    for name, marker in expected.items():
        assert marker in files[name]
    assert len(expected) == len(set(expected.values()))

def test_no_cross_detector_new_pool_or_split_duplication():
    advanced = (ROOT / "detectors" / "advanced.py").read_text()
    specialized = (ROOT / "detectors" / "specialized.py").read_text()
    protocol = (ROOT / "detectors" / "protocol_opportunities.py").read_text()
    assert 'arb_kind = ArbKind.SPLIT_ROUTE' in advanced
    assert 'arb_kind = ArbKind.NEW_POOL' in specialized
    assert 'arb_kind = ArbKind.CROSS_DEX_SPREAD' in protocol
    assert 'NEW_POOL_CROSS_DEX' not in advanced
