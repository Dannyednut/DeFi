from pathlib import Path
import ast
import sys, types

fake_config = types.ModuleType("config")
fake_config.NATIVE_PRICE_USD = 3000.0
sys.modules.setdefault("config", fake_config)

from logger import Opportunity, OppType, ValidationStage
from routing import ArbKind
from opportunity_engine import OpportunityEngine

ROOT = Path(__file__).resolve().parents[1]

class FakeEth:
    gas_price = 1
    block_number = 100
    def get_block(self, n): return {"number": n, "hash": b"\\x01" * 32}

class FakeW3:
    eth = FakeEth()

def test_engine_preserves_detector_kind_and_fails_closed():
    class Detector:
        name = "cross_proto"
        arb_kind = ArbKind.CROSS_DEX_SPREAD
        def on_block(self, block_number):
            return [Opportunity(opp_type=OppType.CROSS_PROTOCOL, chain_id=1, block_number=block_number,
                arb_kind=ArbKind.CROSS_DEX_SPREAD, tokens=["a","b","a"], pools=["p1","p2"],
                dexes=["d1","d2"], metadata={"execution_supported": False})]
    r = OpportunityEngine(FakeW3(), 1, detectors=[Detector()]).process(100)
    assert not r.ready and len(r.rejected) == 1
    assert r.candidates[0].metadata["arb_kind"] == "cross_dex_spread"

def test_cycle_detector_does_not_own_two_pool_spreads():
    src = (ROOT / "detectors" / "cycles.py").read_text()
    assert 'cycles = [c for c in cycles if len(getattr(c, "pools", [])) >= 3]' in src

def test_main_uses_single_engine_and_no_legacy_batch_execution():
    src = (ROOT / "main.py").read_text()
    assert 'opportunity_engine = OpportunityEngine' in src
    assert 'mempool = MempoolPipeline(w3, graph, tracker, cache=cache, router=router, opportunity_engine=opportunity_engine)' in src
    assert 'router.submit_batch_opportunities(all_opps[:12]' not in src

def test_validation_stage_has_adversarial_state():
    assert ValidationStage.ADVERSARIAL_SIMULATED.value == "adversarial_simulated"
