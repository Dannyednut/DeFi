import sys, types

fake_config = types.ModuleType("config")
fake_config.NATIVE_PRICE_USD = 3000.0
sys.modules.setdefault("config", fake_config)

from logger import Opportunity, OppType
from routing import ArbKind
from opportunity_engine import OpportunityEngine


class FakeEth:
    gas_price = 1
    block_number = 100
    def get_block(self, n):
        return {"number": n, "hash": b"\\x01" * 32}


class FakeW3:
    eth = FakeEth()


def test_engine_fails_closed_for_unvalidated_candidates():
    class Detector:
        name = "fake"
        arb_kind = ArbKind.CROSS_DEX_SPREAD
        def on_block(self, block_number):
            return [Opportunity(
                opp_type=OppType.CROSS_PROTOCOL, chain_id=1, block_number=block_number,
                arb_kind=ArbKind.CROSS_DEX_SPREAD,
                tokens=["a", "b", "a"], pools=["p1", "p2"], dexes=["d1", "d2"],
                metadata={"execution_supported": False},
            )]
    result = OpportunityEngine(FakeW3(), 1, detectors=[Detector()]).process(100)
    assert not result.ready
    assert len(result.rejected) == 1
