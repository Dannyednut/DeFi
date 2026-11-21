"""DSS-WO-MI-103 (+R1) — shadow-mode validation recorder + offline replay regressions.

Deterministic, fully OFFLINE tests (stub w3 / stub graph client / temporary log
paths; no test performs a live RPC request). Covers the original work order §8
and the R1 review corrections:

MI-103 §8 (preserved):
- block/mempool origin through the shared recording point; non-ready candidates
- repeated detection → UPDATE; shared fingerprint across detector theses
- terminal CLOSE on expired/overlap/executed paths; duplicate CLOSE idempotent
- reorg records preserve competing block-hash observations
- replay outcome classes, gaps, beyond-window flagging, source immutability
- overlap ≠ confirmed competitor capture; null-not-fabricated fields

R1 corrections:
- §1 replay never mutates detection evidence — even when the detection re-run
  ACTUALLY produces candidates; prior recorder state restored afterwards
- §2 recompute exactly the ordered route legs in their actual direction
  (production-shaped bidirectional fixtures, independent constant-product
  comparison, missing-direction → not evaluable, never the reverse edge)
- §3 chain isolation (same pool + block on two chains cannot contaminate)
- §4 coherent later-state cache fallback (common anchor, per-pool checks,
  per-pool lag reporting, graph-wide max never establishes freshness)
- §5 atomic sequence allocation + append ordering under concurrency
- §6 restart reconstruction: OPEN-after-CLOSE starts a fresh lifecycle; open
  lifecycles are never silently evicted (explicit capacity condition)
- §7 truly bounded backward-seeking tail read (partial/malformed/empty)
- §8 replay evaluates distinct lifecycles of one fingerprint separately
"""
import csv
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from logger import Opportunity, OppType, ArbKind
from graph import TokenGraph, PoolEdge
from tracker import OpportunityTracker, _fingerprint
from executor.executor import ExecutionResult
from opportunity_engine import OpportunityEngine
from shadow import recorder as shadow_rec
from shadow.recorder import ShadowRecorder

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
A = "0x" + "11" * 20
B = "0x" + "22" * 20
P1 = "0x" + "01" * 20   # WETH/A
P2 = "0x" + "02" * 20   # A/B
P3 = "0x" + "03" * 20   # B/WETH
P4 = "0x" + "04" * 20   # dedicated to candidate C (never observed later)
P5 = "0x" + "05" * 20
P6 = "0x" + "06" * 20   # dedicated to candidate B
P7 = "0x" + "07" * 20   # missing-direction candidate
PG1 = "0x" + "71" * 20  # two-lifecycle candidate route
PG2 = "0x" + "72" * 20
PA = "0x" + "a1" * 20   # shared-pool two-chain isolation fixture
PB = "0x" + "b2" * 20
TVL = 250_000.0
STATE_HASH = "0x" + "ab" * 32
OTHER_HASH = "0x" + "77" * 32


class StubEth:
    gas_price = 30 * 10**9

    def get_block(self, n):
        return {"hash": str(n).rjust(30, "0").encode()}


class StubW3:
    eth = StubEth()


class _NoopOppLogger:
    def log(self, opp):
        return None

    def log_execution(self, *args, **kwargs):
        return None


def _edge(pool, tin, tout, r_in, r_out, state_block=100, state_hash=STATE_HASH):
    return PoolEdge(pool, "UniV2", tin, tout, r_in, r_out, 30, 2,
                    tvl_usd=TVL, state_block=state_block,
                    state_block_hash=state_hash, state_source="block_information",
                    state_status="FRESH")


def _add_pool(graph, pool, tin, tout, r_in, r_out, state_block=100, state_hash=STATE_HASH):
    graph.add_or_update_pool(_edge(pool, tin, tout, r_in, r_out, state_block, state_hash),
                             _edge(pool, tout, tin, r_out, r_in, state_block, state_hash))


def _graph() -> TokenGraph:
    g = TokenGraph()
    _add_pool(g, P1, WETH, A, 10**18, 2 * 10**18)
    _add_pool(g, P2, A, B, 2 * 10**18, 2 * 10**18)
    _add_pool(g, P3, B, WETH, 2 * 10**18, int(1.9 * 10**18))
    return g


def _engine(g: TokenGraph) -> OpportunityEngine:
    return OpportunityEngine(w3=StubW3(), chain_id=1, detectors=[], graph=g)


def _opp(pools, tokens, *, block=100, arb_kind=ArbKind.CYCLE, detector="cycles",
         is_mempool=False, timestamp=None, meta=None):
    base_meta = {
        "detector": detector,
        "arb_kind": arb_kind.value,
        "validation_status": "quote_validated",
        "execution_supported": False,
        "is_mempool": is_mempool,
        "quote_profit_wei": 12345,
        "optimal_amount_in": 10**18,
    }
    base_meta.update(meta or {})
    return Opportunity(
        opp_type=OppType.MULTI_HOP, chain_id=1, block_number=block, arb_kind=arb_kind,
        timestamp=timestamp if timestamp is not None else (time.time() - 0.25 if is_mempool else time.time()),
        tokens=tokens, pools=pools, fees=["30"] * len(pools), dexes=["UniV2"] * len(pools),
        net_profit_usd=42.0, estimated_profit_usd=50.0,
        metadata=base_meta,
    )


def _indep_v2_out(amount_in, reserve_in, reserve_out, fee_bps=30):
    """INDEPENDENT constant-product recompute used to cross-check the replay's
    adapter math (reimplemented here on purpose — R1 §2 regression)."""
    a = amount_in * (10_000 - fee_bps)
    return (reserve_out * a) // (reserve_in * 10_000 + a)


# ── production-shaped synthetic shadow records ────────────────────────────────

class _ShadowLogBuilder:
    """Builds shadow records in the PRODUCTION shape the recorder emits: BOTH
    directed edges of every involved pool (the detection snapshot of a
    three-pool route carries six market-input edges) plus the ordered token
    path. Later snapshots may carry deliberately different forward/reverse
    reserve orientations."""

    def __init__(self):
        self.records = []
        self.seq = 0

    def det(self, fp, pools, token_path, block, state_block, fwd_reserves, *,
            hash=STATE_HASH, size=10**18, quote=1000, event="OPEN", chain_id=1,
            rev_reserves=None):
        self.seq += 1
        edges = []
        for i, pool in enumerate(pools):
            tin, tout = token_path[i].lower(), token_path[i + 1].lower()
            r_in, r_out = fwd_reserves[i]
            edges.append({"pool": pool.lower(), "token_in": tin, "token_out": tout,
                          "version": 2, "fee_bps": 30, "state_block": state_block,
                          "state_block_hash": hash,
                          "reserve_in": r_in, "reserve_out": r_out})
            rev = rev_reserves[i] if rev_reserves else (r_out, r_in)
            edges.append({"pool": pool.lower(), "token_in": tout, "token_out": tin,
                          "version": 2, "fee_bps": 30, "state_block": state_block,
                          "state_block_hash": hash,
                          "reserve_in": rev[0], "reserve_out": rev[1]})
        self.records.append({
            "record_type": "detection", "event": event, "schema_version": 1,
            "seq": self.seq,
            "session": {"session_id": "fixture", "process_start_unix": 1.0, "pid": 1},
            "recorded_at_unix": 1.0, "fingerprint": fp,
            "identity": {"chain_id": chain_id, "origin": "block",
                         "pool_addresses": [p.lower() for p in pools],
                         "ordered_token_path": [t.lower() for t in token_path]},
            "detection": {"block_number": block, "block_hash": hash,
                          "state_block": state_block},
            "market_inputs": {"edges": edges, "quote_method": None},
            "size_and_profit": {"trade_size_amount_in": size,
                                "gross_quote_profit_wei": quote},
        })
        return self.records[-1]

    def close(self, fp, *, chain_id=1, outcome="EXPIRED"):
        self.seq += 1
        self.records.append({
            "record_type": "lifecycle", "event": "CLOSE", "schema_version": 1,
            "seq": self.seq,
            "session": {"session_id": "fixture", "process_start_unix": 1.0, "pid": 1},
            "recorded_at_unix": 1.0, "fingerprint": fp,
            "terminal": {"outcome": outcome, "chain_id": chain_id,
                         "outcome_basis": "tracker_absence_or_ttl_heuristic"},
        })
        return self.records[-1]

    def write(self, path: Path):
        with open(path, "w", encoding="utf-8") as f:
            for rec in self.records:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def _main_replay_fixture() -> _ShadowLogBuilder:
    """Five candidates covering every replay outcome class on production-shaped
    records (both directions per pool, ordered token path)."""
    b = _ShadowLogBuilder()
    # A: profitable at the coherent later snapshot (block 150)
    b.det("a" * 16, [P1, P2, P3], [WETH, A, B, WETH], 100, 100,
          [(10**18, 2 * 10**18), (2 * 10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
    b.det("a" * 16, [P1, P2, P3], [WETH, A, B, WETH], 150, 150,
          [(2 * 10**18, 6 * 10**18), (4 * 10**18, 8 * 10**18), (4 * 10**18, int(3.8 * 10**18))],
          event="UPDATE")
    # B: no longer profitable at the later state (rate inverted; dedicated pool)
    b.det("b" * 16, [P6], [WETH, A], 100, 100, [(10**18, 2 * 10**18)])
    b.det("b" * 16, [P6], [WETH, A], 150, 150, [(10**18, 10**12)], event="UPDATE")
    # C: no later state anywhere (dedicated pools, never observed again)
    b.det("c" * 16, [P4, P5], [WETH, A, B], 100, 100,
          [(10**18, 2 * 10**18), (2 * 10**18, 2 * 10**18)])
    # E: later state exists but beyond the 1,000-block catch-up window
    b.det("e" * 16, [P2], [A, B], 100, 100, [(2 * 10**18, 2 * 10**18)])
    b.det("e" * 16, [P2], [A, B], 2000, 2000, [(2 * 10**18, 4 * 10**18)], event="UPDATE")
    # D: conflicting block-hash observations at ITS OWN block 130 (reorg
    # evidence affects candidates at that block, not other blocks)
    b.det("d" * 16, [P1, P2, P3], [WETH, A, B, WETH], 130, 130,
          [(10**18, 2 * 10**18), (2 * 10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))],
          hash=OTHER_HASH)
    b.det("d" * 16, [P1, P2, P3], [WETH, A, B, WETH], 130, 130,
          [(10**18, 2 * 10**18), (2 * 10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))],
          hash=STATE_HASH, event="UPDATE")
    return b


def _evals_by_fp_open_seq(path: Path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("record_type") == "shadow_evaluation":
                out[(r["fingerprint"], r["lifecycle"]["open_seq"])] = r
    return out


class _ReplayTestMixin:
    @classmethod
    def _load_script(cls, name):
        spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class RecorderTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mi103_")
        self.shadow_path = Path(self.tmp) / "shadow_validation.jsonl"
        self.recorder = ShadowRecorder(self.shadow_path, tail_scan_lines=10_000)
        shadow_rec.set_recorder(self.recorder)

    def tearDown(self):
        shadow_rec.reset_recorder()

    def records(self):
        return self.recorder.read_records()


class TestDetectionRecording(RecorderTestBase):
    """MI-103 §8: block origin, mempool origin, non-ready candidates, UPDATE
    vs OPEN, shared fingerprint, null fields."""

    def test_block_origin_candidate_recorded(self):
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        result = _engine(_graph()).process_candidates([opp], 100)
        self.assertEqual(len(result.candidates), 1)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["event"], "OPEN")
        self.assertEqual(rec["fingerprint"], _fingerprint(opp))
        self.assertEqual(rec["identity"]["origin"], "block")
        self.assertEqual(rec["identity"]["origin_evidence"]["is_mempool"], False)
        self.assertEqual(rec["identity"]["canonical_fingerprint"], rec["fingerprint"])
        self.assertEqual(rec["identity"]["detector_source"], "cycles")
        self.assertEqual(rec["identity"]["ordered_token_path"], [WETH, A, B, WETH])
        edges = rec["market_inputs"]["edges"]
        self.assertEqual(len(edges), 6, "both directions of all three pools from the LOCAL graph")
        self.assertTrue(all(e["reserve_in"] and e["state_block"] == 100 and e["state_block_hash"]
                            for e in edges))
        self.assertEqual(rec["reorg_evidence"]["per_edge_state_block_hashes"][P1], STATE_HASH)
        self.assertFalse(rec["execution_config_at_decision"]["EXECUTE_ONCHAIN"])

    def test_mempool_origin_candidate_recorded_through_shared_point(self):
        seen = time.time() - 0.25
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH], arb_kind=ArbKind.BACKRUN,
                   detector="mempool_backrun", is_mempool=True, timestamp=seen,
                   meta={"trigger_tx": "0x" + "cd" * 32, "discovery_source": "mempool_backrun"})
        _engine(_graph()).process_candidates([opp], 101)
        rec = [r for r in self.records() if r.get("record_type") == "detection"][0]
        self.assertEqual(rec["identity"]["origin"], "mempool")
        self.assertIs(rec["identity"]["origin_evidence"]["is_mempool"], True)
        self.assertEqual(rec["detection"]["trigger_tx_hash"], "0x" + "cd" * 32)
        latency = rec["detection"]["mempool_to_open_latency_seconds"]
        self.assertIsNotNone(latency)
        self.assertGreaterEqual(latency, 0.2)
        self.assertLess(latency, 5.0)

    def test_not_execution_ready_candidate_recorded(self):
        opp = _opp([P1, P2], [WETH, A, WETH], meta={"execution_ready": False,
                                                    "route_state_status": "STALE"})
        result = _engine(_graph()).process_candidates([opp], 100)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        self.assertEqual(len(result.ready), 0)
        self.assertEqual(len(recs), 1)
        self.assertFalse(recs[0]["eligibility"]["execution_ready"])
        self.assertEqual(recs[0]["eligibility"]["validation_status"], "quote_validated")
        self.assertEqual(recs[0]["market_inputs"]["route_state_status"], "STALE")

    def test_repeated_detection_produces_update_not_duplicate_open(self):
        engine = _engine(_graph())
        engine.process_candidates([_opp([P1, P2, P3], [WETH, A, B, WETH])], 100)
        engine.process_candidates([_opp([P1, P2, P3], [WETH, A, B, WETH],
                                        meta={"quote_profit_wei": 999})], 101)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        self.assertEqual([r["event"] for r in recs], ["OPEN", "UPDATE"])
        self.assertEqual(recs[1]["lifecycle"]["open_seq"], recs[0]["seq"])
        self.assertEqual(recs[1]["lifecycle"]["update_count"], 1)
        self.assertGreater(recs[1]["seq"], recs[0]["seq"])
        self.assertEqual(recs[1]["size_and_profit"]["gross_quote_profit_wei"], 999)

    def test_shared_fingerprint_across_detector_theses(self):
        engine = _engine(_graph())
        o1 = _opp([P1, P2, P3], [WETH, A, B, WETH], arb_kind=ArbKind.CYCLE, detector="cycles")
        o2 = _opp([P1, P2, P3], [WETH, A, B, WETH], arb_kind=ArbKind.CROSS_DEX_SPREAD,
                  detector="cross_proto", meta={"quote_profit_wei": 555})
        self.assertEqual(_fingerprint(o1), _fingerprint(o2))
        engine.process_candidates([o1], 100)
        engine.process_candidates([o2], 101)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        self.assertEqual([r["event"] for r in recs], ["OPEN", "UPDATE"])
        theses = {r["identity"]["strategy_thesis"]["arb_kind"] for r in recs}
        self.assertEqual(theses, {"cycle", "cross_dex_spread"})
        self.assertEqual(recs[1]["identity"]["detector_source"], "cross_proto")

    def test_missing_fields_stay_null_not_fabricated(self):
        opp = _opp([P1, P2], [WETH, A, WETH], meta={
            "quote_profit_wei": None, "optimal_amount_in": None})
        _engine(_graph()).process_candidates([opp], 100)
        rec = [r for r in self.records() if r.get("record_type") == "detection"][0]
        self.assertIsNone(rec["size_and_profit"]["gross_quote_profit_wei"])
        self.assertIsNone(rec["size_and_profit"]["validated_profit_floor_wei"])
        self.assertIsNone(rec["size_and_profit"]["trade_size_amount_in"])
        self.assertIsNone(rec["size_and_profit"]["simulation_profit_wei"])
        self.assertIsNone(rec["eligibility"]["router_decision"])
        self.assertIsNone(rec["cost_assumptions"]["builder_tip_tier"])
        # MI-105 WS-C: quote_method/size_selection_method are no longer
        # STRUCTURALLY unavailable — the validator persists the actual
        # Exec-contract quote method and selected amount when a quote ran.
        # They remain null HERE because no validation ran (null, not fabricated).
        self.assertIsNone(rec["market_inputs"]["quote_method"])
        self.assertIsNone(rec["size_and_profit"]["size_selection_method"])
        self.assertIsNone(rec["detection"]["mempool_to_open_latency_seconds"])
        for field in ("router_decision", "builder_tip_tier",
                      "inclusion_block", "bundle_position"):
            self.assertIn(field, rec["unavailable_fields"])
        self.assertNotIn("quote_method", rec["unavailable_fields"])
        self.assertNotIn("size_selection_method", rec["unavailable_fields"])
        # MI-105 WS-B: this fixture carries validation_status="quote_validated",
        # so the validation object must reflect an ACTUAL validation outcome
        # (performed=True); the never-validated distinction is covered in
        # tests/test_mi105_shadow_evidence.py.
        self.assertTrue(rec["validation"]["performed"])
        self.assertEqual(rec["validation"]["status"], "quote_validated")
        self.assertIsNone(rec["validation"]["reason"])
        self.assertNotIn("provider_url", json.dumps(rec).lower())
        self.assertNotIn("private_key", json.dumps(rec).lower())


class TestLifecycleRecording(RecorderTestBase):
    """MI-103 §8 + §6: terminal CLOSE paths, idempotency, competition class."""

    def _tracker(self):
        return OpportunityTracker(_NoopOppLogger())

    def test_terminal_close_on_expired_overlap_and_executed_paths(self):
        tracker = self._tracker()
        opp_exp = _opp([P1, P2], [WETH, A, WETH])
        opp_exec = _opp([P2, P3], [A, B, A])
        opp_fill = _opp([P3], [B, WETH, B])
        _engine(_graph()).process_candidates([opp_exp, opp_exec, opp_fill], 200)
        tracker.process_block(200, [opp_exp, opp_exec, opp_fill])
        tracker.close_executed(opp_exec, 201,
                               ExecutionResult(success=True, tx_hash="0x" + "ee" * 32,
                                               method="direct", lender="", amount_in=1,
                                               profit_wei=77, gas_cost_usd=0.2),
                               actual_profit_usd=31.5)
        tracker.process_block(202, [], filled_pools={P3.lower(): "0x" + "ff" * 32})
        tracker.process_block(206, [], filled_pools={})

        closes = [r for r in self.records() if r.get("record_type") == "lifecycle"]
        self.assertEqual(len(closes), 3)
        by_outcome = {c["terminal"]["outcome"]: c for c in closes}
        self.assertEqual(set(by_outcome), {"EXECUTED", "FILLED_BY_COMPETITOR", "EXPIRED"})
        executed = by_outcome["EXECUTED"]
        self.assertEqual(executed["execution_outcome"]["actual_profit_usd"], 31.5)
        self.assertEqual(executed["execution_outcome"]["tx_hash"], "0x" + "ee" * 32)
        self.assertIsNone(executed["execution_outcome"]["inclusion_block"])
        filled = by_outcome["FILLED_BY_COMPETITOR"]
        self.assertEqual(filled["competition_evidence"]["affected_pool"], P3.lower())
        self.assertEqual(filled["competition_evidence"]["observed_tx_hash"], "0x" + "ff" * 32)
        self.assertEqual(filled["terminal"]["outcome_basis"], "tracker_pool_overlap_heuristic")
        expired = by_outcome["EXPIRED"]
        self.assertEqual(expired["terminal"]["outcome_basis"], "tracker_absence_or_ttl_heuristic")
        for c in closes:
            self.assertIsNotNone(c["lifecycle"]["open_seq"])

    def test_duplicate_close_is_idempotent(self):
        tracker = self._tracker()
        opp = _opp([P1, P2], [WETH, A, WETH])
        _engine(_graph()).process_candidates([opp], 200)
        tracker.process_block(200, [opp])
        tracker.process_block(206, [], filled_pools={})
        closes_before = len([r for r in self.records() if r.get("record_type") == "lifecycle"])
        tracker.close_executed(opp, 207,
                               ExecutionResult(success=True, tx_hash="0x" + "11" * 32,
                                               method="direct", lender="", amount_in=1,
                                               profit_wei=1, gas_cost_usd=0.0), 1.0)
        dup = types.SimpleNamespace(
            fingerprint=_fingerprint(opp), status="CLOSED", opp_type="MULTI_HOP",
            arb_kind="cycle", chain_id=1, open_block=200, open_time=1.0,
            close_block=206, close_time=2.0, last_seen_block=200,
            duration_blocks=1, duration_seconds=1.0, peak_profit_usd=1.0,
            open_profit_usd=1.0, close_profit_usd=1.0, update_count=0,
            tokens=[WETH, A, WETH], pools=[P1, P2], dexes=["UniV2"],
            metadata={"outcome": "EXPIRED"})
        self.recorder.record_terminal(dup)
        closes_after = [r for r in self.records() if r.get("record_type") == "lifecycle"]
        self.assertEqual(len(closes_after), closes_before)
        self.assertGreaterEqual(self.recorder.stats["duplicate_closes_suppressed"], 1)


class TestRestartReconstruction(RecorderTestBase):
    """R1 §6/§7: reopen-after-CLOSE, open-state retention, bounded tail read."""

    def test_restart_reconstruction_continues_open_lifecycle(self):
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        _engine(_graph()).process_candidates([opp], 100)
        open_seq = self.records()[0]["seq"]
        session_a = self.records()[0]["session"]["session_id"]

        restarted = ShadowRecorder(self.shadow_path, tail_scan_lines=10_000)
        self.assertGreater(restarted.stats["restart_scanned_records"], 0)
        self.assertGreater(restarted.stats["restart_scan_bytes_read"], 0)
        shadow_rec.set_recorder(restarted)
        _engine(_graph()).process_candidates([_opp([P1, P2, P3], [WETH, A, B, WETH])], 105)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["OPEN", "UPDATE"])
        self.assertEqual(recs[1]["lifecycle"]["open_seq"], open_seq)
        self.assertEqual(recs[1]["lifecycle"]["continued_from_previous_session"], True)
        self.assertNotEqual(recs[1]["session"]["session_id"], session_a)

    def test_reopen_after_close_in_tail_starts_fresh_lifecycle(self):
        """R1 §6A: OPEN → CLOSE → OPEN → UPDATE in the scanned tail — the new
        process must use the SECOND OPEN's sequence for the fresh lifecycle,
        not retain the closed one's open_seq."""
        opp1 = _opp([P1, P2], [WETH, A, WETH])
        _engine(_graph()).process_candidates([opp1], 200)          # OPEN #1
        first = self.records()[0]
        tracker = OpportunityTracker(_NoopOppLogger())
        tracker._active = {}  # deterministic: close through the central path directly
        close_evt = types.SimpleNamespace(
            fingerprint=_fingerprint(opp1), status="CLOSED", opp_type="MULTI_HOP",
            arb_kind="cycle", chain_id=1, open_block=200, open_time=1.0,
            close_block=205, close_time=2.0, last_seen_block=200,
            duration_blocks=5, duration_seconds=1.0, peak_profit_usd=1.0,
            open_profit_usd=1.0, close_profit_usd=1.0, update_count=0,
            tokens=[WETH, A, WETH], pools=[P1, P2], dexes=["UniV2"],
            metadata={"outcome": "EXPIRED"})
        self.recorder.record_terminal(close_evt)                    # CLOSE
        opp2 = _opp([P1, P2], [WETH, A, WETH])
        _engine(_graph()).process_candidates([opp2], 210)          # OPEN #2 (fresh)
        _engine(_graph()).process_candidates([_opp([P1, P2], [WETH, A, WETH])], 211)  # UPDATE
        opens = [r for r in self.records() if r.get("record_type") == "detection"
                 and r["event"] == "OPEN"]
        self.assertEqual(len(opens), 2)
        second_open_seq = opens[1]["seq"]

        restarted = ShadowRecorder(self.shadow_path, tail_scan_lines=10_000)
        state = restarted._lifecycles.get(_fingerprint(opp1))
        self.assertIsNotNone(state)
        self.assertTrue(state.open)
        self.assertEqual(state.open_seq, second_open_seq,
                         "the fresh lifecycle uses the SECOND OPEN's sequence")
        self.assertEqual(state.updates, 1, "the tail UPDATE belongs to the fresh lifecycle")
        self.assertEqual(state.matched, "open")

        # next CLOSE closes ONLY the fresh lifecycle
        shadow_rec.set_recorder(restarted)
        self.recorder.record_terminal(close_evt)
        closes = [r for r in self.records() if r.get("record_type") == "lifecycle"
                  and r["event"] == "CLOSE"]
        self.assertEqual(len(closes), 2)
        self.assertEqual(closes[-1]["lifecycle"]["open_seq"], second_open_seq,
                         "the new CLOSE is associated with the fresh lifecycle only")

    def test_open_lifecycles_never_evicted(self):
        """R1 §6B: with a tiny capacity, open lifecycles are never evicted — a
        re-detection of the earliest fingerprint stays an UPDATE (no duplicate
        OPEN) and the capacity condition is visible in the stats."""
        small = ShadowRecorder(Path(self.tmp) / "small.jsonl", tail_scan_lines=100,
                               state_capacity=4)
        shadow_rec.set_recorder(small)
        engine = _engine(_graph())
        routes = [
            ([P1, P2], [WETH, A, WETH]),
            ([P2, P3], [A, B, A]),
            ([P1, P3], [WETH, B, WETH]),
            ([P1, P2, P3], [WETH, A, B, WETH]),
            ([P3], [B, WETH, B]),
            ([P2], [A, B, A]),
        ]
        opps = [_opp(pools, tokens) for pools, tokens in routes]
        for opp in opps:
            engine.process_candidates([opp], 100)
        opens = [r for r in small.read_records()
                 if r.get("record_type") == "detection" and r["event"] == "OPEN"]
        self.assertEqual(len(opens), 6, "every fingerprint opened exactly once")
        # re-detect the EARLIEST fingerprint (would be evicted by naive LRU)
        engine.process_candidates([_opp(routes[0][0], routes[0][1])], 105)
        recs = [r for r in small.read_records()
                if r.get("record_type") == "detection"]
        self.assertEqual([r["event"] for r in recs][-1], "UPDATE",
                         "no duplicate OPEN for the retained lifecycle")
        opens_after = [r for r in recs if r["event"] == "OPEN"]
        self.assertEqual(len(opens_after), 6, "still exactly six OPENs — no duplicate")
        self.assertGreaterEqual(small.stats["open_state_capacity_exceeded"], 1,
                                "capacity overflow is exposed, never silent")

    def test_tail_scan_is_bounded_and_correct(self):
        """R1 §7: a large synthetic file is tail-scanned by seeking backward —
        the reconstructed open lifecycle and maximum sequence match the true
        tail, a malformed line and a final line without a trailing newline are
        handled, and the scan does NOT read the whole file."""
        big = Path(self.tmp) / "big.jsonl"
        lines = []
        seq = 0
        for i in range(5000):  # ~1MB of padding records
            seq += 1
            lines.append(json.dumps({
                "record_type": "detection", "event": "OPEN", "schema_version": 1,
                "seq": seq, "fingerprint": f"pad{i:05d}", "session": {},
            }, separators=(",", ":")))
        seq += 1
        tail_open_seq = seq
        tail_open = json.dumps({
            "record_type": "detection", "event": "OPEN", "schema_version": 1,
            "seq": tail_open_seq, "fingerprint": "tailfp", "session": {},
        }, separators=(",", ":"))
        seq += 1
        tail_update_seq = seq
        tail_update = json.dumps({
            "record_type": "detection", "event": "UPDATE", "schema_version": 1,
            "seq": tail_update_seq, "fingerprint": "tailfp", "session": {},
        }, separators=(",", ":"))
        seq += 1
        malformed = '{"record_type": "detection", "seq": '  # broken JSON
        seq += 1
        last_seq = seq
        last = json.dumps({
            "record_type": "detection", "event": "OPEN", "schema_version": 1,
            "seq": last_seq, "fingerprint": "lastfp", "session": {},
        }, separators=(",", ":"))
        with open(big, "w", encoding="utf-8") as f:
            f.write("\n".join(lines + [tail_open, tail_update, malformed, last]))
            f.write("\n")  # trailing newline; stripped below so the FINAL line has none
        # strip the trailing newline to exercise a FINAL line without one
        raw = big.read_bytes()
        big.write_bytes(raw[:-1])

        size = big.stat().st_size
        rec = ShadowRecorder(big, tail_scan_lines=100)
        self.assertGreater(rec.stats["restart_scan_bytes_read"], 0)
        self.assertLess(rec.stats["restart_scan_bytes_read"], size / 2,
                        "the tail scan must not read the whole file")
        self.assertEqual(rec._seq - 1, last_seq, "maximum sequence reconstructed from the tail")
        tail_state = rec._lifecycles.get("tailfp")
        self.assertIsNotNone(tail_state)
        self.assertTrue(tail_state.open)
        self.assertEqual(tail_state.open_seq, tail_open_seq,
                         "the tail lifecycle continues from the true tail OPEN")
        self.assertEqual(tail_state.updates, 1, "the tail UPDATE belongs to that lifecycle")
        self.assertIsNotNone(rec._lifecycles.get("lastfp"))
        self.assertEqual(rec.stats["restart_scanned_records"], 99,
                         "the requested 100-line tail minus the malformed line was scanned")

        # empty file: no state, no crash
        empty = Path(self.tmp) / "empty.jsonl"
        empty.write_text("")
        rec_empty = ShadowRecorder(empty, tail_scan_lines=100)
        self.assertEqual(rec_empty.stats["restart_scanned_records"], 0)
        self.assertEqual(rec_empty._seq, 1)

    def test_small_file_tail_limit(self):
        """R2 §3A: the configured line limit applies to the small-file path as
        well as the backward-seeking path; ``tail_scan_lines=0`` reconstructs
        nothing; OPENs outside the requested tail do not reappear in recorder
        state; zero, one and fewer-than-limit counts are exercised."""
        small = Path(self.tmp) / "limit.jsonl"
        lines = [json.dumps({
            "record_type": "detection", "event": "OPEN", "schema_version": 1,
            "seq": i + 1, "fingerprint": f"pad{i:03d}", "session": {},
        }, separators=(",", ":")) for i in range(30)]
        small.write_text("\n".join(lines))  # final line without trailing newline

        rec = ShadowRecorder(small, tail_scan_lines=10)
        self.assertEqual(rec.stats["restart_scanned_records"], 10,
                         "only the requested tail is reconstructed on a small file")
        self.assertEqual(rec._seq - 1, 30, "the maximum sequence is still correct")
        self.assertIsNone(rec._lifecycles.get("pad000"),
                          "an OPEN outside the tail leaves no recorder state")
        self.assertIsNone(rec._lifecycles.get("pad019"))
        self.assertIsNotNone(rec._lifecycles.get("pad020"))
        self.assertIsNotNone(rec._lifecycles.get("pad029"))

        rec0 = ShadowRecorder(small, tail_scan_lines=0)
        self.assertEqual(rec0.stats["restart_scanned_records"], 0)
        self.assertEqual(rec0._seq, 1, "zero limit reconstructs nothing")
        self.assertEqual(len(rec0._lifecycles), 0)

        rec1 = ShadowRecorder(small, tail_scan_lines=1)
        self.assertEqual(rec1.stats["restart_scanned_records"], 1)
        self.assertIsNotNone(rec1._lifecycles.get("pad029"))
        self.assertIsNone(rec1._lifecycles.get("pad028"))

        rec_all = ShadowRecorder(small, tail_scan_lines=100)
        self.assertEqual(rec_all.stats["restart_scanned_records"], 30,
                         "fewer records than the limit → all are reconstructed")

    def test_initial_orphan_update_counted(self):
        """R2 §3B: a tail that begins with UPDATE records and no OPEN for the
        fingerprint reconstructs an open lifecycle with ``open_seq=None``,
        counts ALL orphan UPDATEs, and a subsequent live detection continues
        that lifecycle as an UPDATE (never a new OPEN)."""
        from tracker import _fingerprint as _fp
        orphan_fp = _fp(_opp([P1, P2], [WETH, A, WETH]))
        orphan_path = Path(self.tmp) / "orphan.jsonl"
        lines = [json.dumps({
            "record_type": "detection", "event": "UPDATE", "schema_version": 1,
            "seq": i + 1, "fingerprint": orphan_fp, "session": {},
        }, separators=(",", ":")) for i in range(2)]
        # the recorder itself always ends every append with a newline, so a
        # real log ends with one; the fixture must match before a live append
        orphan_path.write_text("\n".join(lines) + "\n")

        rec = ShadowRecorder(orphan_path, tail_scan_lines=100)
        st = rec._lifecycles.get(orphan_fp)
        self.assertIsNotNone(st)
        self.assertTrue(st.open)
        self.assertIsNone(st.open_seq,
                          "honest outside-recorded-history: no fabricated OPEN")
        self.assertEqual(st.updates, 2,
                         "the initial orphan UPDATE is counted as the first update")
        self.assertTrue(st.continued)

        # a live detection continues the reconstructed lifecycle as an UPDATE
        shadow_rec.set_recorder(rec)
        _engine(_graph()).process_candidates([_opp([P1, P2], [WETH, A, WETH])], 110)
        live = [r for r in rec.read_records() if r.get("record_type") == "detection"]
        self.assertEqual([r["event"] for r in live], ["UPDATE", "UPDATE", "UPDATE"],
                         "two orphan UPDATEs + one live UPDATE — no duplicate OPEN")
        self.assertEqual(live[-1]["lifecycle"]["update_count"], 3,
                         "the live UPDATE counts on top of the reconstructed ones")
        self.assertIsNone(live[-1]["lifecycle"]["open_seq"])
        self.assertEqual(live[-1]["lifecycle"]["continued_from_previous_session"], True)


class TestConcurrency(RecorderTestBase):
    """R1 §5: sequence allocation, lifecycle transition, record construction
    and append ordering share one critical section."""

    def test_concurrent_distinct_fingerprints_keep_sequence_order(self):
        engine = _engine(_graph())
        routes = [([P1, P2], [WETH, A, WETH]), ([P2, P3], [A, B, A]),
                  ([P1, P3], [WETH, B, WETH])]
        opps = []
        for i in range(24):
            pools, tokens = routes[i % 3]
            # distinct fingerprints: vary the pool SET via dedicated dedup —
            # instead vary nothing; identical routes share a lifecycle, so use
            # per-thread route rotation plus distinct pool orderings
            pools = pools if i % 2 == 0 else list(reversed(pools))
            tokens = tokens if i % 2 == 0 else list(reversed(tokens))
            opps.append(_opp(pools, tokens))
        threads = [threading.Thread(target=engine.process_candidates,
                                    args=([o], 100 + i))
                   for i, o in enumerate(opps)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = [r["seq"] for r in self.records()]
        self.assertEqual(seqs, sorted(seqs),
                         "physical file order matches the allocated sequence order")

    def test_concurrent_repeated_detection_single_open(self):
        engine = _engine(_graph())
        opp0 = _opp([P1, P2, P3], [WETH, A, B, WETH])
        threads = [threading.Thread(target=engine.process_candidates,
                                    args=([_opp([P1, P2, P3], [WETH, A, B, WETH])], 100 + i))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        opens = [r for r in recs if r["event"] == "OPEN"]
        self.assertEqual(len(opens), 1, "exactly one OPEN under contention")
        self.assertEqual(len(recs), 8)
        self.assertEqual([r["seq"] for r in recs], sorted(r["seq"] for r in recs))
        for r in recs[1:]:
            self.assertEqual(r["lifecycle"]["open_seq"], opens[0]["seq"])

    def test_concurrent_detection_and_terminal_contention(self):
        recorder = self.recorder
        opps = [_opp([P1, P2], [WETH, A, WETH]), _opp([P2, P3], [A, B, A]),
                _opp([P1, P3], [WETH, B, WETH]), _opp([P1, P2, P3], [WETH, A, B, WETH])]
        engine = _engine(_graph())
        for opp in opps:
            engine.process_candidates([opp], 100)
        opens_before = [r for r in self.records() if r["event"] == "OPEN"]
        close_evts = [types.SimpleNamespace(
            fingerprint=_fingerprint(o), status="CLOSED", opp_type="MULTI_HOP",
            arb_kind="cycle", chain_id=1, open_block=100, open_time=1.0,
            close_block=106, close_time=2.0, last_seen_block=100,
            duration_blocks=6, duration_seconds=1.0, peak_profit_usd=1.0,
            open_profit_usd=1.0, close_profit_usd=1.0, update_count=0,
            tokens=o.tokens, pools=o.pools, dexes=o.dexes,
            metadata={"outcome": "EXPIRED"}) for o in opps]
        more = [_opp([P1, P2], [WETH, A, WETH]), _opp([P2, P3], [A, B, A])]
        threads = ([threading.Thread(target=recorder.record_terminal, args=(e,))
                    for e in close_evts]
                   + [threading.Thread(target=engine.process_candidates, args=([o], 107))
                      for o in more])
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recs = self.records()
        seqs = [r["seq"] for r in recs]
        self.assertEqual(seqs, sorted(seqs))
        for o in opps[:3]:
            fp = _fingerprint(o)
            idx_open = min(i for i, r in enumerate(recs)
                           if r.get("record_type") == "detection"
                           and r["event"] == "OPEN" and r["fingerprint"] == fp)
            idx_close = [i for i, r in enumerate(recs)
                         if r.get("record_type") == "lifecycle" and r["fingerprint"] == fp]
            self.assertTrue(idx_close and idx_close[0] > idx_open,
                            "a CLOSE never physically precedes the OPEN it closes")


class TestOfflineReplay(RecorderTestBase, _ReplayTestMixin):
    """R1 §1/§2/§3/§4/§8 replay regressions on production-shaped records."""

    @classmethod
    def setUpClass(cls):
        cls.replay = cls._load_script("shadow_replay")
        cls.agg = cls._load_script("shadow_aggregate")

    def _cache_cycle_fixture(self, pool_states: dict):
        """PoolCache fixture with PER-POOL state blocks (the warmed graph
        carries each pool's own state_block after warm_graph)."""
        from cache import PoolCache
        cache_dir = Path(self.tmp) / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        derived = {WETH: 1.0, A: 0.5, B: 0.25}

        def rec(t0, t1, r0, r1, sb):
            return {"token0": t0.lower(), "token1": t1.lower(), "dex": "UniswapV2",
                    "protocol": "uniswap_v2", "version": 2, "fee_bps": 30,
                    "r0": r0, "r1": r1, "last_updated": 1.0, "state_block": sb,
                    "state_block_hash": STATE_HASH, "state_source": "block_information",
                    "state_status": "FRESH", "tvl_usd": TVL,
                    "token0_meta": {"symbol": "T0", "decimals": 18,
                                    "derivedETH": derived.get(t0.lower(), 0.0)},
                    "token1_meta": {"symbol": "T1", "decimals": 18,
                                    "derivedETH": derived.get(t1.lower(), 0.0)}}

        cache = PoolCache(chain_id=1, cache_dir=str(cache_dir))
        for pool, (t0, t1, r0, r1, sb) in pool_states.items():
            cache.set_pool(pool, rec(t0, t1, r0, r1, sb))
        cache.save()
        return cache_dir

    def test_replay_outcomes_direction_and_source_immutability(self):
        """Production-shaped bidirectional records: the replay recomputes
        EXACTLY the ordered route legs in their actual direction (verified
        against an independently calculated constant-product result), the
        four outcome classes hold, the >1,000-block stretch is a declared
        gap, and the source log is byte-identical afterwards."""
        _main_replay_fixture().write(self.shadow_path)
        source_hash_before = hashlib.sha256(self.shadow_path.read_bytes()).hexdigest()
        evaluation_path = Path(self.tmp) / "shadow_evaluation.jsonl"
        cache_dir = Path(self.tmp) / "cache"
        cache_dir.mkdir()

        header = self.replay.evaluate(self.shadow_path, evaluation_path, str(cache_dir),
                                      chain_id=1, catchup_window=1000, rerun=False)
        by_fp = {fp: r for (fp, _seq), r in
                 _evals_by_fp_open_seq(evaluation_path).items()}

        # A: exact direction + exact independent math
        a = by_fp["a" * 16]
        self.assertEqual(a["outcome"], self.replay.OUTCOME_STILL)
        self.assertEqual(a["route_leg_count"], 3)
        legs = a["recompute_legs"]
        self.assertEqual([(l["pool"], l["token_in"], l["token_out"]) for l in legs],
                         [(P1.lower(), WETH, A), (P2.lower(), A, B), (P3.lower(), B, WETH)],
                         "exactly one leg per ordered hop, exact direction")
        self.assertTrue(all(l["later_state_block"] == 150 for l in legs))
        out = _indep_v2_out(10**18, 2 * 10**18, 6 * 10**18)
        out = _indep_v2_out(out, 4 * 10**18, 8 * 10**18)
        out = _indep_v2_out(out, 4 * 10**18, int(3.8 * 10**18))
        self.assertEqual(a["recomputed_gross_profit_wei"], out - 10**18,
                         "recomputed profit equals the INDEPENDENT forward-direction result")
        self.assertEqual(a["later_state_blocks"][f"{P1.lower()}:{WETH}->{A}"], 150)
        self.assertEqual(a["common_anchor_block"], 150)

        # B: the later snapshot carries both directions; only the FORWARD
        # (inverted) one may be used → no longer profitable
        self.assertEqual(by_fp["b" * 16]["outcome"], self.replay.OUTCOME_GONE)
        # C: no coherent later state anywhere
        self.assertEqual(by_fp["c" * 16]["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn("no coherent common anchor", by_fp["c" * 16]["reason"])
        # E: beyond-window state flagged, never skipped
        e = by_fp["e" * 16]
        self.assertEqual(e["outcome"], self.replay.OUTCOME_STILL)
        self.assertTrue(e["beyond_catchup_window"])
        # D: reorg evidence at its own block
        self.assertEqual(by_fp["d" * 16]["outcome"], self.replay.OUTCOME_REORG)

        self.assertGreater(header["gap_count"], 0)
        with open(evaluation_path, encoding="utf-8") as f:
            recs = [json.loads(l) for l in f if l.strip()]
        self.assertTrue(any(r.get("kind") == "state_gap" and r.get("gap_blocks", 0) > 1000
                            for r in recs))
        self.assertEqual(hashlib.sha256(self.shadow_path.read_bytes()).hexdigest(),
                         source_hash_before)
        self.assertNotEqual(evaluation_path, self.shadow_path)

    def test_missing_direction_returns_not_evaluable(self):
        """R1 §2.5: when the exact directed edge is unavailable at the later
        state, the candidate is not evaluable — the reverse edge (present and
        profitable!) is never substituted."""
        b = _ShadowLogBuilder()
        b.det("f" * 16, [P7], [WETH, A], 100, 100, [(10**18, 2 * 10**18)])
        # later snapshot: ONLY the reverse direction (A→WETH, profitable
        # orientation); the forward WETH→A edge is absent
        b.seq += 1
        later = {
            "record_type": "detection", "event": "UPDATE", "schema_version": 1,
            "seq": b.seq, "session": {"session_id": "fixture"}, "recorded_at_unix": 1.0,
            "fingerprint": "f" * 16,
            "identity": {"chain_id": 1, "origin": "block", "pool_addresses": [P7],
                         "ordered_token_path": [WETH, A]},
            "detection": {"block_number": 150, "block_hash": STATE_HASH, "state_block": 150},
            "market_inputs": {"edges": [{
                "pool": P7, "token_in": A, "token_out": WETH, "version": 2,
                "fee_bps": 30, "state_block": 150, "state_block_hash": STATE_HASH,
                "reserve_in": 2 * 10**18, "reserve_out": 4 * 10**18}]},
            "size_and_profit": {"trade_size_amount_in": 10**18,
                                "gross_quote_profit_wei": 1000},
        }
        b.records.append(later)
        b.write(self.shadow_path)
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        (Path(self.tmp) / "cache").mkdir()
        self.replay.evaluate(self.shadow_path, evaluation_path, str(Path(self.tmp) / "cache"),
                             chain_id=1, catchup_window=1000, rerun=False)
        rec = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(rec["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn(f"{P7.lower()} {WETH}->{A}", rec["reason"],
                      "the missing DIRECTED leg is named")
        self.assertIn("never substituted", rec["reason"])
        self.assertIsNone(rec["recomputed_gross_profit_wei"])

    def test_replay_with_redetection_never_mutates_source(self):
        """R1 §1: a replay whose detection re-run ACTUALLY produces candidates
        must leave the source log byte-identical (no appends) and restore the
        previously configured recorder."""
        _main_replay_fixture().write(self.shadow_path)
        cache_dir = self._cache_cycle_fixture({
            P1: (WETH, A, 10**21, 2 * 10**21, 150),
            P2: (A, B, 2 * 10**21, 4 * 10**21, 150),
            P3: (B, WETH, 4 * 10**21, int(2.4 * 10**21), 150),  # B→WETH rate 0.6
        })
        source_bytes_before = self.shadow_path.read_bytes()
        source_lines_before = len(source_bytes_before.decode().splitlines())
        evaluation_path = Path(self.tmp) / "eval.jsonl"

        sentinel = ShadowRecorder(Path(self.tmp) / "sentinel.jsonl", enabled=True)
        shadow_rec.set_recorder(sentinel)
        try:
            header = self.replay.evaluate(self.shadow_path, evaluation_path,
                                          str(cache_dir), chain_id=1,
                                          catchup_window=1000, rerun=True)
            self.assertTrue(header["redetection"]["ran"])
            self.assertGreaterEqual(header["redetection"]["candidates_detected"], 1,
                                    "the regression requires a NON-EMPTY redetection")
            self.assertEqual(self.shadow_path.read_bytes(), source_bytes_before,
                             "source log byte-identical despite real redetection")
            self.assertEqual(len(self.shadow_path.read_text().splitlines()), source_lines_before,
                             "no replay-produced candidate was appended to the source log")
            self.assertIs(shadow_rec._recorder, sentinel,
                          "the previously configured recorder is restored after replay")
        finally:
            shadow_rec.set_recorder(None)

    def test_chain_isolation(self):
        """R1 §3: the same pool address and block number on two chains with
        different hashes and reserves — replaying each chain uses ONLY its own
        chain's states, and cross-chain hash differences never trigger false
        reorg invalidation."""
        b = _ShadowLogBuilder()
        for chain, later_reserves, h in (
                (1, [(1000 * 10**18, 3000 * 10**18), (3000 * 10**18, 3000 * 10**18)],
                 "0x" + "c1" * 32),
                (2, [(1000 * 10**18, 1000 * 10**18), (1000 * 10**18, 1000 * 10**18)],
                 "0x" + "c2" * 32)):
            b.det("x" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
                  [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))],
                  hash=h, chain_id=chain)
            b.det("x" * 16, [PA, PB], [WETH, A, WETH], 150, 150, later_reserves,
                  hash=h, chain_id=chain, event="UPDATE")
        b.write(self.shadow_path)
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        (Path(self.tmp) / "cache").mkdir()

        h1 = self.replay.evaluate(self.shadow_path, evaluation_path, str(Path(self.tmp) / "cache"),
                                  chain_id=1, catchup_window=1000, rerun=False)
        r1 = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        evaluation_path.unlink()
        h2 = self.replay.evaluate(self.shadow_path, evaluation_path, str(Path(self.tmp) / "cache"),
                                  chain_id=2, catchup_window=1000, rerun=False)
        r2 = list(_evals_by_fp_open_seq(evaluation_path).values())[0]

        self.assertEqual(h1["chain_isolation"]["records_other_chain_excluded"], 2)
        self.assertEqual(h2["chain_isolation"]["records_other_chain_excluded"], 2)
        for r in (r1, r2):
            self.assertNotEqual(r["outcome"], self.replay.OUTCOME_REORG,
                                "cross-chain hash differences never trigger false reorg")
        # chain 1's later state is profitable (3.0 × 1.0 gross), chain 2's is not (1.0 × 1.0)
        self.assertEqual(r1["outcome"], self.replay.OUTCOME_STILL)
        self.assertEqual(r2["outcome"], self.replay.OUTCOME_GONE)
        out1 = _indep_v2_out(_indep_v2_out(10**18, 1000 * 10**18, 3000 * 10**18),
                             3000 * 10**18, 3000 * 10**18)
        self.assertEqual(r1["recomputed_gross_profit_wei"], out1 - 10**18,
                         "chain 1 used ONLY chain-1 reserves")
        out2 = _indep_v2_out(_indep_v2_out(10**18, 1000 * 10**18, 1000 * 10**18),
                             1000 * 10**18, 1000 * 10**18)
        self.assertEqual(r2["recomputed_gross_profit_wei"], out2 - 10**18,
                         "chain 2 used ONLY chain-2 reserves")
        self.assertNotEqual(r1["recomputed_gross_profit_wei"], r2["recomputed_gross_profit_wei"])

    def test_cache_fallback_coherence(self):
        """R1 §4: coherent common-anchor cache fallback — per-pool later-than-
        detection and window checks apply to cache states; per-pool lags are
        reported; the graph-wide maximum cache block never establishes
        freshness for an individual pool."""
        evaluation_path = Path(self.tmp) / "eval.jsonl"

        def run_case(pool_states):
            self.shadow_path.unlink(missing_ok=True)
            b = _ShadowLogBuilder()
            b.det("k" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
                  [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
            b.write(self.shadow_path)
            cache_dir = self._cache_cycle_fixture(pool_states)
            header = self.replay.evaluate(self.shadow_path, evaluation_path,
                                          str(cache_dir), chain_id=1,
                                          catchup_window=1000, rerun=False)
            return header, list(_evals_by_fp_open_seq(evaluation_path).values())[0]

        # reserves are scaled ×1000 so the warmed pools clear the runtime's
        # MIN_LIQUIDITY_USD TVL gate (rates preserved: 2.0 and 0.95)
        PA_R = (1000 * 10**18, 2000 * 10**18)          # WETH→A at PA
        PB_R = (2000 * 10**18, 1900 * 10**18)          # A→WETH at PB (rate 0.95)

        # case 1: all legs near a common anchor → evaluated, lags reported
        _h, r = run_case({
            PA: (WETH, A, PA_R[0], PA_R[1], 500),
            PB: (WETH, A, PB_R[1], PB_R[0], 490),
        })
        self.assertEqual(r["outcome"], self.replay.OUTCOME_STILL)
        self.assertEqual(r["common_anchor_block"], 500)
        lags = r["later_state_lag_from_anchor"]
        self.assertEqual(lags[f"{PA.lower()}:{WETH}->{A}"], 0)
        self.assertEqual(lags[f"{PB.lower()}:{A}->{WETH}"], 10)
        out = _indep_v2_out(_indep_v2_out(10**18, PA_R[0], PA_R[1]), PB_R[0], PB_R[1])
        self.assertEqual(r["recomputed_gross_profit_wei"], out - 10**18)

        # case 2: one pool's cache state predates the detection block → rejected
        _h, r = run_case({
            PA: (WETH, A, PA_R[0], PA_R[1], 500),
            PB: (WETH, A, PB_R[1], PB_R[0], 50),
        })
        self.assertEqual(r["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn(PB.lower(), r["reason"])
        self.assertIn("common anchor", r["reason"])

        # case 3: one pool outside the permitted common-anchor window → rejected
        _h, r = run_case({
            PA: (WETH, A, PA_R[0], PA_R[1], 2000),
            PB: (WETH, A, PB_R[1], PB_R[0], 900),
        })
        self.assertEqual(r["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn(PB.lower(), r["reason"])

        # case 4 (R2 §1 case 2): different heights within the window, with an
        # UNRELATED pool at a MUCH newer block in the cache — the graph-wide
        # maximum (50000) must NOT set the anchor, gate eligibility or
        # contribute state for the route's own pools
        _h, r = run_case({
            PA: (WETH, A, PA_R[0], PA_R[1], 500),
            PB: (WETH, A, PB_R[1], PB_R[0], 450),
            P1: (WETH, A, 1000 * 10**18, 1000 * 10**18, 50000),
        })
        self.assertEqual(r["outcome"], self.replay.OUTCOME_STILL)
        self.assertEqual(r["common_anchor_block"], 500,
                         "anchor comes from the route's OWN pools, not the graph-wide max")
        lags = r["later_state_lag_from_anchor"]
        self.assertEqual(lags[f"{PB.lower()}:{A}->{WETH}"], 50)
        self.assertTrue(all(v >= 0 for v in lags.values()),
                        "reported lags are always non-negative")

    def test_mixed_recorded_cache_common_anchor(self):
        """R2 §1 case 1: one leg's state is RECORDED at block 150, the other
        leg's exact-direction state is a warmed-cache observation at 160. The
        resolver must establish anchor 160 — the highest candidate from the
        route's OWN observations — and prove every leg valid relative to it.
        It must never report anchor 150 while using state 160, and never a
        negative lag."""
        b = _ShadowLogBuilder()
        b.det("m" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        # later snapshot RECORDED for leg 0 (PA WETH→A) at block 150 only;
        # leg 1 (PB A→WETH) has NO recorded later state — it comes from the
        # exact-direction warmed cache at block 160
        b.seq += 1
        mixed = {
            "record_type": "detection", "event": "UPDATE", "schema_version": 1,
            "seq": b.seq, "session": {"session_id": "fixture"}, "recorded_at_unix": 1.0,
            "fingerprint": "m" * 16,
            "identity": {"chain_id": 1, "origin": "block", "pool_addresses": [PA, PB],
                         "ordered_token_path": [WETH, A, WETH]},
            "detection": {"block_number": 150, "block_hash": STATE_HASH, "state_block": 150},
            "market_inputs": {"edges": [{
                "pool": PA, "token_in": WETH, "token_out": A, "version": 2,
                "fee_bps": 30, "state_block": 150, "state_block_hash": STATE_HASH,
                "reserve_in": 1000 * 10**18, "reserve_out": 2000 * 10**18}]},
            "size_and_profit": {"trade_size_amount_in": 10**18,
                                "gross_quote_profit_wei": 1000},
        }
        b.records.append(mixed)
        b.write(self.shadow_path)
        cache_dir = self._cache_cycle_fixture({
            PB: (WETH, A, 1900 * 10**18, 2000 * 10**18, 160),
        })
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        self.replay.evaluate(self.shadow_path, evaluation_path, str(cache_dir),
                             chain_id=1, catchup_window=1000, rerun=False)
        r = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(r["outcome"], self.replay.OUTCOME_STILL)
        self.assertEqual(r["common_anchor_block"], 160,
                         "the anchor is the highest route-own candidate, not the recorded block")
        blocks = r["later_state_blocks"]
        lags = r["later_state_lag_from_anchor"]
        self.assertEqual(blocks[f"{PA.lower()}:{WETH}->{A}"], 150,
                         "leg 0 uses its recorded state at 150")
        self.assertEqual(blocks[f"{PB.lower()}:{A}->{WETH}"], 160,
                         "leg 1 uses its exact-direction cache state at 160")
        self.assertEqual(lags[f"{PA.lower()}:{WETH}->{A}"], 10)
        self.assertEqual(lags[f"{PB.lower()}:{A}->{WETH}"], 0)
        self.assertTrue(all(v >= 0 for v in lags.values()),
                         "no negative lag is ever reported")
        # the mixed-state profit equals the independent calculation over the
        # ACTUAL selected states (PA recorded@150, PB cache@160)
        out = _indep_v2_out(_indep_v2_out(10**18, 1000 * 10**18, 2000 * 10**18),
                            2000 * 10**18, 1900 * 10**18)
        self.assertEqual(r["recomputed_gross_profit_wei"], out - 10**18)

    def test_route_leg_older_than_detection_rejected(self):
        """R2 §1 case 3: a required leg whose only exact-direction state is
        older than the detection block is rejected — it is named, and no
        profitability value is produced."""
        b = _ShadowLogBuilder()
        b.det("o" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        b.write(self.shadow_path)
        cache_dir = self._cache_cycle_fixture({
            PA: (WETH, A, 1000 * 10**18, 2000 * 10**18, 500),
            PB: (WETH, A, 1900 * 10**18, 2000 * 10**18, 50),  # predates detection
        })
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        self.replay.evaluate(self.shadow_path, evaluation_path, str(cache_dir),
                             chain_id=1, catchup_window=1000, rerun=False)
        r = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(r["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn(PB.lower(), r["reason"])
        self.assertIn("not later than the detection block", r["reason"])
        self.assertIsNone(r["recomputed_gross_profit_wei"])

    def test_route_leg_exceeding_anchor_lag_rejected(self):
        """R2 §1 case 4: a leg whose state exceeds the permitted common-anchor
        lag is rejected and named; the route is never evaluated on a mixed
        anchor."""
        b = _ShadowLogBuilder()
        b.det("p" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        b.write(self.shadow_path)
        cache_dir = self._cache_cycle_fixture({
            PA: (WETH, A, 1000 * 10**18, 2000 * 10**18, 2000),
            PB: (WETH, A, 1900 * 10**18, 2000 * 10**18, 900),  # lag 1100 > window
        })
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        self.replay.evaluate(self.shadow_path, evaluation_path, str(cache_dir),
                             chain_id=1, catchup_window=1000, rerun=False)
        r = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(r["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn(PB.lower(), r["reason"])
        self.assertIn("common anchor", r["reason"])
        self.assertIsNone(r["recomputed_gross_profit_wei"])

    def test_latest_state_selection_prefers_newer_recorded_over_older_cache(self):
        """R3 §1 cases 1+4: a route leg RECORDED at block 150 with an
        exact-direction warmed-cache observation at block 120 — the resolver
        merges the observations, orders them chronologically and selects the
        genuinely latest eligible state (recorded@150); the stale cache
        observation can never replace it, and the recomputed profit equals
        the independent constant-product calculation over the SELECTED
        states' reserves (deliberately different at the competing blocks)."""
        b = _ShadowLogBuilder()
        b.det("n" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        # later snapshot RECORDED for leg 0 (PA WETH→A) at block 150 with
        # distinctive reserves; leg 1 (PB A→WETH) has no recorded later state
        b.seq += 1
        b.records.append({
            "record_type": "detection", "event": "UPDATE", "schema_version": 1,
            "seq": b.seq, "session": {"session_id": "fixture"}, "recorded_at_unix": 1.0,
            "fingerprint": "n" * 16,
            "identity": {"chain_id": 1, "origin": "block", "pool_addresses": [PA, PB],
                         "ordered_token_path": [WETH, A, WETH]},
            "detection": {"block_number": 150, "block_hash": STATE_HASH,
                          "state_block": 150},
            "market_inputs": {"edges": [{
                "pool": PA, "token_in": WETH, "token_out": A, "version": 2,
                "fee_bps": 30, "state_block": 150, "state_block_hash": STATE_HASH,
                "reserve_in": 1000 * 10**18, "reserve_out": 2000 * 10**18}]},
            "size_and_profit": {"trade_size_amount_in": 10**18,
                                "gross_quote_profit_wei": 1000},
        })
        b.write(self.shadow_path)
        # a STALE exact-direction cache observation for the SAME leg at an
        # older block, with deliberately different reserves
        cache_dir = self._cache_cycle_fixture({
            PA: (WETH, A, 1500 * 10**18, 3000 * 10**18, 120),
            PB: (WETH, A, 1900 * 10**18, 2000 * 10**18, 150),
        })
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        self.replay.evaluate(self.shadow_path, evaluation_path, str(cache_dir),
                             chain_id=1, catchup_window=1000, rerun=False)
        r = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(r["outcome"], self.replay.OUTCOME_STILL)
        self.assertEqual(r["common_anchor_block"], 150)
        self.assertEqual(r["later_state_blocks"][f"{PA.lower()}:{WETH}->{A}"], 150,
                         "the genuinely latest eligible state (recorded@150) is "
                         "selected, not the stale cache observation")
        self.assertEqual(r["later_state_blocks"][f"{PB.lower()}:{A}->{WETH}"], 150)
        lags = r["later_state_lag_from_anchor"]
        self.assertEqual(lags[f"{PA.lower()}:{WETH}->{A}"], 0)
        self.assertEqual(lags[f"{PB.lower()}:{A}->{WETH}"], 0)
        self.assertTrue(all(v >= 0 for v in lags.values()))
        legs_ev = r["recompute_legs"]
        self.assertEqual(legs_ev[0]["state_source"], "recorded")
        self.assertEqual(legs_ev[1]["state_source"], "cache_warm_graph")
        # independent profitability over the SELECTED reserves
        out = _indep_v2_out(_indep_v2_out(10**18, 1000 * 10**18, 2000 * 10**18),
                            2000 * 10**18, 1900 * 10**18)
        self.assertEqual(r["recomputed_gross_profit_wei"], out - 10**18)
        # ...and NOT the result the stale cache reserves would produce
        stale = _indep_v2_out(_indep_v2_out(10**18, 1500 * 10**18, 3000 * 10**18),
                              2000 * 10**18, 1900 * 10**18)
        self.assertNotEqual(r["recomputed_gross_profit_wei"], stale - 10**18,
                            "stale cache reserves must not replace the recorded state")

    def test_multiple_observations_select_latest_eligible(self):
        """R3 §1 case 3: one route leg carries several recorded observations
        and an exact-direction cache observation at different blocks — the
        latest eligible observation at or below the selected anchor is always
        chosen."""
        b = _ShadowLogBuilder()
        b.det("q" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])

        def update(block, edges):
            b.seq += 1
            b.records.append({
                "record_type": "detection", "event": "UPDATE", "schema_version": 1,
                "seq": b.seq, "session": {"session_id": "fixture"},
                "recorded_at_unix": 1.0, "fingerprint": "q" * 16,
                "identity": {"chain_id": 1, "origin": "block",
                             "pool_addresses": [PA, PB],
                             "ordered_token_path": [WETH, A, WETH]},
                "detection": {"block_number": block, "block_hash": STATE_HASH,
                              "state_block": block},
                "market_inputs": {"edges": edges},
                "size_and_profit": {"trade_size_amount_in": 10**18,
                                    "gross_quote_profit_wei": 1000},
            })

        update(120, [{"pool": PA, "token_in": WETH, "token_out": A, "version": 2,
                      "fee_bps": 30, "state_block": 120,
                      "state_block_hash": STATE_HASH,
                      "reserve_in": 1100 * 10**18, "reserve_out": 2200 * 10**18}])
        update(135, [{"pool": PA, "token_in": WETH, "token_out": A, "version": 2,
                      "fee_bps": 30, "state_block": 135,
                      "state_block_hash": STATE_HASH,
                      "reserve_in": 1200 * 10**18, "reserve_out": 2400 * 10**18}])
        update(150, [{"pool": PB, "token_in": A, "token_out": WETH, "version": 2,
                      "fee_bps": 30, "state_block": 150,
                      "state_block_hash": STATE_HASH,
                      "reserve_in": 2000 * 10**18, "reserve_out": 1900 * 10**18}])
        b.write(self.shadow_path)
        cache_dir = self._cache_cycle_fixture({
            PA: (WETH, A, 1300 * 10**18, 2600 * 10**18, 148),
        })
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        self.replay.evaluate(self.shadow_path, evaluation_path, str(cache_dir),
                             chain_id=1, catchup_window=1000, rerun=False)
        r = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(r["outcome"], self.replay.OUTCOME_STILL)
        self.assertEqual(r["common_anchor_block"], 150,
                         "the highest route-own candidate anchor wins")
        blocks = r["later_state_blocks"]
        lags = r["later_state_lag_from_anchor"]
        self.assertEqual(blocks[f"{PA.lower()}:{WETH}->{A}"], 148,
                         "the cache observation at 148 is the greatest eligible "
                         "block at or below the anchor — not 135, not 120")
        self.assertEqual(blocks[f"{PB.lower()}:{A}->{WETH}"], 150)
        self.assertEqual(lags[f"{PA.lower()}:{WETH}->{A}"], 2)
        self.assertEqual(lags[f"{PB.lower()}:{A}->{WETH}"], 0)
        self.assertTrue(all(v >= 0 for v in lags.values()))
        out = _indep_v2_out(_indep_v2_out(10**18, 1300 * 10**18, 2600 * 10**18),
                            2000 * 10**18, 1900 * 10**18)
        self.assertEqual(r["recomputed_gross_profit_wei"], out - 10**18,
                         "profit uses the selected @148 reserves")
        older = _indep_v2_out(_indep_v2_out(10**18, 1200 * 10**18, 2400 * 10**18),
                              2000 * 10**18, 1900 * 10**18)
        self.assertNotEqual(r["recomputed_gross_profit_wei"], older - 10**18,
                            "the @135 observation was not selected")

    def test_same_block_recorded_cache_hash_handling(self):
        """R3 §1 case 5: recorded and cache observations at the SAME block
        are handled deterministically — equivalent observations (compatible
        hashes) prefer the recorded evidence; conflicting block hashes are
        NOT silently treated as equivalent state: the block is never selected
        and the candidate is declared not evaluable naming the conflict and
        both hashes."""
        # (a) equivalent: same block, same hash → the RECORDED entry is used
        b = _ShadowLogBuilder()
        b.det("s" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        b.seq += 1
        b.records.append({
            "record_type": "detection", "event": "UPDATE", "schema_version": 1,
            "seq": b.seq, "session": {"session_id": "fixture"},
            "recorded_at_unix": 1.0, "fingerprint": "s" * 16,
            "identity": {"chain_id": 1, "origin": "block",
                         "pool_addresses": [PA, PB],
                         "ordered_token_path": [WETH, A, WETH]},
            "detection": {"block_number": 150, "block_hash": STATE_HASH,
                          "state_block": 150},
            "market_inputs": {"edges": [{
                "pool": PA, "token_in": WETH, "token_out": A, "version": 2,
                "fee_bps": 30, "state_block": 150, "state_block_hash": STATE_HASH,
                "reserve_in": 1000 * 10**18, "reserve_out": 2000 * 10**18}]},
            "size_and_profit": {"trade_size_amount_in": 10**18,
                                "gross_quote_profit_wei": 1000},
        })
        b.write(self.shadow_path)
        cache_dir = self._cache_cycle_fixture({
            PA: (WETH, A, 1500 * 10**18, 3000 * 10**18, 150),  # same block+hash
            PB: (WETH, A, 1900 * 10**18, 2000 * 10**18, 150),
        })
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        self.replay.evaluate(self.shadow_path, evaluation_path, str(cache_dir),
                             chain_id=1, catchup_window=1000, rerun=False)
        r = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(r["outcome"], self.replay.OUTCOME_STILL)
        self.assertEqual(r["recompute_legs"][0]["state_source"], "recorded",
                         "deterministic preference: recorded evidence over the "
                         "equivalent same-block cache observation")
        out = _indep_v2_out(_indep_v2_out(10**18, 1000 * 10**18, 2000 * 10**18),
                            2000 * 10**18, 1900 * 10**18)
        self.assertEqual(r["recomputed_gross_profit_wei"], out - 10**18,
                         "the recorded reserves (not the same-block cache "
                         "reserves) are used")

        # (b) conflicting: same block, DIFFERENT hashes → never equivalent
        self.shadow_path.unlink()
        recorded_hash = "0x" + "aa" * 32
        b = _ShadowLogBuilder()
        b.det("t" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        b.seq += 1
        b.records.append({
            "record_type": "detection", "event": "UPDATE", "schema_version": 1,
            "seq": b.seq, "session": {"session_id": "fixture"},
            "recorded_at_unix": 1.0, "fingerprint": "t" * 16,
            "identity": {"chain_id": 1, "origin": "block",
                         "pool_addresses": [PA, PB],
                         "ordered_token_path": [WETH, A, WETH]},
            "detection": {"block_number": 150, "block_hash": recorded_hash,
                          "state_block": 150},
            "market_inputs": {"edges": [{
                "pool": PA, "token_in": WETH, "token_out": A, "version": 2,
                "fee_bps": 30, "state_block": 150,
                "state_block_hash": recorded_hash,
                "reserve_in": 1000 * 10**18, "reserve_out": 2000 * 10**18}]},
            "size_and_profit": {"trade_size_amount_in": 10**18,
                                "gross_quote_profit_wei": 1000},
        })
        b.write(self.shadow_path)
        cache_dir2 = self._cache_cycle_fixture({
            PA: (WETH, A, 1500 * 10**18, 3000 * 10**18, 150),  # hash STATE_HASH
            PB: (WETH, A, 1900 * 10**18, 2000 * 10**18, 150),
        })
        evaluation_path2 = Path(self.tmp) / "eval2.jsonl"
        self.replay.evaluate(self.shadow_path, evaluation_path2, str(cache_dir2),
                             chain_id=1, catchup_window=1000, rerun=False)
        r2 = list(_evals_by_fp_open_seq(evaluation_path2).values())[0]
        self.assertEqual(r2["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn("conflicting state-hash evidence", r2["reason"])
        self.assertIn(recorded_hash, r2["reason"], "the recorded hash is named")
        self.assertIn(STATE_HASH, r2["reason"], "the cache hash is named")
        self.assertIn(f"{PA.lower()} {WETH}->{A}", r2["reason"],
                      "the affected leg is named")
        self.assertIsNone(r2["recomputed_gross_profit_wei"])
        conflicts = r2["state_hash_conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["block"], 150)
        self.assertEqual(conflicts[0]["pool"], PA.lower())
        self.assertEqual(conflicts[0]["observed_hashes"],
                         sorted([recorded_hash, STATE_HASH]))
        self.assertEqual(conflicts[0]["sources"], ["cache_warm_graph", "recorded"])

    def test_unknown_pool_version_fails_closed(self):
        """R2 §2 + R3 §2: only an explicit INTEGER V2 permits the
        constant-product recompute; an explicit integer V3 keeps the
        not-evaluable quoter classification; booleans, floats (including
        2.0 and non-integral 2.5), strings, null, zero, malformed values and
        unsupported integer versions are NEVER defaulted or coerced to V2 —
        no profitability value, and the evaluation output retains the
        affected leg with its exact pool, direction, raw version and
        reason."""
        leg = (P6.lower(), WETH, A)

        def _resolved(raw_version):
            edge = {"pool": P6, "token_in": WETH, "token_out": A, "fee_bps": 30,
                    "reserve_in": 1000 * 10**18, "reserve_out": 2000 * 10**18}
            if raw_version is not ...:
                edge["version"] = raw_version
            return {0: {"block": 150, "edge": edge, "source": "recorded",
                        "state_lag_from_anchor": 0}}

        # explicit integer V2 → the independent constant-product calculation passes
        out = _indep_v2_out(10**18, 1000 * 10**18, 2000 * 10**18)
        rec = self.replay.recompute_route_profit(10**18, [leg], _resolved(2))
        self.assertTrue(rec["evaluable"])
        self.assertEqual(rec["profit_wei"], out - 10**18)
        # explicit integer V3 → not evaluable, quoter reason
        rec = self.replay.recompute_route_profit(10**18, [leg], _resolved(3))
        self.assertFalse(rec["evaluable"])
        self.assertIsNone(rec["profit_wei"])
        self.assertIn("quoter", rec["reason"])
        self.assertEqual(rec["affected_leg"]["pool"], P6.lower())
        self.assertEqual(rec["affected_leg"]["raw_version"], 3)
        # strict matrix: bools, floats (2.0 included), strings, null, zero,
        # malformed and unsupported integer versions → unknown, never coerced
        for raw in (..., None, "", 0, "v2", "2", 1, 4, True, False, 2.0, 2.5, "2.5"):
            rec = self.replay.recompute_route_profit(10**18, [leg], _resolved(raw))
            self.assertFalse(rec["evaluable"], f"version {raw!r} must not evaluate")
            self.assertIsNone(rec["profit_wei"])
            self.assertIn("never", rec["reason"])
            self.assertIn("coerced", rec["reason"])
            self.assertIn("V2", rec["reason"])
            self.assertEqual(rec["affected_leg"]["pool"], P6.lower())
            self.assertEqual(rec["affected_leg"]["raw_version"],
                             None if raw is ... else raw)

        # end-to-end: the evaluation output retains the affected leg + reason
        b = _ShadowLogBuilder()
        b.det("v" * 16, [P6], [WETH, A], 100, 100, [(1000 * 10**18, 2000 * 10**18)])
        b.seq += 1
        later = {
            "record_type": "detection", "event": "UPDATE", "schema_version": 1,
            "seq": b.seq, "session": {"session_id": "fixture"}, "recorded_at_unix": 1.0,
            "fingerprint": "v" * 16,
            "identity": {"chain_id": 1, "origin": "block", "pool_addresses": [P6],
                         "ordered_token_path": [WETH, A]},
            "detection": {"block_number": 150, "block_hash": STATE_HASH, "state_block": 150},
            "market_inputs": {"edges": [{
                "pool": P6, "token_in": WETH, "token_out": A,
                "fee_bps": 30, "state_block": 150, "state_block_hash": STATE_HASH,
                "reserve_in": 1000 * 10**18, "reserve_out": 2000 * 10**18}]},
            "size_and_profit": {"trade_size_amount_in": 10**18,
                                "gross_quote_profit_wei": 1000},
        }
        b.records.append(later)
        b.write(self.shadow_path)
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        (Path(self.tmp) / "cache").mkdir()
        self.replay.evaluate(self.shadow_path, evaluation_path, str(Path(self.tmp) / "cache"),
                             chain_id=1, catchup_window=1000, rerun=False)
        r = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(r["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn("version", r["reason"])
        self.assertIn(f"{P6.lower()} {WETH}->{A}", r["reason"])
        self.assertIsNone(r["recomputed_gross_profit_wei"])
        self.assertEqual(r["route_leg_affected"]["pool"], P6.lower())
        self.assertEqual(r["route_leg_affected"]["later_state_block"], 150)
        self.assertIsNone(r["route_leg_affected"]["raw_version"],
                          "the missing version is retained as null, never implied")

    def test_replay_evaluates_distinct_lifecycles_separately(self):
        """R1 §8: two OPEN lifecycles of the same fingerprint are TWO
        separately identifiable evaluations; CLOSE is associated with its
        matching lifecycle sequence."""
        b = _ShadowLogBuilder()
        b.det("g" * 16, [PG1, PG2], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        close_rec = b.close("g" * 16, outcome="EXPIRED")
        b.det("g" * 16, [PG1, PG2], [WETH, A, WETH], 150, 150,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        b.write(self.shadow_path)
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        (Path(self.tmp) / "cache").mkdir()
        self.replay.evaluate(self.shadow_path, evaluation_path, str(Path(self.tmp) / "cache"),
                             chain_id=1, catchup_window=1000, rerun=False)
        evals = _evals_by_fp_open_seq(evaluation_path)
        g_evals = {seq: r for (fp, seq), r in evals.items() if fp == "g" * 16}
        self.assertEqual(len(g_evals), 2, "two distinct lifecycles → two evaluations")
        first_seq, second_seq = sorted(g_evals)
        first, second = g_evals[first_seq], g_evals[second_seq]
        self.assertEqual(first["lifecycle"]["open_seq"], first_seq)
        self.assertEqual(first["lifecycle"]["close_seq"], close_rec["seq"],
                         "the CLOSE is associated with the lifecycle it terminates")
        self.assertEqual(first["lifecycle"]["close_outcome"], "EXPIRED")
        self.assertEqual(second["lifecycle"]["open_evidence"], "recorded")
        self.assertIsNone(second["lifecycle"]["close_seq"])
        self.assertEqual(second["detected_block"], 150)
        self.assertEqual(second["outcome"], self.replay.OUTCOME_MISSING,
                         "the second lifecycle has no state after its own OPEN")

    def test_fallback_lifecycle_classification_without_fabrication(self):
        """R1 §8: an UPDATE whose OPEN is outside the recorded history keeps
        the fallback classification and is still evaluated from its own
        snapshot — without fabricating the missing OPEN."""
        b = _ShadowLogBuilder()
        b.det("h" * 16, [PA, PB], [WETH, A, WETH], 100, 100,
              [(10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        orphan = dict(b.det("h" * 16, [PA, PB], [WETH, A, WETH], 150, 150,
                            [(2 * 10**18, 6 * 10**18), (6 * 10**18, int(5.7 * 10**18))],
                            event="UPDATE"))
        # drop the OPEN record from the file: the UPDATE's OPEN is outside the
        # recorded history
        b.records = [r for r in b.records
                     if not (r.get("record_type") == "detection"
                             and r.get("event") == "OPEN" and r.get("fingerprint") == "h" * 16)]
        b.write(self.shadow_path)
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        (Path(self.tmp) / "cache").mkdir()
        self.replay.evaluate(self.shadow_path, evaluation_path, str(Path(self.tmp) / "cache"),
                             chain_id=1, catchup_window=1000, rerun=False)
        r = list(_evals_by_fp_open_seq(evaluation_path).values())[0]
        self.assertEqual(r["lifecycle"]["open_seq"], None)
        self.assertEqual(r["lifecycle"]["open_evidence"], "outside_recorded_history")
        self.assertEqual(r["outcome"], self.replay.OUTCOME_MISSING,
                         "no later state after the orphan snapshot → declared, not inferred")
        self.assertIn("no coherent common anchor", r["reason"])


class TestAggregatesAndEvidence(RecorderTestBase, _ReplayTestMixin):
    @classmethod
    def setUpClass(cls):
        cls.agg = cls._load_script("shadow_aggregate")
        cls.replay = cls._load_script("shadow_replay")

    def test_observed_overlap_not_reported_as_confirmed_capture(self):
        tracker = OpportunityTracker(_NoopOppLogger())
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        _engine(_graph()).process_candidates([opp], 200)
        tracker.process_block(200, [opp])
        tracker.process_block(201, [], filled_pools={P1.lower(): "0x" + "ff" * 32})
        agg = self.agg.aggregate(self.shadow_path, Path(self.tmp) / "eval.jsonl")
        self.assertEqual(agg["competition_evidence"]["observed_pool_overlap_closes"], 1)
        self.assertEqual(agg["competition_evidence"]["confirmed_competitor_captures"], 0)
        self.assertIn("NOT", agg["competition_evidence"]["claim"])
        self.assertIn("confirmed_competitor_capture", agg["unavailable_aggregates"])
        close = [r for r in self.records() if r.get("record_type") == "lifecycle"][0]
        self.assertFalse(close["competition_evidence"]["confirmed_competitor_capture"])
        self.assertEqual(close["competition_evidence"]["basis"], "tracker_pool_overlap_heuristic")

    def test_aggregates_honest_missing_denominators(self):
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        _engine(_graph()).process_candidates([opp], 100)
        agg = self.agg.aggregate(self.shadow_path, Path(self.tmp) / "eval.jsonl")
        self.assertEqual(agg["detection_yield"]["counts_by_origin"], {"block": 1})
        rate = agg["detection_yield"]["detection_yield_rate"]
        self.assertEqual(rate["status"], "unavailable")
        self.assertIn("searched-universe", rate["missing_evidence_source"])
        csv_path = Path(self.tmp) / "agg.csv"
        self.agg.write_csv(agg, csv_path)
        rows = list(csv.reader(open(csv_path, encoding="utf-8")))
        self.assertEqual(rows[0], ["section", "key", "value"])
        self.assertTrue(any(r[0] == "detection_count_by_origin" for r in rows))


class TestNoLiveRPC(unittest.TestCase):
    """No replay or recorder test performs a live RPC request: the recorder and
    the replay carry no HTTP/WebSocket provider plumbing at all, and the
    offline harness above uses only stub w3 / stub graph clients."""

    def test_recorder_and_replay_have_no_live_rpc_plumbing(self):
        banned = ("HTTPProvider", "IPCProvider", "WebsocketProvider",
                  "requests.get", "urllib.request.urlopen")
        for mod in ("shadow/recorder.py", "scripts/shadow_replay.py", "scripts/shadow_aggregate.py"):
            source = (REPO / mod).read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, source, f"{mod} must not contain {token}")


if __name__ == "__main__":
    unittest.main()
