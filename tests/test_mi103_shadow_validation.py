"""DSS-WO-MI-103 — shadow-mode validation recorder + offline replay regressions.

Deterministic, fully OFFLINE tests (stub w3 / stub graph client / temporary log
paths; no test performs a live RPC request). Covers work order §8:

- block-origin candidate recording (shared recording point)
- mempool-origin candidate recording through the SAME recording point
- candidates detected but NOT execution-ready are recorded
- repeated detection → UPDATE, not duplicate OPEN
- shared fingerprint across detector theses for the same executable route
- terminal CLOSE on expired / overlap / executed lifecycle paths
- duplicate CLOSE is idempotent
- restart reconstruction continues an open lifecycle
- reorg records preserve competing block-hash observations
- detection-time records remain unchanged after replay
- replay writes SEPARATE evaluation records, source untouched
- replay explicitly reports unavailable history and ranges beyond the
  1,000-block catch-up window
- observed pool overlap is NOT reported as confirmed competitor capture
- missing fields stay null/unavailable, never fabricated
- the recorder and replay contain no live-RPC plumbing (source scan)
"""
import hashlib
import csv
import importlib.util
import json
import sys
import tempfile
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
P1 = "0x" + "01" * 20   # WETH/A  rate 2.0
P2 = "0x" + "02" * 20   # A/B     rate 2.0
P3 = "0x" + "03" * 20   # B/WETH  rate 1.9 (round trip profitable)
TVL = 250_000.0
STATE_HASH = "0x" + "ab" * 32


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


class _ReplayTestMixin:
    """Load scripts/shadow_replay.py and scripts/shadow_aggregate.py as modules."""

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
    """§8: block origin, mempool origin, non-ready candidates, UPDATE vs OPEN,
    shared fingerprint, null fields, no live-RPC plumbing."""

    def test_block_origin_candidate_recorded(self):
        """Block-origin candidate through the shared process_candidates exit —
        detection evidence includes local-graph market inputs and the block
        execution configuration state."""
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        result = _engine(_graph()).process_candidates([opp], 100)
        self.assertEqual(len(result.candidates), 1)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        self.assertEqual(len(recs), 1, "exactly one detection record")
        rec = recs[0]
        self.assertEqual(rec["event"], "OPEN")
        self.assertEqual(rec["fingerprint"], _fingerprint(opp))
        self.assertEqual(rec["identity"]["origin"], "block")
        self.assertEqual(rec["identity"]["origin_evidence"]["is_mempool"], False)
        self.assertEqual(rec["identity"]["canonical_fingerprint"], rec["fingerprint"])
        self.assertEqual(rec["identity"]["detector_source"], "cycles")
        self.assertEqual(rec["identity"]["pool_addresses"], [P1, P2, P3])
        self.assertEqual(rec["identity"]["ordered_token_path"], [WETH, A, B, WETH])
        edges = rec["market_inputs"]["edges"]
        self.assertEqual(len(edges), 6, "both directions of all three pools from the LOCAL graph")
        self.assertTrue(all(e["reserve_in"] and e["state_block"] == 100 and e["state_block_hash"]
                            for e in edges), "per-edge state block/hash preserved")
        self.assertEqual(rec["reorg_evidence"]["per_edge_state_block_hashes"][P1], STATE_HASH)
        self.assertFalse(rec["execution_config_at_decision"]["EXECUTE_ONCHAIN"])
        self.assertEqual(rec["execution_config_at_decision"]["execute_onchain_source"],
                         "config.EXECUTE_ONCHAIN")

    def test_mempool_origin_candidate_recorded_through_shared_point(self):
        """Mempool-origin candidate — the SAME process_candidates recording
        point (MempoolPipeline._promote_and_route calls it) — origin captured
        from existing is_mempool provenance, mempool-to-open latency computed
        from the pending-seen timestamp, trigger tx preserved."""
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
        self.assertLess(latency, 5.0, "pending-seen → open latency computed, not fabricated")

    def test_not_execution_ready_candidate_recorded(self):
        """Candidates detected but NOT execution-ready are recorded with their
        actual validation/eligibility outcome — never silently dropped."""
        opp = _opp([P1, P2], [WETH, A, WETH], meta={"execution_ready": False,
                                                    "route_state_status": "STALE"})
        result = _engine(_graph()).process_candidates([opp], 100)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        self.assertEqual(len(result.ready), 0)
        self.assertEqual(len(recs), 1, "non-ready candidate still recorded")
        self.assertFalse(recs[0]["eligibility"]["execution_ready"])
        self.assertEqual(recs[0]["eligibility"]["validation_status"], "quote_validated")
        self.assertEqual(recs[0]["market_inputs"]["route_state_status"], "STALE")

    def test_repeated_detection_produces_update_not_duplicate_open(self):
        """One OPEN per fingerprint per open lifecycle; the second detection is
        an UPDATE snapshot with a monotone sequence and update count."""
        engine = _engine(_graph())
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        engine.process_candidates([opp], 100)
        engine.process_candidates([_opp([P1, P2, P3], [WETH, A, B, WETH],
                                        meta={"quote_profit_wei": 999})], 101)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        self.assertEqual([r["event"] for r in recs], ["OPEN", "UPDATE"])
        self.assertEqual(recs[1]["lifecycle"]["open_seq"], recs[0]["seq"],
                         "UPDATE references the same lifecycle OPEN")
        self.assertEqual(recs[1]["lifecycle"]["update_count"], 1)
        self.assertGreater(recs[1]["seq"], recs[0]["seq"], "monotone sequence")
        self.assertEqual(recs[1]["size_and_profit"]["gross_quote_profit_wei"], 999,
                         "latest snapshot carries the newer value")

    def test_shared_fingerprint_across_detector_theses(self):
        """The same executable route discovered by two detector theses shares
        the canonical fingerprint → one lifecycle, second detection is UPDATE
        (ENG-017 identity; no second fingerprint algorithm)."""
        engine = _engine(_graph())
        o1 = _opp([P1, P2, P3], [WETH, A, B, WETH], arb_kind=ArbKind.CYCLE, detector="cycles")
        o2 = _opp([P1, P2, P3], [WETH, A, B, WETH], arb_kind=ArbKind.CROSS_DEX_SPREAD,
                  detector="cross_proto", meta={"quote_profit_wei": 555})
        self.assertEqual(_fingerprint(o1), _fingerprint(o2))
        engine.process_candidates([o1], 100)
        engine.process_candidates([o2], 101)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        self.assertEqual([r["event"] for r in recs], ["OPEN", "UPDATE"],
                         "same route from another thesis → UPDATE, no duplicate OPEN")
        theses = {r["identity"]["strategy_thesis"]["arb_kind"] for r in recs}
        self.assertEqual(theses, {"cycle", "cross_dex_spread"},
                         "thesis distinction preserved inside one lifecycle")
        self.assertEqual(recs[1]["identity"]["detector_source"], "cross_proto")

    def test_missing_fields_stay_null_not_fabricated(self):
        """A candidate without quote/simulation metadata records null — never
        estimates disguised as observed data — and lists structurally
        unavailable fields explicitly."""
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
        self.assertIsNone(rec["market_inputs"]["quote_method"])
        self.assertIsNone(rec["detection"]["mempool_to_open_latency_seconds"],
                          "block-origin candidates have no mempool latency")
        for field in ("router_decision", "builder_tip_tier", "quote_method",
                      "size_selection_method", "inclusion_block", "bundle_position"):
            self.assertIn(field, rec["unavailable_fields"])
        self.assertNotIn("provider_url", json.dumps(rec).lower())
        self.assertNotIn("private_key", json.dumps(rec).lower())


class TestLifecycleRecording(RecorderTestBase):
    """§8: terminal CLOSE paths, idempotency, restart reconstruction, reorg
    evidence, competition-evidence class."""

    def _tracker(self):
        return OpportunityTracker(_NoopOppLogger())

    def test_terminal_close_on_expired_overlap_and_executed_paths(self):
        """All three closure paths converge on the central _write_lifecycle →
        the recorder appends one CLOSE per lifecycle with the terminal outcome
        and available evidence. The OPEN is recorded through the shared
        detection point first — the real runtime data flow (engine
        process_candidates → tracker lifecycle)."""
        tracker = self._tracker()
        opp_exp = _opp([P1, P2], [WETH, A, WETH])
        opp_exec = _opp([P2, P3], [A, B, A])
        opp_fill = _opp([P3], [B, WETH, B])
        # the shared recording point records the detection OPEN first
        _engine(_graph()).process_candidates([opp_exp, opp_exec, opp_fill], 200)
        tracker.process_block(200, [opp_exp, opp_exec, opp_fill])
        # EXECUTED close via close_executed
        tracker.close_executed(opp_exec, 201,
                               ExecutionResult(success=True, tx_hash="0x" + "ee" * 32,
                                               method="direct", lender="", amount_in=1,
                                               profit_wei=77, gas_cost_usd=0.2),
                               actual_profit_usd=31.5)
        # FILLED_BY_COMPETITOR close: pool touched by a mined tx
        tracker.process_block(202, [], filled_pools={P3.lower(): "0x" + "ff" * 32})
        # EXPIRED close: not re-detected within the TTL
        tracker.process_block(206, [], filled_pools={})

        closes = [r for r in self.records() if r.get("record_type") == "lifecycle"]
        self.assertEqual(len(closes), 3, "one CLOSE per lifecycle")
        by_outcome = {c["terminal"]["outcome"]: c for c in closes}
        self.assertEqual(set(by_outcome), {"EXECUTED", "FILLED_BY_COMPETITOR", "EXPIRED"})
        executed = by_outcome["EXECUTED"]
        self.assertEqual(executed["execution_outcome"]["actual_profit_usd"], 31.5)
        self.assertEqual(executed["execution_outcome"]["tx_hash"], "0x" + "ee" * 32)
        self.assertIsNone(executed["execution_outcome"]["inclusion_block"],
                          "inclusion block is not tracked — null, never invented")
        filled = by_outcome["FILLED_BY_COMPETITOR"]
        self.assertEqual(filled["competition_evidence"]["affected_pool"], P3.lower())
        self.assertEqual(filled["competition_evidence"]["observed_tx_hash"], "0x" + "ff" * 32)
        self.assertEqual(filled["terminal"]["outcome_basis"], "tracker_pool_overlap_heuristic")
        expired = by_outcome["EXPIRED"]
        self.assertEqual(expired["terminal"]["outcome_basis"], "tracker_absence_or_ttl_heuristic")
        for c in closes:
            self.assertIsNotNone(c["lifecycle"]["open_seq"],
                                 "terminal records reference their lifecycle OPEN seq")

    def test_duplicate_close_is_idempotent(self):
        """A repeated close must not create another terminal record (§4.3)."""
        tracker = self._tracker()
        opp = _opp([P1, P2], [WETH, A, WETH])
        tracker.process_block(200, [opp])
        tracker.process_block(206, [], filled_pools={})  # EXPIRED close
        closes_before = len([r for r in self.records() if r.get("record_type") == "lifecycle"])
        # a second close_executed for the same (already closed) fingerprint
        tracker.close_executed(opp, 207,
                               ExecutionResult(success=True, tx_hash="0x" + "11" * 32,
                                               method="direct", lender="", amount_in=1,
                                               profit_wei=1, gas_cost_usd=0.0), 1.0)
        # and a direct duplicate terminal record through the central path
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
        self.assertEqual(len(closes_after), closes_before, "no second terminal record")
        self.assertGreaterEqual(self.recorder.stats["duplicate_closes_suppressed"], 1)

    def test_restart_reconstruction_continues_open_lifecycle(self):
        """A new process reconstructs open lifecycles from a bounded scan of
        its own JSONL tail: a re-detection continues the SAME lifecycle (no
        duplicate OPEN), with the process-start session stamped."""
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        _engine(_graph()).process_candidates([opp], 100)
        open_seq = self.records()[0]["seq"]
        session_a = self.records()[0]["session"]["session_id"]

        restarted = ShadowRecorder(self.shadow_path, tail_scan_lines=10_000)
        self.assertGreater(restarted.stats["restart_scanned_records"], 0, "bounded tail scanned")
        shadow_rec.set_recorder(restarted)
        _engine(_graph()).process_candidates([_opp([P1, P2, P3], [WETH, A, B, WETH])], 105)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["OPEN", "UPDATE"],
                         "re-detection continues the lifecycle — no duplicate OPEN")
        self.assertEqual(recs[1]["lifecycle"]["open_seq"], open_seq)
        self.assertEqual(recs[1]["lifecycle"]["continued_from_previous_session"], True)
        self.assertNotEqual(recs[1]["session"]["session_id"], session_a,
                            "records are segmented by process-start session")
        self.assertEqual(recs[1]["session"]["pid"], restarted._pid)

    def test_reorg_records_preserve_competing_block_hash_observations(self):
        """Competing block-hash observations for the same block number are all
        preserved append-only; replay uses the parent-hash evidence to mark
        candidates invalidated by reorg (no runtime reorg handling)."""
        engine = _engine(_graph())
        ctx_a = types.SimpleNamespace(block_hash="0x" + "11" * 32, number=100,
                                      touched_pools=frozenset())
        ctx_b = types.SimpleNamespace(block_hash="0x" + "22" * 32, number=100,
                                      touched_pools=frozenset())
        engine.process_candidates([_opp([P1, P2, P3], [WETH, A, B, WETH])], 100, block_context=ctx_a)
        engine.process_candidates([_opp([P2, P3], [A, B, A])], 100, block_context=ctx_b)
        recs = [r for r in self.records() if r.get("record_type") == "detection"]
        hashes = {r["detection"]["block_hash"] for r in recs}
        self.assertEqual(hashes, {"0x" + "11" * 32, "0x" + "22" * 32},
                         "both competing observations preserved")
        self.assertEqual({r["detection"]["block_number"] for r in recs}, {100})


class TestOfflineReplay(RecorderTestBase, _ReplayTestMixin):
    """§8: replay methodology, separate evaluation output, source immutability,
    gaps, beyond-window flagging."""

    @classmethod
    def setUpClass(cls):
        cls.replay = cls._load_script("shadow_replay")
        cls.agg = cls._load_script("shadow_aggregate")

    def _shadow_records(self):
        """A deterministic shadow log with four candidates covering the four
        replay outcome classes plus a beyond-window state stretch."""
        recs = []
        seq = [0]

        def det(fp, pools, route, block, state_block, reserves, hash=STATE_HASH, size=10**18,
                quote=1000):
            seq[0] += 1
            edges = [{"pool": p, "token_in": route[i], "token_out": route[i + 1],
                      "version": 2, "fee_bps": 30, "state_block": state_block,
                      "state_block_hash": hash,
                      "reserve_in": reserves[i][0], "reserve_out": reserves[i][1]}
                     for i, p in enumerate(pools)]
            recs.append({
                "record_type": "detection", "event": "OPEN", "schema_version": 1,
                "seq": seq[0], "session": {"session_id": "s1", "process_start_unix": 1.0, "pid": 1},
                "recorded_at_unix": 1.0, "fingerprint": fp,
                "identity": {"chain_id": 1, "origin": "block", "pool_addresses": pools},
                "detection": {"block_number": block, "block_hash": hash, "state_block": state_block},
                "market_inputs": {"edges": edges, "quote_method": None},
                "size_and_profit": {"trade_size_amount_in": size, "gross_quote_profit_wei": quote},
            })

        # A: profitable at the coherent later snapshot (block 150)
        det("a" * 16, [P1, P2, P3], [WETH, A, B, WETH], 100, 100,
            [(10**18, 2 * 10**18), (2 * 10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))])
        det("a" * 16, [P1, P2, P3], [WETH, A, B, WETH], 150, 150,
            [(2 * 10**18, 6 * 10**18), (4 * 10**18, 8 * 10**18), (4 * 10**18, int(3.8 * 10**18))])
        # B: no longer profitable at the later state (rate inverted; dedicated
        # pool so the later state is unambiguous)
        P6 = "0x" + "06" * 20
        det("b" * 16, [P6], [WETH, A], 100, 100, [(10**18, 2 * 10**18)])
        det("b" * 16, [P6], [WETH, A], 150, 150, [(10**18, 10**12)])
        # C: no later state anywhere (dedicated pools, never observed again)
        P4, P5 = "0x" + "04" * 20, "0x" + "05" * 20
        det("c" * 16, [P4, P5], [WETH, A, B], 100, 100,
            [(10**18, 2 * 10**18), (2 * 10**18, 2 * 10**18)])
        # E: later state exists but beyond the 1,000-block catch-up window
        det("e" * 16, [P2], [A, B], 100, 100, [(2 * 10**18, 2 * 10**18)])
        det("e" * 16, [P2], [A, B], 2000, 2000, [(2 * 10**18, 4 * 10**18)], size=10**18)
        # D: conflicting discovery-block hash observations at ITS OWN block 130
        # (reorg evidence affects candidates at that block, not other blocks)
        det("d" * 16, [P1, P2, P3], [WETH, A, B, WETH], 130, 130,
            [(10**18, 2 * 10**18), (2 * 10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))],
            hash="0x" + "77" * 32)
        det("d" * 16, [P1, P2, P3], [WETH, A, B, WETH], 130, 130,
            [(10**18, 2 * 10**18), (2 * 10**18, 2 * 10**18), (2 * 10**18, int(1.9 * 10**18))],
            hash=STATE_HASH)
        return recs

    def _write_source(self, path):
        with open(path, "w", encoding="utf-8") as f:
            for rec in self._shadow_records():
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def test_replay_outcomes_gaps_and_source_immutability(self):
        """The four outcome classes, the >1,000-block gap declaration and
        beyond-window flagging; the source log is byte-identical afterwards
        and the evaluation goes to a SEPARATE file."""
        cache_dir = Path(self.tmp) / "cache"
        cache_dir.mkdir()
        evaluation_path = Path(self.tmp) / "shadow_evaluation.jsonl"
        self._write_source(self.shadow_path)
        source_bytes_before = self.shadow_path.read_bytes()
        source_hash_before = hashlib.sha256(source_bytes_before).hexdigest()

        header = self.replay.evaluate(self.shadow_path, evaluation_path, str(cache_dir),
                                      chain_id=1, catchup_window=1000, rerun=False)
        by_fp = {}
        with open(evaluation_path, encoding="utf-8") as f:
            eval_records = [json.loads(l) for l in f if l.strip()]
        for r in eval_records:
            if r.get("record_type") == "shadow_evaluation":
                by_fp[r["fingerprint"]] = r

        self.assertEqual(by_fp["a" * 16]["outcome"], self.replay.OUTCOME_STILL)
        self.assertGreater(by_fp["a" * 16]["recomputed_gross_profit_wei"], 0)
        self.assertEqual(by_fp["a" * 16]["recompute_basis"], "constant_product_v2_adapter_math_offline")
        self.assertEqual(by_fp["b" * 16]["outcome"], self.replay.OUTCOME_GONE)
        self.assertLessEqual(by_fp["b" * 16]["recomputed_gross_profit_wei"], 0)
        self.assertEqual(by_fp["c" * 16]["outcome"], self.replay.OUTCOME_MISSING)
        self.assertIn("no later state", by_fp["c" * 16]["reason"])
        self.assertEqual(by_fp["d" * 16]["outcome"], self.replay.OUTCOME_REORG)
        self.assertTrue(by_fp["d" * 16]["reorg_evidence"])
        self.assertEqual(by_fp["e" * 16]["outcome"], self.replay.OUTCOME_STILL)
        self.assertTrue(by_fp["e" * 16]["beyond_catchup_window"],
                        "beyond-window state is flagged, never silently skipped")
        self.assertGreater(header["gap_count"], 0, "the 100→2000 stretch is a declared gap")
        self.assertTrue(any(g.get("kind") == "state_gap" and g.get("gap_blocks", 0) > 1000
                            for g in eval_records))
        # source immutability: byte-identical after replay
        self.assertEqual(hashlib.sha256(self.shadow_path.read_bytes()).hexdigest(), source_hash_before)
        self.assertNotEqual(evaluation_path, self.shadow_path)

    def test_replay_does_not_mutate_detection_records_after_full_run(self):
        """Detection-time records remain unchanged after a later replay —
        per-record content equality, not just a file hash."""
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        _engine(_graph()).process_candidates([opp], 100)
        before = self.records()
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        (Path(self.tmp) / "cache").mkdir()
        self.replay.evaluate(self.shadow_path, evaluation_path, str(Path(self.tmp) / "cache"),
                             chain_id=1, catchup_window=1000, rerun=True)
        self.assertEqual(self.records(), before, "source records unchanged after replay")
        with open(evaluation_path, encoding="utf-8") as f:
            kinds = {json.loads(l).get("record_type") for l in f if l.strip()}
        self.assertIn("shadow_evaluation_run", kinds)
        self.assertIn("shadow_evaluation", kinds)
        self.assertNotIn("detection", kinds, "evaluation output is separate from detection evidence")

    def test_replay_reports_unavailable_history(self):
        """Empty/missing history is declared as a gap, never skipped silently."""
        evaluation_path = Path(self.tmp) / "eval.jsonl"
        (Path(self.tmp) / "cache").mkdir()
        header = self.replay.evaluate(self.shadow_path, evaluation_path,
                                      str(Path(self.tmp) / "cache"), chain_id=1,
                                      catchup_window=1000, rerun=False)
        self.assertEqual(header["candidates_evaluated"], 0)
        with open(evaluation_path, encoding="utf-8") as f:
            gaps = [json.loads(l) for l in f if l.strip()
                    and json.loads(l).get("record_type") == "shadow_evaluation_gap"]
        self.assertTrue(any(g["kind"] == "empty_source" for g in gaps),
                        "empty source declared as an explicit gap")

    def test_observed_overlap_not_reported_as_confirmed_capture(self):
        """Aggregates label overlap closes as heuristic evidence and never as
        confirmed competitor capture (§6)."""
        tracker = OpportunityTracker(_NoopOppLogger())
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        tracker.process_block(200, [opp])
        tracker.process_block(201, [], filled_pools={P1.lower(): "0x" + "ff" * 32})
        agg = self.agg.aggregate(self.shadow_path, Path(self.tmp) / "eval.jsonl")
        self.assertEqual(agg["competition_evidence"]["observed_pool_overlap_closes"], 1)
        self.assertEqual(agg["competition_evidence"]["confirmed_competitor_captures"], 0)
        self.assertIn("NOT", agg["competition_evidence"]["claim"])
        self.assertIn("confirmed_competitor_capture", agg["unavailable_aggregates"])
        # the close record itself carries the same labelling
        close = [r for r in self.records() if r.get("record_type") == "lifecycle"][0]
        self.assertFalse(close["competition_evidence"]["confirmed_competitor_capture"])
        self.assertEqual(close["competition_evidence"]["basis"], "tracker_pool_overlap_heuristic")

    def test_aggregates_honest_missing_denominators(self):
        """Counts exist; the yield RATE is marked unavailable with the missing
        evidence source named (§7) — no fabricated denominators."""
        opp = _opp([P1, P2, P3], [WETH, A, B, WETH])
        _engine(_graph()).process_candidates([opp], 100)
        agg = self.agg.aggregate(self.shadow_path, Path(self.tmp) / "eval.jsonl")
        self.assertEqual(agg["detection_yield"]["counts_by_origin"], {"block": 1})
        rate = agg["detection_yield"]["detection_yield_rate"]
        self.assertEqual(rate["status"], "unavailable")
        self.assertIn("searched-universe", rate["missing_evidence_source"])
        self.assertIn("searched-universe denominator", agg["unavailable_aggregates"]["detection_yield_rate"])
        # CSV is written and rows are flattened triples
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
