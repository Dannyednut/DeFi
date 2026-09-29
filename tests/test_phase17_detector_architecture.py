import sys, types
from types import SimpleNamespace

# Keep this test dependency-light; project runtime may not have web3 installed.
fake_config = types.ModuleType("config")
fake_config.LOG_DIR = "/tmp"
fake_config.NATIVE_PRICE_USD = 3000.0
sys.modules.setdefault("config", fake_config)

from logger import Opportunity, OppType
from routing import ArbKind
from optimizer.opportunity_ranker import rank


def test_canonical_opportunity_has_distinct_arb_kind():
    o = Opportunity(
        opp_type=OppType.CROSS_PROTOCOL,
        chain_id=1,
        block_number=100,
        arb_kind=ArbKind.STABLE_IMBALANCE,
        metadata={"arb_kind": ArbKind.STABLE_IMBALANCE.value, "detector": "stable_imbalance"},
    )
    assert o.arb_kind == ArbKind.STABLE_IMBALANCE
    assert o.metadata["detector"] == "stable_imbalance"
    assert o.metadata["validation_status"] == "discovered"


def test_rank_does_not_merge_distinct_arb_kinds():
    common = dict(chain_id=1, block_number=100, tokens=["a", "b"], pools=["p1", "p2"], dexes=["d1", "d2"], net_profit_usd=10)
    a = Opportunity(opp_type=OppType.CROSS_PROTOCOL, arb_kind=ArbKind.CROSS_DEX_SPREAD, **common)
    b = Opportunity(opp_type=OppType.CROSS_PROTOCOL, arb_kind=ArbKind.STABLE_IMBALANCE, **common)
    ranked = rank([a, b])
    assert len(ranked) == 2
