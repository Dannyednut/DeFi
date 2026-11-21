"""DSS-WO-MI-105 — Shadow Evidence Integrity (offline tests).

Covers, per work order §6 (synthetic fixtures only, no live RPC):
- WS-A: successful refresh produces truthful FRESH status at the block the
  reads were pinned to; failed/partial refreshes stay UNCERTAIN/unavailable;
  the eth_call batch is pinned; cache persistence keeps provenance across
  reload; cache-loaded state without recorded status is UNKNOWN, not STALE;
  the engine's route-status aggregation is truthful (UNKNOWN never STALE).
- WS-B: validation rejection evidence (status, sanitized reason, block
  number/hash) survives serialization; never-validated vs validated-rejected
  are distinguished; execution-support limitations stay separate.
- WS-C: known quote-size evidence (the Exec contract's selected input) is
  preserved with its denomination and mode; missing quote evidence stays
  explicitly null.
- WS-D: tracker lifecycle expiry is detector-schedule aware (no false CLOSE
  when a detector was not scheduled); mempool-origin candidates expire by
  TTL; the bounded hard-TTL still closes eventually; shadow CLOSE records
  exist for non-execution-ready candidates.
- Schema compatibility (v2 additive) and replay source immutability.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from block_information import BlockContext, PoolStateChange, ProtocolStateSynchronizer
from cache import PoolCache
from graph import TokenGraph, PoolEdge
from tracker import OpportunityTracker
from validation.opportunity import OpportunityValidator, validate_many
import utils.multicall3 as mc3
from shadow import recorder as shadow_rec
from shadow.recorder import ShadowRecorder


def _load_mi103_helpers():
    spec = importlib.util.spec_from_file_location(
        "mi103_helpers", REPO / "tests" / "test_mi103_shadow_validation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load_mi103_helpers()
P1, P2, P3 = T.P1, T.P2, T.P3
WETH, A, B = T.WETH, T.A, T.B
STATE_HASH = T.STATE_HASH


def _cache(tmp: Path, name: str = "pools_1.json") -> PoolCache:
    # PoolCache(chain_id, cache_dir) — the file name derives from the chain id
    return PoolCache(1, cache_dir=str(tmp))


def _sync(graph, cache=None) -> ProtocolStateSynchronizer:
    return ProtocolStateSynchronizer(T.StubW3(), graph, cache)


def _v2_graph() -> TokenGraph:
    """MI-103 fixture graph whose edges carry a real V2 protocol tag so the
    synchronizer routes the pools through the pinned multicall3 path."""
    graph = T._graph()
    for pool in (P1, P2, P3):
        graph.upsert_pool_edges([
            replace(e, protocol="uniswap_v2")
            for e in graph.get_edges_for_pool(pool)
        ])
    return graph


def _ctx(number: int = 500, pools=(P1,), hash_: str | None = None) -> BlockContext:
    h = hash_ or ("0x" + "cd" * 32)
    return BlockContext(
        chain_id=1, number=number, block_hash=h, parent_hash="", timestamp=0,
        state_changes=[PoolStateChange(1, number, h, p, "uniswap_v2", "Sync")
                       for p in pools],
    )


def _set_status(graph, pool: str, status: str, block: int = 100) -> None:
    graph.upsert_pool_edges([
        replace(e, state_status=status, state_block=block)
        for e in graph.get_edges_for_pool(pool)
    ])


# ══════════════════════════════════════════════════════════════════════════════
# WS-A — market-state provenance and freshness
# ══════════════════════════════════════════════════════════════════════════════

class TestRefreshProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mi105_a_"))
        self.graph = _v2_graph()
        _set_status(self.graph, P1, "STALE")  # cache-loaded-era status

    def test_successful_refresh_stamps_fresh_at_pinned_block(self):
        cache = _cache(self.tmp)
        cache.set_pool(P1.lower(), {"token0": WETH.lower(), "token1": A.lower(),
                                    "dex": "UniV2", "version": 2,
                                    "r0": 10**18, "r1": 2 * 10**18,
                                    "tvl_usd": 250_000.0})
        seen_blocks = []

        def fake_refresh(addresses, *, block_identifier=None):
            seen_blocks.append(block_identifier)
            return set(addresses)

        sync = _sync(self.graph, cache)
        sync._refresh_multicall3 = fake_refresh
        ctx = _ctx(number=500)
        refreshed = sync.refresh(ctx)

        self.assertEqual(refreshed, {P1.lower()})
        self.assertEqual(seen_blocks, [500])  # read pinned to the trigger block
        e = self.graph.get_edges_for_pool(P1)[0]
        self.assertEqual(e.state_status, "FRESH")
        self.assertEqual(e.state_block, 500)
        self.assertEqual(e.state_block_hash, "0x" + "cd" * 32)
        self.assertTrue(e.state_source.startswith("onchain:uniswap_v2"))
        # provenance persisted into the cache too (MI-105 §2 requirement 5)
        cached = cache._pools[P1.lower()]
        self.assertEqual(cached["state_status"], "FRESH")
        self.assertEqual(cached["state_block"], 500)

    def test_failed_refresh_never_fresh(self):
        sync = _sync(self.graph)

        def boom(addresses, *, block_identifier=None):
            raise RuntimeError("rpc down")

        sync._refresh_multicall3 = boom
        refreshed = sync.refresh(_ctx(number=501))
        self.assertEqual(refreshed, set())
        e = self.graph.get_edges_for_pool(P1)[0]
        self.assertEqual(e.state_status, "UNCERTAIN")
        self.assertIn("multicall_error", e.state_source)
        self.assertEqual(e.state_block, 100)  # prior provenance untouched

    def test_incomplete_adapter_refresh_not_fresh(self):
        import unittest.mock as mock
        graph = _v2_graph()
        for e in graph.get_edges_for_pool(P1):
            e.protocol = "curve"
        sync = _sync(graph)
        sync._refresh_multicall3 = lambda a, *, block_identifier=None: set()
        with mock.patch("registry.refresh_pools_batched", return_value=set()):
            refreshed = sync.refresh(_ctx(number=502, pools=(P1,)))
        self.assertEqual(refreshed, set())
        e = graph.get_edges_for_pool(P1)[0]
        self.assertEqual(e.state_status, "UNCERTAIN")
        self.assertEqual(e.state_source, "adapter_refresh_incomplete")

    def test_batched_reads_are_pinned_to_the_given_block(self):
        captured = {}

        class FakeFunctions:
            def aggregate3(self, calls):
                captured["calls"] = calls
                holder = self

                class Pending:
                    def call(self, kwargs, block_identifier=None):
                        captured["kwargs"] = kwargs
                        captured["block_identifier"] = block_identifier
                        return [(True, b"\x00" * 96)] * len(calls)

                return Pending()

        class FakeMc:
            functions = FakeFunctions()

        graph = T._graph()
        orig = {
            "get": mc3._get_multicall3, "is_v3": mc3._is_v3,
            "enc": mc3._encode_get_reserves, "dec": mc3._decode_get_reserves,
        }
        mc3._get_multicall3 = lambda w3: FakeMc()
        mc3._is_v3 = lambda graph, addr: False
        mc3._encode_get_reserves = lambda w3, addr: b"\x00"
        mc3._decode_get_reserves = lambda w3, addr, data: (10**18, 2 * 10**18, 0)
        try:
            updated = mc3.multicall3_bulk_refresh(
                T.StubW3(), graph, None, {P1.lower()}, block_identifier=777)
        finally:
            mc3._get_multicall3, mc3._is_v3 = orig["get"], orig["is_v3"]
            mc3._encode_get_reserves, mc3._decode_get_reserves = orig["enc"], orig["dec"]
        self.assertEqual(updated, {P1.lower()})
        self.assertEqual(captured["block_identifier"], 777)

    def test_partial_v3_read_is_not_a_complete_refresh(self):
        class FakeFunctions:
            def aggregate3(self, calls):
                holder = self

                class Pending:
                    def call(self, kwargs, block_identifier=None):
                        # slot0 succeeds, liquidity call FAILS → partial read
                        return [(True, b"\x01" * 40), (False, b"")]

                return Pending()

        class FakeMc:
            functions = FakeFunctions()

        graph = T._graph()
        orig = {
            "get": mc3._get_multicall3, "is_v3": mc3._is_v3,
            "s0e": mc3._encode_slot0, "s0d": mc3._decode_slot0,
            "lie": mc3._encode_liquidity, "lid": mc3._decode_liquidity,
            "enc": mc3._encode_get_reserves, "dec": mc3._decode_get_reserves,
        }
        mc3._get_multicall3 = lambda w3: FakeMc()
        mc3._is_v3 = lambda graph, addr: True
        mc3._encode_slot0 = lambda w3, addr: b"\x00"
        mc3._encode_liquidity = lambda w3, addr: b"\x00"
        mc3._decode_slot0 = lambda w3, addr, data: (123, 4, 0, 0, 0, 0, True)
        mc3._decode_liquidity = lambda w3, addr, data: 5
        mc3._encode_get_reserves = orig["enc"]
        mc3._decode_get_reserves = orig["dec"]
        try:
            updated = mc3.multicall3_bulk_refresh(T.StubW3(), graph, None, {P1.lower()})
        finally:
            for k, v in orig.items():
                setattr(mc3, {"get": "_get_multicall3", "is_v3": "_is_v3",
                              "s0e": "_encode_slot0", "s0d": "_decode_slot0",
                              "lie": "_encode_liquidity", "lid": "_decode_liquidity",
                              "enc": "_encode_get_reserves",
                              "dec": "_decode_get_reserves"}[k], v)
        self.assertEqual(updated, set())  # partial read must not count as refreshed

    def test_cache_reload_keeps_persisted_provenance_and_defaults_unknown(self):
        import unittest.mock as mock
        cache = _cache(self.tmp)
        cache.set_pool(P1.lower(), {"token0": WETH.lower(), "token1": A.lower(),
                                    "dex": "UniV2", "version": 2,
                                    "r0": 10**18, "r1": 2 * 10**18,
                                    "tvl_usd": 250_000.0})
        cache.set_pool(P2.lower(), {"token0": A.lower(), "token1": B.lower(),
                                    "dex": "UniV2", "version": 2,
                                    "r0": 2 * 10**18, "r1": 2 * 10**18,
                                    "tvl_usd": 250_000.0})
        cache.update_pool_state(P1.lower(), state_block=500,
                                state_block_hash="0x" + "cd" * 32,
                                state_source="onchain:uniswap_v2:block_log",
                                state_status="FRESH")
        cache.save()

        cache2 = _cache(self.tmp)
        self.assertTrue(cache2.load())
        graph = TokenGraph()
        with mock.patch("tvl.is_above_min_liquidity", return_value=True):
            restored = cache2.warm_graph(graph)  # provenance is the test target, not TVL
        self.assertGreater(restored, 0)
        e = graph.get_edges_for_pool(P1)[0]
        self.assertEqual(e.state_status, "FRESH")
        self.assertEqual(e.state_block, 500)

        # a cache WITHOUT recorded status defaults to UNKNOWN, never STALE
        entry = cache2._pools.get(P2.lower())
        if entry is not None:
            entry.pop("state_status", None)
            cache2.save()
            cache3 = _cache(self.tmp)
            cache3.load()
            graph3 = TokenGraph()
            with mock.patch("tvl.is_above_min_liquidity", return_value=True):
                cache3.warm_graph(graph3)
            e3 = graph3.get_edges_for_pool(P2)[0]
            self.assertEqual(e3.state_status, "UNKNOWN")


class TestEngineRouteAggregation(unittest.TestCase):
    def test_uniform_fresh_route_is_fresh(self):
        self.assertEqual(
            T.OpportunityEngine.aggregate_route_status({"FRESH"}), "FRESH")

    def test_unknown_is_never_reported_as_stale(self):
        self.assertEqual(
            T.OpportunityEngine.aggregate_route_status({"UNKNOWN"}), "UNKNOWN")

    def test_mixed_statuses_are_uncertain(self):
        self.assertEqual(
            T.OpportunityEngine.aggregate_route_status({"FRESH", "STALE"}),
            "UNCERTAIN")
        self.assertEqual(
            T.OpportunityEngine.aggregate_route_status({"FRESH", "UNKNOWN"}),
            "UNKNOWN")

    def test_invalid_dominates(self):
        self.assertEqual(
            T.OpportunityEngine.aggregate_route_status({"INVALID", "FRESH"}),
            "INVALID")

    def test_finalize_wiring_uses_the_helper(self):
        # integration: the discovery path (process → _discover → finalize)
        # annotates candidates through the same truthful helper
        graph = _v2_graph()
        _set_status(graph, P1, "FRESH", block=99)
        _set_status(graph, P2, "FRESH", block=99)

        class FakeDetector:
            name = "cycles"
            block_interval = 1
            requires_touched_pools = False

            def on_block(self, block_number, touched_pools=None):
                return [T._opp([P1, P2], [WETH, A, WETH], block=block_number)]

        engine = T._engine(graph)
        engine.detectors = [FakeDetector()]
        engine.scheduled_discovery = False
        result = engine.process(100, touched_pools={P1.lower(), P2.lower()})
        self.assertTrue(result.candidates)
        for c in result.candidates:
            self.assertEqual(c.metadata["route_state_status"], "FRESH")
        self.assertEqual(result.scheduled_detectors, ("cycles",))


# ══════════════════════════════════════════════════════════════════════════════
# WS-B — validation rejection evidence
# ══════════════════════════════════════════════════════════════════════════════

class RecorderBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mi105_rec_"))
        self.path = self.tmp / "shadow_validation.jsonl"
        self.recorder = ShadowRecorder(self.path, tail_scan_lines=10_000)
        shadow_rec.set_recorder(self.recorder)

    def tearDown(self):
        shadow_rec.set_recorder(None)
        shadow_rec.reset_recorder()

    def records(self):
        return self.recorder.read_records()


class TestValidationEvidence(RecorderBase):
    REJECT_META = {
        "validation_status": "rejected",
        "validation_reason": "state moved from candidate block 100 to head 105",
        "validation_block": 100,
        "validation_block_hash": "0x" + "aa" * 32,
        "execution_simulated": False,
        "execution_ready": False,
        "quote_profit_wei": 0,
        "validated_profit_floor_wei": 0,
        "validated_gas_units": 0,
    }

    def test_rejected_candidate_retains_reason_and_block_context(self):
        engine = T._engine(T._graph())
        opp = T._opp([P1, P2], [WETH, A, WETH], meta=dict(self.REJECT_META))
        engine.process_candidates([opp], 100)
        det = [r for r in self.records() if r.get("record_type") == "detection"][0]
        v = det["validation"]
        self.assertTrue(v["performed"])            # validated and rejected
        self.assertEqual(v["status"], "rejected")
        self.assertEqual(v["reason"], self.REJECT_META["validation_reason"])
        self.assertEqual(v["validation_block"], 100)
        self.assertEqual(v["validation_block_hash"], "0x" + "aa" * 32)
        self.assertFalse(v["execution_simulated"])
        # v1 fields still present (eligibility keeps the status for compat)
        self.assertEqual(det["eligibility"]["validation_status"], "rejected")
        # survives a JSON serialization round-trip
        round_trip = json.loads(json.dumps(det))
        self.assertEqual(round_trip["validation"], v)

    def test_never_validated_candidate_is_distinguished(self):
        engine = T._engine(T._graph())
        opp = T._opp([P1, P2], [WETH, A, WETH],
                     meta={"validation_status": None})
        engine.process_candidates([opp], 100)
        det = [r for r in self.records() if r.get("record_type") == "detection"][0]
        self.assertFalse(det["validation"]["performed"])
        self.assertIsNone(det["validation"]["status"])
        self.assertIsNone(det["validation"]["reason"])

    def test_execution_support_limitation_stays_separate(self):
        engine = T._engine(T._graph())
        opp = T._opp([P1, P2], [WETH, A, WETH], meta={
            "validation_status": None,
            "execution_supported": False,
            "execution_reason": "route contains an unsupported venue",
        })
        engine.process_candidates([opp], 100)
        det = [r for r in self.records() if r.get("record_type") == "detection"][0]
        self.assertFalse(det["eligibility"]["execution_supported"])
        self.assertEqual(det["eligibility"]["execution_reason"],
                         "route contains an unsupported venue")
        self.assertFalse(det["validation"]["performed"])  # support gap ≠ validation failure

    def test_reason_is_sanitized(self):
        dirty = ("https://user:secret@node.example.com/v2/  " + "0x" + "f1" * 40 + "  "
                 + "x" * 600)
        engine = T._engine(T._graph())
        opp = T._opp([P1, P2], [WETH, A, WETH], meta={
            "validation_status": "execution_rejected",
            "validation_reason": dirty,
        })
        engine.process_candidates([opp], 100)
        reason = self.records()[0]["validation"]["reason"]
        self.assertNotIn("secret", reason)
        self.assertNotIn("f1" * 20, reason)
        self.assertLessEqual(len(reason), 410)
        self.assertEqual(reason, reason.strip())


# ══════════════════════════════════════════════════════════════════════════════
# WS-C — real quote-size evidence
# ══════════════════════════════════════════════════════════════════════════════

class TestQuoteSizeEvidence(RecorderBase):
    def _validator(self, ad_amount_in, profit, fail_estimate=False):
        import validation.opportunity as vo

        captured = {}

        class FakeTxBuilder:
            def build_transaction(self, kwargs):
                return {"from": kwargs.get("from"), "chainId": kwargs.get("chainId")}

        class FakeFunctions:
            def yieldOut(self, raw):
                captured["raw"] = raw
                holder = self

                class Pending:
                    def call(self, *, block_identifier=None):
                        captured["block"] = block_identifier
                        return ((ad_amount_in, 0, (), (), (),
                                 "0x" + "11" * 20, 0), profit)

                return Pending()

            def swap(self, ad, force_balancer, tip, valid_until):
                class Pending:
                    def build_transaction(self, kwargs):
                        return {"from": kwargs.get("from")}

                return Pending()

        class FakeContract:
            functions = FakeFunctions()
            address = "0x" + "99" * 20

        class FakeEth:
            block_number = 100

            def get_block(self, which):
                return {"number": 100, "hash": "0x" + "bb" * 32}

            def estimate_gas(self, tx, block_identifier=None):
                if fail_estimate:
                    raise RuntimeError("estimation reverted")
                return 123_456

            def call(self, tx, block_identifier=None):
                return b""

        class FakeW3:
            eth = FakeEth()

            def to_checksum_address(self, v):
                return v

        validator = OpportunityValidator.__new__(OpportunityValidator)
        validator.w3 = FakeW3()
        validator.chain_id = 1
        validator.address = FakeContract.address
        validator.contract = FakeContract()
        return validator, captured

    def test_validator_preserves_contract_selected_amount(self):
        import unittest.mock as mock
        validator, captured = self._validator(ad_amount_in=2_500_000, profit=750_000)
        opp = T._opp([P1, P2], [WETH, A, WETH], meta={
            "execution_supported": True, "fees_bps": [30, 30]})
        with mock.patch("validation.opportunity.recorded_agent_address",
                        return_value="0x" + "22" * 20):
            result = validator.validate(opp, block_number=100)
        self.assertTrue(result.ok)
        self.assertEqual(result.amount_in_raw, 2_500_000)
        self.assertEqual(result.amount_in_token, WETH)
        self.assertEqual(result.quote_method, "exec_yieldOut")
        self.assertEqual(captured["block"], 100)  # quote pinned to the candidate block
        # write the evidence into metadata exactly as validate_many does
        meta = dict(opp.metadata)
        meta["validation_status"] = result.status
        meta["validation_reason"] = result.reason
        meta["validation_block"] = 100
        meta["validation_block_hash"] = "0x" + "bb" * 32
        meta["execution_simulated"] = result.ok
        meta["quote_amount_in_raw"] = result.amount_in_raw
        meta["quote_amount_in_denomination"] = result.amount_in_token or None
        meta["quote_amount_in_mode"] = result.amount_in_mode
        meta["quote_method"] = result.quote_method or None
        opp.metadata = meta
        # record through the engine pipeline WITHOUT re-running the engine's
        # own offline validation pass (which would overwrite the metadata)
        engine = T._engine(T._graph())
        with mock.patch.object(engine, "_apply_validation",
                               side_effect=lambda c, b: c), \
             mock.patch.object(engine, "_apply_adversarial",
                               side_effect=lambda c, b: c):
            engine.process_candidates([opp], 100)
        det = [r for r in self.records() if r.get("record_type") == "detection"][0]
        qi = det["market_inputs"]["quote_inputs"]
        self.assertEqual(qi["quote_amount_in_raw"], 2_500_000)
        self.assertEqual(qi["quote_amount_in_denomination"], WETH)
        self.assertEqual(det["market_inputs"]["quote_method"], "exec_yieldOut")
        self.assertEqual(det["size_and_profit"]["trade_size_amount_in"], 2_500_000)
        self.assertEqual(det["size_and_profit"]["size_selection_method"],
                         "contract_optimal_borrow")
        self.assertTrue(det["validation"]["performed"])
        self.assertEqual(det["validation"]["status"], "execution_simulated")

    def test_mode1_amount_is_labelled_profit_target(self):
        engine = T._engine(T._graph())
        opp = T._opp([P1, P2], [WETH, A, WETH], meta={
            "validation_status": "execution_simulated",
            "quote_amount_in_raw": 750_000,
            "quote_amount_in_denomination": WETH,
            "quote_amount_in_mode": 1,
            "quote_method": "exec_yieldOut",
        })
        engine.process_candidates([opp], 100)
        det = [r for r in self.records() if r.get("record_type") == "detection"][0]
        self.assertEqual(det["size_and_profit"]["trade_size_amount_in"], 750_000)
        self.assertEqual(det["size_and_profit"]["size_selection_method"],
                         "contract_profit_target")

    def test_missing_quote_evidence_stays_explicitly_unavailable(self):
        engine = T._engine(T._graph())
        opp = T._opp([P1, P2], [WETH, A, WETH], meta={
            "validation_status": None,
            "optimal_amount_in": None,
        })
        engine.process_candidates([opp], 100)
        det = [r for r in self.records() if r.get("record_type") == "detection"][0]
        qi = det["market_inputs"]["quote_inputs"]
        self.assertIsNone(qi["quote_amount_in_raw"])
        self.assertIsNone(qi["quote_amount_in_denomination"])
        self.assertIsNone(det["market_inputs"]["quote_method"])
        self.assertIsNone(det["size_and_profit"]["trade_size_amount_in"])
        self.assertIsNone(det["size_and_profit"]["size_selection_method"])


# ══════════════════════════════════════════════════════════════════════════════
# WS-D — candidate lifecycle observability
# ══════════════════════════════════════════════════════════════════════════════

class TestLifecycleScheduleGate(unittest.TestCase):
    def setUp(self):
        self.tracker = OpportunityTracker(T._NoopOppLogger())

    def _drive(self, opp, start=100):
        self.tracker.process_block(start, [opp], detector_scheduled={"cycles"})
        # absent for 4 blocks with the detector NOT scheduled → must stay open
        for b in range(start + 1, start + 5):
            self.tracker.process_block(b, [], detector_scheduled=set())

    def test_absence_without_scheduling_does_not_close(self):
        opp = T._opp([P1, P2], [WETH, A, WETH])
        self._drive(opp)
        self.assertEqual(self.tracker.active_count, 1)

    def test_scheduled_absence_closes_honestly(self):
        opp = T._opp([P1, P2], [WETH, A, WETH])
        self._drive(opp)
        self.tracker.process_block(106, [], detector_scheduled={"cycles"})
        self.assertEqual(self.tracker.active_count, 0)

    def test_legacy_callers_keep_unconditional_ttl(self):
        opp = T._opp([P1, P2], [WETH, A, WETH])
        self.tracker.process_block(100, [opp])
        for b in range(101, 107):
            self.tracker.process_block(b, [])
        self.assertEqual(self.tracker.active_count, 0)

    def test_mempool_origin_expires_by_ttl_without_scheduling(self):
        opp = T._opp([P1, P2], [WETH, A, WETH], is_mempool=True)
        self.tracker.process_block(100, [opp], detector_scheduled=set())
        for b in range(101, 107):
            self.tracker.process_block(b, [], detector_scheduled=set())
        self.assertEqual(self.tracker.active_count, 0)

    def test_hard_ttl_closes_eventually_without_fabrication(self):
        opp = T._opp([P1, P2], [WETH, A, WETH])
        self.tracker.process_block(100, [opp], detector_scheduled={"cycles"})
        for b in range(101, 167):
            self.tracker.process_block(b, [], detector_scheduled=set())
        self.assertEqual(self.tracker.active_count, 0)

    def test_competitor_overlap_closes_regardless_of_schedule(self):
        opp = T._opp([P1, P2], [WETH, A, WETH])
        self.tracker.process_block(100, [opp], detector_scheduled={"cycles"})
        self.tracker.process_block(101, [], detector_scheduled=set(),
                                   filled_pools={P1.lower(): "0x" + "ff" * 32})
        self.assertEqual(self.tracker.active_count, 0)

    def test_shadow_close_record_exists_for_non_ready_candidate(self):
        rec_path = Path(tempfile.mkdtemp(prefix="mi105_lc_")) / "shadow_validation.jsonl"
        recorder = ShadowRecorder(rec_path, tail_scan_lines=10_000)
        shadow_rec.set_recorder(recorder)
        try:
            engine = T._engine(T._graph())
            # execution_supported=False → the candidate can never be execution-ready
            opp = T._opp([P1, P2], [WETH, A, WETH],
                         meta={"validation_status": None, "execution_supported": False})
            engine.process_candidates([opp], 100)          # shadow OPEN
            tracker = OpportunityTracker(T._NoopOppLogger())
            tracker.process_block(100, [opp], detector_scheduled={"cycles"})
            tracker.process_block(106, [], detector_scheduled={"cycles"})
            recs = recorder.read_records()
            events = [(r.get("record_type"), r.get("event")) for r in recs]
            self.assertIn(("lifecycle", "CLOSE"), events)  # non-ready candidates close too
        finally:
            shadow_rec.set_recorder(None)
            shadow_rec.reset_recorder()


# ══════════════════════════════════════════════════════════════════════════════
# Schema compatibility + replay immutability
# ══════════════════════════════════════════════════════════════════════════════

class TestSchemaCompatAndReplay(unittest.TestCase):
    V1_KEYS = {"record_type", "event", "schema_version", "seq", "session",
               "recorded_at_unix", "fingerprint", "lifecycle", "identity",
               "detection", "market_inputs", "size_and_profit",
               "cost_assumptions", "eligibility", "execution_config_at_decision",
               "reorg_evidence", "unavailable_fields"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mi105_schema_"))
        self.path = self.tmp / "shadow_validation.jsonl"
        self.recorder = ShadowRecorder(self.path, tail_scan_lines=10_000)
        shadow_rec.set_recorder(self.recorder)

    def tearDown(self):
        shadow_rec.set_recorder(None)
        shadow_rec.reset_recorder()

    def test_v2_is_additive_over_v1(self):
        engine = T._engine(T._graph())
        engine.process_candidates([T._opp([P1, P2], [WETH, A, WETH])], 100)
        det = [r for r in self.recorder.read_records()
               if r.get("record_type") == "detection"][0]
        self.assertEqual(det["schema_version"], 2)
        self.assertTrue(self.V1_KEYS.issubset(det.keys()))
        self.assertIn("validation", det.keys())

    def test_restart_reconstruction_and_replay_immutability(self):
        engine = T._engine(T._graph())
        engine.process_candidates([T._opp([P1, P2], [WETH, A, WETH])], 100)
        open_seq = self.recorder.read_records()[0]["seq"]
        restarted = ShadowRecorder(self.path, tail_scan_lines=10_000)
        shadow_rec.set_recorder(restarted)
        engine.process_candidates([T._opp([P1, P2], [WETH, A, WETH])], 101)
        last = restarted.read_records()[-1]
        self.assertEqual(last["event"], "UPDATE")
        self.assertEqual(last["lifecycle"]["open_seq"], open_seq)

        # v1-shaped record compatibility: replay accepts a schema_version 1 log
        v1_line = json.loads(self.path.read_text().splitlines()[0])
        v1_line["schema_version"] = 1
        v1_line.pop("validation", None)
        v1_path = self.tmp / "v1_collection.jsonl"
        v1_path.write_text(json.dumps(v1_line, default=str) + "\n")

        rspec = importlib.util.spec_from_file_location(
            "replay_mod", REPO / "scripts" / "shadow_replay.py")
        replay = importlib.util.module_from_spec(rspec)
        rspec.loader.exec_module(replay)
        sha_before = hashlib.sha256(v1_path.read_bytes()).hexdigest()
        (self.tmp / "cache").mkdir()
        replay.evaluate(v1_path, self.tmp / "eval.jsonl", str(self.tmp / "cache"),
                        chain_id=1, catchup_window=1000, rerun=False)
        sha_after = hashlib.sha256(v1_path.read_bytes()).hexdigest()
        self.assertEqual(sha_before, sha_after)

        # and the v2 log replays byte-identically as well
        sha_before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        replay.evaluate(self.path, self.tmp / "eval2.jsonl", str(self.tmp / "cache"),
                        chain_id=1, catchup_window=1000, rerun=False)
        self.assertEqual(sha_before,
                         hashlib.sha256(self.path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
