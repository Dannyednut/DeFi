"""
tests/test_eng017_opportunity_identity.py
==========================================
DSS-WO-ENG-017 §5 regression coverage — one executable-opportunity identity
across OpportunityEngine deduplication and tracker lifecycle management.

PROBLEM (measured on deterministic detector-shaped fixtures, see
TestDetectorOverlapMeasurement): CycleDetector and CrossProtocolDetector
can independently report the SAME executable two-pool route — a cross-DEX
anchor-pair cycle — differing only in arb_kind ('cycle' vs
'cross_dex_spread'), opp_type and detector provenance. The pre-ENG-017
identity key included arb_kind (engine) and arb_kind+opp_type (tracker
fingerprint), so equivalent executable routes survived as separate
candidates, could be validated twice, queued twice and EXECUTED twice.

MEASUREMENT METHODOLOGY AND LIMITATIONS
  * Representative historical production data is NOT available in the
    repository (logs/ contains only git-ignored synthetic development
    fixtures with placeholder addresses; no replayable capture of
    detector outputs over live blocks exists). No production frequency
    statistics are claimed anywhere.
  * The measurement therefore uses DETERMINISTIC detector-shaped fixtures:
    candidates constructed exactly as CycleDetector.on_block /
    CrossProtocolDetector.on_block / StableImbalanceDetector.on_block /
    PendingOpportunity.to_opportunity build them for a fixed route, so the
    identity rules are proven reproducibly rather than statistically.

POLICY PINNED HERE
  1. Equivalent routes discovered by different detectors are consolidated
     into ONE canonical candidate (strongest validated quote wins).
  2. Detector provenance and useful research information of absorbed
     candidates is RETAINED (detector_provenance / consolidated_theses).
  3. Distinct routes (different pools, different borrowed root token) and
     genuinely distinct execution opportunities remain separate.
  4. Deduplication is stable across repeated blocks and mempool promotion
     (one lifecycle record with UPDATEs, never one OPEN per thesis).
  5. A duplicate cannot enter execution twice through different detector
     sources (the router's fingerprint dedup uses the same identity).
"""
import os
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Light-stdlib fallback for the engine import (same pattern as
# tests/test_phase17_engine.py) — harmless when the real config module is
# already importable in the session.
if "config" not in sys.modules:
    try:
        import config  # noqa: F401
    except Exception:
        fake_config = types.ModuleType("config")
        fake_config.NATIVE_PRICE_USD = 3000.0
        sys.modules["config"] = fake_config

from eth_utils import to_checksum_address

from logger import Opportunity, OppType
from routing import ArbKind
from opportunity_engine import OpportunityEngine
from tracker import OpportunityTracker, _fingerprint
from logger import OppLogger

CHAIN = 1
WETH = to_checksum_address("0x" + "c0" * 20)
USDC = to_checksum_address("0x" + "a0" * 20)
P1 = to_checksum_address("0x" + "11" * 20)   # WETH/USDC uniswap_v3
P2 = to_checksum_address("0x" + "22" * 20)   # WETH/USDC sushiswap_v2
P3 = to_checksum_address("0x" + "33" * 20)   # unrelated pool
OTHER = to_checksum_address("0x" + "d0" * 20)


def _cycle_thesis_candidate(quote: int = 500_000) -> Opportunity:
    """Exactly the shape CycleDetector.simulate_and_build emits for a
    2-pool cross-DEX WETH cycle over P1→P2 (arb_kind='cycle')."""
    return Opportunity(
        opp_type=OppType.DEX_SPREAD,
        chain_id=CHAIN,
        arb_kind=ArbKind.CYCLE,
        block_number=100,
        tokens=[WETH, USDC, WETH],
        pools=[P1, P2],
        dexes=["uniswap_v3", "sushiswap_v2"],
        estimated_profit_usd=12.0,
        net_profit_usd=9.0,
        gas_cost_usd=0.4,
        metadata={
            "detector": "cycles",
            "arb_kind": ArbKind.CYCLE.value,
            "hop_count": 2,
            "quote_profit_wei": quote,
            "execution_supported": True,
        },
    )


def _cross_protocol_thesis_candidate(quote: int = 420_000) -> Opportunity:
    """Exactly the shape CrossProtocolDetector.on_block emits when the
    anchor pair WETH/USDC is fragmented across the SAME two pools
    (arb_kind='cross_dex_spread')."""
    return Opportunity(
        opp_type=OppType.CROSS_PROTOCOL,
        chain_id=CHAIN,
        arb_kind=ArbKind.CROSS_DEX_SPREAD,
        block_number=100,
        tokens=[WETH, USDC, WETH],
        pools=[P1, P2],
        dexes=["uniswap_v3", "sushiswap_v2"],
        estimated_profit_usd=0.0,
        net_profit_usd=0.0,
        gas_cost_usd=0.0,
        metadata={
            "detector": "cross_proto",
            "arb_kind": ArbKind.CROSS_DEX_SPREAD.value,
            "spread_pct": 0.42,
            "buy_protocol": "uniswap_v3",
            "sell_protocol": "sushiswap_v2",
            "quote_profit_wei": quote,
            "execution_supported": True,
        },
    )


def _stable_thesis_candidate(quote: int = 300_000) -> Opportunity:
    """StableImbalanceDetector-shaped candidate for the same route."""
    return Opportunity(
        opp_type=OppType.CROSS_PROTOCOL,
        chain_id=CHAIN,
        arb_kind=ArbKind.STABLE_IMBALANCE,
        block_number=100,
        tokens=[WETH, USDC, WETH],
        pools=[P1, P2],
        dexes=["uniswap_v3", "sushiswap_v2"],
        estimated_profit_usd=0.0,
        net_profit_usd=0.0,
        gas_cost_usd=0.0,
        metadata={
            "detector": "stable_imbalance",
            "arb_kind": ArbKind.STABLE_IMBALANCE.value,
            "spread_pct": 0.31,
            "curve_pool": P2,
            "comparison_pool": P1,
            "quote_profit_wei": quote,
            "execution_supported": True,
        },
    )


class TestDetectorOverlapMeasurement(unittest.TestCase):
    """Deterministic overlap measurement over detector-shaped fixtures."""

    def test_three_theses_on_one_route_consolidate_to_one_candidate(self):
        candidates = [
            _cycle_thesis_candidate(quote=500_000),
            _cross_protocol_thesis_candidate(quote=420_000),
            _stable_thesis_candidate(quote=300_000),
        ]
        # PRE-ENG-017 identity (arb_kind in the key): 3 surviving candidates
        # = 3 validation runs and 3 executable queue slots for ONE route.
        pre_keys = {
            (o.chain_id, o.arb_kind.value,
             tuple(x.lower() for x in o.tokens),
             tuple(x.lower() for x in o.pools),
             tuple(x.lower() for x in o.dexes))
            for o in candidates
        }
        self.assertEqual(len(pre_keys), 3)

        # POST-ENG-017 identity (executable-route identity): 1 candidate.
        consolidated = OpportunityEngine._dedupe(list(candidates))
        self.assertEqual(len(consolidated), 1)
        self.assertEqual(len(candidates) - len(consolidated), 2)

        canonical = consolidated[0]
        # The strongest valid evidence wins.
        self.assertEqual(canonical.metadata["quote_profit_wei"], 500_000)
        self.assertEqual(canonical.metadata["detector"], "cycles")
        # Provenance is retained, not discarded.
        prov = canonical.metadata["detector_provenance"]
        self.assertIn("cross_proto", prov)
        self.assertIn("stable_imbalance", prov)
        self.assertEqual(prov["cross_proto"]["arb_kind"], "cross_dex_spread")
        self.assertEqual(prov["cross_proto"]["alt_quote_profit_wei"], 420_000)
        self.assertEqual(prov["cross_proto"]["spread_pct"], 0.42)
        self.assertEqual(prov["stable_imbalance"]["curve_pool"], P2)
        self.assertEqual(sorted(canonical.metadata["consolidated_theses"]),
                         ["cross_dex_spread", "cycle", "stable_imbalance"])
        # The canonical candidate still carries its own thesis unchanged.
        self.assertEqual(canonical.arb_kind, ArbKind.CYCLE)

    def test_distinct_routes_remain_separate(self):
        different_pools = _cycle_thesis_candidate()
        different_pools.pools = [P1, P3]
        different_root = _cycle_thesis_candidate()
        different_root.tokens = [USDC, WETH, USDC]
        different_chain = _cycle_thesis_candidate()
        different_chain.chain_id = 56

        out = OpportunityEngine._dedupe([
            _cycle_thesis_candidate(),
            _cross_protocol_thesis_candidate(),  # same route → merged
            different_pools,                      # distinct → kept
            different_root,                       # distinct → kept
            different_chain,                      # distinct → kept
        ])
        self.assertEqual(len(out), 4)


class TestFingerprintIdentity(unittest.TestCase):

    def test_thesis_fields_do_not_split_the_identity(self):
        fp_cycle = _fingerprint(_cycle_thesis_candidate())
        fp_cross = _fingerprint(_cross_protocol_thesis_candidate())
        fp_stable = _fingerprint(_stable_thesis_candidate())
        self.assertEqual(fp_cycle, fp_cross)
        self.assertEqual(fp_cycle, fp_stable)

        # Semantic classification (opp_type) is not part of the identity.
        renamed = _cycle_thesis_candidate()
        renamed.opp_type = OppType.CROSS_PROTOCOL
        self.assertEqual(_fingerprint(renamed), fp_cycle)

    def test_execution_relevant_differences_change_the_identity(self):
        base = _fingerprint(_cycle_thesis_candidate())

        rotated = _cycle_thesis_candidate()          # different borrow root
        rotated.tokens = [USDC, WETH, USDC]
        self.assertNotEqual(_fingerprint(rotated), base)

        other_pools = _cycle_thesis_candidate()      # different route
        other_pools.pools = [P1, P3]
        self.assertNotEqual(_fingerprint(other_pools), base)

        other_chain = _cycle_thesis_candidate()      # different chain
        other_chain.chain_id = 56
        self.assertNotEqual(_fingerprint(other_chain), base)

    def test_identity_matches_engine_identity_key(self):
        for factory in (_cycle_thesis_candidate, _cross_protocol_thesis_candidate,
                        _stable_thesis_candidate):
            opp = factory()
            self.assertEqual(_fingerprint(opp),
                             _fingerprint(opp))  # stable within an object
            key = OpportunityEngine._identity_key(opp)
            self.assertEqual(key[0], opp.chain_id)
            self.assertEqual(key[1], tuple(t.lower() for t in opp.tokens))
            self.assertEqual(key[2], tuple(p.lower() for p in opp.pools))


class _NoopOppLogger(OppLogger):
    """OppLogger writing nowhere (no filesystem traffic in tests)."""

    def __init__(self):  # noqa: super().__init__ not needed
        pass

    def log(self, opp):
        pass

    def close(self):
        pass


class TestTrackerLifecycleConsolidation(unittest.TestCase):
    """Deduplication remains stable across repeated blocks and mempool
    promotion: one lifecycle record with UPDATEs, never one OPEN per thesis."""

    def test_same_route_from_two_theses_opens_once_and_updates(self):
        tracker = OpportunityTracker(_NoopOppLogger())
        tracker.process_block(100, [_cycle_thesis_candidate()])
        tracker.process_block(101, [_cross_protocol_thesis_candidate()])
        tracker.process_block(102, [_stable_thesis_candidate()])
        self.assertEqual(len(tracker._active), 1)
        (active,) = tracker._active.values()
        self.assertEqual(active.open_block, 100)
        # The tracker counts the opening-block sighting as the first update
        # (existing lifecycle semantics), so blocks 100/101/102 → 3.
        self.assertEqual(active.update_count, 3)

    def test_repeated_blocks_keep_the_fingerprint_stable(self):
        block_a = _cycle_thesis_candidate()
        block_b = _cycle_thesis_candidate()
        block_b.metadata["quote_profit_wei"] = 510_000  # refreshed quote
        self.assertEqual(_fingerprint(block_a), _fingerprint(block_b))

        promoted = _cross_protocol_thesis_candidate()  # mempool-shaped thesis
        self.assertEqual(_fingerprint(block_a), _fingerprint(promoted))


class TestRouterRejectsDuplicateDetectorSources(unittest.TestCase):
    """A duplicate cannot enter execution twice through different detector
    sources: the router queues by the same executable-route fingerprint."""

    def setUp(self):
        self._env = patch.dict(os.environ, {"EXECUTE_ONCHAIN": "true"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _router(self):
        from execution_router import ExecutionRouter, OppSource
        executor = MagicMock()
        executor._arb_exec = MagicMock()          # executor "ready"
        executor.chain_id = CHAIN
        router = ExecutionRouter(MagicMock(), executor)
        return router, OppSource

    def _execution_ready(self, factory):
        opp = factory()
        opp.metadata["execution_supported"] = True
        opp.metadata["execution_simulated"] = True
        opp.metadata["execution_ready"] = True
        opp.estimated_profit_usd = 50.0  # above the streaming threshold
        opp.net_profit_usd = 40.0
        return opp

    def test_second_detector_source_is_refused(self):
        router, OppSource = self._router()
        first = self._execution_ready(_cycle_thesis_candidate)
        second = self._execution_ready(_cross_protocol_thesis_candidate)
        self.assertNotEqual(first.metadata["detector"], second.metadata["detector"])

        self.assertTrue(router.submit_opportunity(first, OppSource.DETECTOR_CYCLES))
        # Same executable route via a different detector source: refused.
        self.assertFalse(router.submit_opportunity(second, OppSource.DETECTOR_CROSS_PROTO))
        self.assertEqual(len(router._queued_opps), 1)

    def test_distinct_routes_are_both_queued(self):
        router, OppSource = self._router()
        a = self._execution_ready(_cycle_thesis_candidate)
        b = self._execution_ready(_cycle_thesis_candidate)
        b.pools = [P1, P3]  # genuinely different route
        self.assertTrue(router.submit_opportunity(a, OppSource.DETECTOR_CYCLES))
        self.assertTrue(router.submit_opportunity(b, OppSource.DETECTOR_CYCLES))
        self.assertEqual(len(router._queued_opps), 2)


class _FakeEth:
    gas_price = 1
    block_number = 100

    def get_block(self, n):
        return {"number": n, "hash": b"\x01" * 32}


class _FakeW3:
    eth = _FakeEth()


class TestEngineDiscoveryConsolidatesAcrossDetectors(unittest.TestCase):
    """End-to-end through _discover: two registered detectors reporting the
    same route produce ONE canonical candidate with merged provenance."""

    def _detector(self, name, kind, factory):
        class _D:
            pass
        d = _D()
        d.name = name
        d.arb_kind = kind
        d.requires_touched_pools = False
        d.block_interval = 1
        # The engine passes touched_pools= to the 'cycles' detector and
        # positional-only args to the others.
        d.on_block = (lambda block_number, touched_pools=None, factory=factory: [factory()])
        return d

    def test_two_detectors_one_route(self):
        engine = OpportunityEngine(_FakeW3(), CHAIN, detectors=[
            self._detector("cycles", ArbKind.CYCLE, _cycle_thesis_candidate),
            self._detector("cross_proto", ArbKind.CROSS_DEX_SPREAD,
                           _cross_protocol_thesis_candidate),
        ])
        result = engine.process(100)
        self.assertEqual(len(result.candidates), 1)
        canonical = result.candidates[0]
        # Consolidation happened BEFORE validation; the surviving thesis is
        # the strongest-quote candidate's.
        self.assertEqual(canonical.metadata["detector"], "cycles")
        self.assertIn("cross_proto", canonical.metadata["detector_provenance"])
        # Validation ran against the fake chain and correctly rejected the
        # candidate (fail closed) — no execution-ready duplicate either way.
        self.assertEqual(len(result.ready), 0)
        self.assertNotEqual(canonical.metadata.get("validation_status"),
                            "execution_simulated")


if __name__ == "__main__":
    unittest.main()
