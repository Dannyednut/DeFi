"""DSS-WO-MI-102B-R1A regression: scheduling fairness, failure integrity
and strengthened pair-check budget assertions.

Covers the R1A requirements:

- §1 P0 fairness: with a CONTINUOUS stream of new registrations during
  successive process() invocations (not a finite pre-existing backlog),
  ranked-universe seeds keep advancing, every registration is eventually
  drained and seeded exactly once, FIFO order is preserved and per-slice /
  per-invocation bounds hold.
- §2 P0 failure integrity: a failed scheduled cycle slice does not advance
  the work (seeds re-queued at the front, retried, batch not complete before
  success); a failed cross-protocol pair-check stage is retried and keeps
  the batch explicitly incomplete until it succeeds; persistent failures
  never produce a misleading complete=True.
- §4 P1 pair-check budget: actual all_edges_for_pair() CALL COUNT (not just
  distinct pairs) is <= 30 block-wide, every anchor pair is checked exactly
  once, and no pair is checked twice in either the event-free or the
  touched+scheduled scenario.
"""
import unittest

from bounded_discovery import BoundedDiscoveryConfig, RankedPairUniverse, normalize_pair
from detectors.protocol_opportunities import CrossProtocolDetector
from graph import TokenGraph
from tvl import init_prices

try:  # shared fixtures from the MI-102B regression module
    from tests.test_mi102b_bounded_discovery import (  # type: ignore
        WETH, USDC, A, P1, P2, P3,
        StubW3, _edge, add_v2_pool, build_profitable_graph,
        make_engine, patch_quote_path,
    )
except ImportError:  # pragma: no cover - direct (non-package) execution
    from test_mi102b_bounded_discovery import (
        WETH, USDC, A, P1, P2, P3,
        StubW3, _edge, add_v2_pool, build_profitable_graph,
        make_engine, patch_quote_path,
    )

from detectors.cycles import CycleDetector


def _addr(i: int) -> str:
    return f"0x{0xd000 + i:064x}"


def _token(i: int) -> str:
    return f"0x{0xe000 + i:064x}"


class SliceRecorder:
    """Records (block, seed) for every scheduled cycle-search slot booking
    and supports injected detector failures (scheduled and/or reactive)."""

    def __init__(self, engine):
        self.events = []           # (block, seed) per booked slot
        self.calls = 0             # successful scheduled invocations
        self.fail_next = 0         # fail this many SCHEDULED invocations
        self.failed_slices = []    # (block, seeds) of failed invocations
        self.fail_reactive = False  # fail EVERY reactive (touched) invocation
        self.reactive_calls = 0
        self.engine = engine
        self._original = engine.detectors[0].on_block
        engine.detectors[0].on_block = self._hook

    def _hook(self, block_number, *args, **kwargs):
        if kwargs.get("scheduled_seeds") is not None:
            if self.fail_next > 0:
                self.fail_next -= 1
                self.failed_slices.append((block_number, set(kwargs["scheduled_seeds"])))
                raise RuntimeError("injected cycle-detector failure")
            self.calls += 1
            self.events.extend((block_number, s) for s in kwargs["scheduled_seeds"])
            return self._original(block_number, *args, **kwargs)
        # reactive invocation (touched pools present)
        self.reactive_calls += 1
        if self.fail_reactive:
            raise RuntimeError("injected REACTIVE cycle-detector failure")
        return self._original(block_number, *args, **kwargs)

    @property
    def seeds(self):
        return [s for _, s in self.events]


class CrossRecorder:
    """Wraps the engine's cross_proto detector: records every invocation
    (reactive vs scheduled, anchors checked or not) and supports injected
    failures for either path."""

    def __init__(self, engine):
        self.invocations = []      # (block, check_anchors) — successful only
        self.scheduled_calls = []  # (block, check_anchors) for scheduled calls only
        self.failures = 0
        self.fail_next = 0          # fail this many SCHEDULED invocations
        self.fail_reactive = False  # fail EVERY reactive invocation
        self.engine = engine
        self._original = engine.detectors[1].on_block
        engine.detectors[1].on_block = self._hook

    def _hook(self, block_number, *args, **kwargs):
        scheduled = "extra_pairs" in kwargs
        anchors = kwargs.get("check_anchors", True)
        if scheduled:
            self.scheduled_calls.append((block_number, anchors))
        if (scheduled and self.fail_next > 0) or (not scheduled and self.fail_reactive):
            if scheduled:
                self.fail_next -= 1
            self.failures += 1
            raise RuntimeError("injected cross-protocol failure")
        self.invocations.append((block_number, anchors))
        return self._original(block_number, *args, **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — Scheduling fairness under sustained arrivals
# ═════════════════════════════════════════════════════════════════════════════

class TestSustainedArrivalFairness(unittest.TestCase):
    def setUp(self):
        # Steady-state fixture: the ranked universe holds seeds that are NOT
        # part of the arrival stream, and the registration backlog is drained
        # so the batch's main seed list is PURE ranked-universe selection
        # (R1A1 §1: batch["seeds"] must not be mistaken for the universe —
        # it also contains registrations consumed at batch start).
        self.graph = build_profitable_graph()
        init_prices(2000.0, WETH, [USDC])
        self.extra_universe_pools = []
        for i in range(6):
            pool, tok = _addr(900 + i), _token(900 + i)
            add_v2_pool(self.graph, pool, WETH, tok, 1.0)
            self.extra_universe_pools.append(pool)
        self.graph.recently_registered.drain()  # steady state: backlog empty

    def test_ranked_universe_progresses_under_sustained_arrivals(self):
        """Registrations arrive EVERY invocation while a batch is active —
        never a finite pre-existing backlog. The alternation rule must keep
        RANKED-UNIVERSE seeds advancing (identified from the universe's own
        selection, independently of the newly registered pool addresses);
        once arrivals stop, every registration must have been seeded exactly
        once (FIFO), with the per-slice and per-pass bounds respected
        throughout. This test FAILS if registrations monopolize all
        scheduled slices while ranked-universe seeds remain pending."""
        with patch_quote_path():
            engine = make_engine(self.graph)
            rec = SliceRecorder(engine)
            engine.process(100, touched_pools=frozenset())  # batch starts
            batch = engine._scheduled_batch
            self.assertIsNotNone(batch)
            # R1A1 §1: ranked-universe seeds come from the universe's OWN
            # selection — never from the batch seed list, which can mix in
            # registrations consumed at batch start. The backlog was drained
            # before engine creation, so the batch list must be a subset of
            # the ranked selection and contain no stream address.
            universe_seeds = list(engine.pair_universe.seed_pools())
            self.assertTrue(universe_seeds, "fixture must provide ranked-universe seeds")
            self.assertTrue(set(batch["seeds"]) <= set(universe_seeds),
                            "batch main list is pure ranked-universe selection at start")
            pure_universe = set(universe_seeds) - set(stream := [_addr(i) for i in range(60)])
            self.assertTrue(pure_universe,
                            "ranked-universe seeds must be independent of the arrival stream")

            # SUSTAINED arrivals: one new searchable pool per invocation,
            # 60 invocations in a row while the batch is active.
            for i, addr in enumerate(stream):
                add_v2_pool(self.graph, addr, _token(i), WETH, 2.0)
                engine.process(101 + i, touched_pools=frozenset())
            mid = engine.last_scheduled_batch_info
            self.assertFalse(mid["complete"], "stream ongoing: batch still active")

            # UNIVERSE PROGRESS DURING THE STREAM: at least one actual
            # ranked-universe seed searched while arrivals continue, and the
            # cursor kept advancing (more than a one-off) — this is the
            # assertion that fails under registration monopolization.
            during_stream = [(b, s) for b, s in rec.events if b >= 101]
            universe_progress = [s for _, s in during_stream if s in pure_universe]
            self.assertGreaterEqual(
                len(set(universe_progress)), 2,
                "ranked-universe seeds must CONTINUE advancing while registrations "
                f"keep arriving (got {sorted(set(universe_progress))})")
            # and the arrivals were seeded too (priority retained)
            self.assertTrue({s for _, s in during_stream} & set(stream),
                            "simultaneous priority progress expected under alternation")
            # per-slice bound held throughout the stream
            from collections import Counter
            per_block = Counter(b for b, _ in rec.events)
            self.assertTrue(all(n <= 1 for n in per_block.values()),
                            f"per-invocation slice bound respected during the stream: {per_block}")

            # arrivals stop: batch must complete and every registration must
            # have been seeded exactly once, in FIFO order.
            block = 101 + len(stream)
            while engine._scheduled_batch is not None:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info

        searched_all = set(rec.seeds)
        self.assertTrue(set(stream) <= searched_all,
                        "every sustained registration eventually seeded")
        counts = Counter(rec.seeds)
        dups = [s for s in stream if counts[s] != 1]
        self.assertEqual(dups, [], "every registration seeded EXACTLY once")
        # every ranked-universe seed was also searched exactly once
        universe_dups = [s for s in pure_universe if counts[s] != 1]
        self.assertEqual(universe_dups, [], "every ranked-universe seed searched exactly once")
        # FIFO: the stream's first-half seeds appear before its second half
        order = {s: i for i, (_, s) in enumerate(rec.events)}
        first_half = [order.get(s) for s in stream[: len(stream) // 2] if s in order]
        second_half = [order.get(s) for s in stream[len(stream) // 2:] if s in order]
        self.assertLess(max(first_half), min(second_half),
                        "registration FIFO order preserved")
        self.assertTrue(done["complete"])
        self.assertEqual(done["failed"], False)
        self.assertEqual(done["seeds_processed"] + done["seeds_skipped_reactive_overlap"]
                         + done["seeds_deduped"], done["seeds_total"])

    def test_fifo_order_of_registration_stream(self):
        """Shorter stream, explicit FIFO check: registration seeds are
        searched in the exact order they were registered (per-slice cap
        may interleave universe seeds between them, never among them)."""
        with patch_quote_path():
            engine = make_engine(self.graph)
            rec = SliceRecorder(engine)
            engine.process(100, touched_pools=frozenset())
            stream = [_addr(100 + i) for i in range(10)]
            for i, addr in enumerate(stream):
                add_v2_pool(self.graph, addr, _token(100 + i), WETH, 2.0)
                engine.process(101 + i, touched_pools=frozenset())
            block = 111
            while engine._scheduled_batch is not None:
                engine.process(block, touched_pools=frozenset())
                block += 1
        positions = [i for i, (_, s) in enumerate(rec.events) if s in set(stream)]
        got = [s for _, s in rec.events if s in set(stream)]
        self.assertEqual(got, stream, "registrations searched in FIFO order")
        self.assertEqual(len(positions), len(stream))


# ═════════════════════════════════════════════════════════════════════════════
# §2 — Failure integrity
# ═══════════════════════════════════════════════════════════════════════════

class TestCycleSliceFailureIntegrity(unittest.TestCase):
    def setUp(self):
        self.graph = build_profitable_graph()

    def test_failed_slice_is_retried_and_not_marked_processed(self):
        """Cycle detector fails ONCE, then succeeds: the affected seed is
        re-queued and retried, never counted as processed before success,
        and the batch is not complete before the retry succeeds."""
        with patch_quote_path():
            engine = make_engine(self.graph)
            rec = SliceRecorder(engine)
            engine.process(100, touched_pools=frozenset())  # slice 1 succeeds
            first_seeds = list(rec.seeds)
            # inject one failure: the NEXT slice raises
            rec.fail_next = 1
            engine.process(101, touched_pools=frozenset())  # slice fails
            self.assertEqual(rec.failed_slices and len(rec.failed_slices[0][1]), 1)
            failed_seed = next(iter(rec.failed_slices[0][1]))
            info = engine.last_scheduled_batch_info
            self.assertEqual(info["failed"], False, "retry budget not exhausted: not failed")
            # the failed seed was NOT marked processed (retried instead)
            self.assertNotIn(failed_seed, first_seeds)
            block = 102
            while engine._scheduled_batch is not None:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info
        # the failed seed WAS retried and searched exactly once in the end
        self.assertIn(failed_seed, rec.seeds)
        self.assertEqual(rec.seeds.count(failed_seed), 1, "retried seed searched exactly once")
        self.assertEqual(rec.calls, 3 + 1 - 1 + 1 - 1, "only the injected failure raised")
        self.assertTrue(done["complete"])
        self.assertFalse(done["failed"])
        self.assertEqual(done["failed_seeds"], 0)

    def test_persistent_cycle_failure_never_reports_complete(self):
        """The cycle detector fails on EVERY invocation: retries are bounded,
        the seeds become failed_seeds, and the batch summary must NOT claim
        successful completion (complete stays False / failed=True)."""
        with patch_quote_path():
            engine = make_engine(self.graph)
            rec = SliceRecorder(engine)
            rec.fail_next = 10**6  # persistent failure
            engine.process(100, touched_pools=frozenset())
            block = 101
            while engine._scheduled_batch is not None and block < 130:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info
        self.assertFalse(done.get("complete", True) or done["failed"] is False,
                         "persistent failure must not produce a plain complete=True")
        self.assertTrue(done["failed"], "terminal batch state is explicitly failed")
        self.assertGreater(done["failed_seeds"], 0)
        self.assertEqual(done["seeds_processed"], 0, "no failed slice counted as processed")


class TestCrossProtocolFailureIntegrity(unittest.TestCase):
    def setUp(self):
        init_prices(2000.0, WETH, [USDC])
        self.graph = build_profitable_graph()

    def _make_engine(self):
        engine = make_engine(
            self.graph,
            detectors=[CycleDetector(StubW3(), self.graph, None, None, 1),
                       CrossProtocolDetector(StubW3(), self.graph, None, None, 1)],
        )
        return engine

    def test_pair_check_failure_retried_and_then_batch_completes(self):
        """CrossProtocol fails ONCE at batch start, then succeeds: the
        pair-check stage is retried on the next invocation, the batch is
        explicitly incomplete until the retry succeeds, then completes."""
        with patch_quote_path():
            engine = self._make_engine()
            rec = CrossRecorder(engine)
            rec.fail_next = 1
            engine.process(100, touched_pools=frozenset())
            info = engine.last_scheduled_batch_info
            self.assertFalse(info["pair_checks_ok"], "failed stage recorded as not ok")
            self.assertFalse(info["complete"], "batch not complete while pair checks failed")
            # next invocation retries the pair checks (and continues the batch)
            engine.process(101, touched_pools=frozenset())
            info2 = engine.last_scheduled_batch_info
            self.assertTrue(info2.get("pair_checks_ok"), "retry succeeded")
            self.assertEqual((rec.failures, len(rec.invocations)), (1, 1),
                             "one failed invocation + one successful retry")
            block = 102
            while engine._scheduled_batch is not None:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info
        self.assertTrue(done["complete"])
        self.assertFalse(done["failed"])
        self.assertTrue(done["pair_checks_ok"])

    def test_persistent_cross_failure_never_reports_complete(self):
        """CrossProtocol fails on EVERY invocation: the batch drains its
        seeds but must still be reported as failed (pair_checks_ok False) —
        never a misleading complete=True."""
        with patch_quote_path():
            engine = self._make_engine()
            rec = CrossRecorder(engine)
            rec.fail_next = 10**6
            engine.process(100, touched_pools=frozenset())
            block = 101
            while engine._scheduled_batch is not None and block < 140:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info
        self.assertFalse(done["pair_checks_ok"], "persistent pair-check failure visible")
        self.assertTrue(done["failed"], "terminal state explicitly failed")
        self.assertFalse(done["complete"], "not reported as successfully complete")


# ═════════════════════════════════════════════════════════════════════════════
# R1A1 §3 — reactive detector success is not assumed
# ═════════════════════════════════════════════════════════════════════════════

class TestReactiveFailureHandoff(unittest.TestCase):
    def setUp(self):
        init_prices(2000.0, WETH, [USDC])
        self.graph = build_profitable_graph()

    def _both_detector_engine(self):
        return make_engine(
            self.graph,
            detectors=[CycleDetector(StubW3(), self.graph, None, None, 1),
                       CrossProtocolDetector(StubW3(), self.graph, None, None, 1)],
        )

    def test_reactive_cycle_failure_preserves_scheduled_eligibility(self):
        """R1A1 §3.1: a REACTIVE CycleDetector failure on a block with
        touched pools and a newly registered pool must not let the scheduled
        path silently discard that pool. Under the old behavior the pool
        (present in touched_pools) was excluded from the scheduled slice
        solely because it appeared touched — consumed from the backlog and
        never searched."""
        with patch_quote_path():
            engine = make_engine(self.graph)
            rec = SliceRecorder(engine)
            engine.process(100, touched_pools=frozenset())  # batch starts
            rec.fail_reactive = True  # every reactive cycle invocation fails
            new_pool = _addr(500)
            add_v2_pool(self.graph, new_pool, _token(500), WETH, 2.0)
            engine.process(101, touched_pools=frozenset({new_pool}))
            live = engine.last_scheduled_batch_info
            # the newly registered pool was NOT silently consumed+discarded:
            # the scheduled slice searched it despite the reactive failure
            self.assertIn(new_pool, rec.seeds,
                          "touched pool must stay scheduled-eligible when the "
                          "reactive cycle invocation failed")
            self.assertEqual(rec.seeds.count(new_pool), 1, "searched exactly once")
            block = 102
            while engine._scheduled_batch is not None:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info
        self.assertTrue(done["complete"])
        self.assertFalse(done["failed"])
        self.assertEqual(done["seeds_skipped_reactive_overlap"], 0,
                         "nothing may be skipped as reactive-overlap when the "
                         "reactive cycle invocation failed")
        self.assertEqual(done["seeds_processed"] + done["seeds_skipped_reactive_overlap"]
                         + done["seeds_deduped"], done["seeds_total"])

    def test_reactive_cross_failure_completes_anchor_stage(self):
        """R1A1 §3.2 / R1A2 §2: a REACTIVE CrossProtocol failure on a block
        that also starts a scheduled batch must not silently skip the
        anchor-pair checks — AND the scheduled fallback must not repeat the
        anchor stage inside the same process() invocation (R1A2 block
        budget). The stage stays explicitly pending; a subsequent invocation
        completes the missing anchor + supplementary stages, and pair-check
        success is reported only after those stages actually succeeded."""
        from bounded_discovery import anchor_pairs
        with patch_quote_path():
            engine = self._both_detector_engine()
            rec = CrossRecorder(engine)
            rec.fail_reactive = True        # reactive anchor stage fails (before any lookup)
            counter = _PairCountingGraph(self.graph)
            engine.detectors[1].graph = counter
            engine.process(100, touched_pools=frozenset({P1}))
            live = engine.last_scheduled_batch_info
            self.assertFalse(live["pair_checks_ok"],
                             "anchors were not checked successfully — pair checks not ok")
            self.assertFalse(live["complete"])
            self.assertFalse(live["anchor_stage_covered"])
            # R1A2 §2: NO scheduled cross invocation in the same process()
            # call — the anchor stage is not repeated immediately
            self.assertEqual(rec.scheduled_calls, [],
                             "the scheduled fallback must not repeat the anchor stage "
                             "in the same invocation after a reactive failure")
            self.assertEqual(counter.calls, [],
                             "reactive failure happened before any pair lookup")
            # next invocation (reactive not invoked): retry completes the
            # missing anchor + supplementary stages in one bounded call
            calls_before = len(counter.calls)
            engine.process(101, touched_pools=frozenset())
            self.assertEqual(rec.scheduled_calls, [(101, True)],
                             "retry completes the missing anchor stage "
                             "(check_anchors=True) once, on the next invocation")
            block_calls = len(counter.calls) - calls_before
            self.assertLessEqual(block_calls, 30, "retry stays within the block budget")
            live2 = engine.last_scheduled_batch_info
            self.assertTrue(live2["pair_checks_ok"], "retry covered anchors + supplementary")
            self.assertTrue(live2["anchor_stage_covered"])
            block = 102
            while engine._scheduled_batch is not None:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info
        # every anchor pair was actually checked (by the successful retry)
        checked = set(counter.calls)
        for a in anchor_pairs(1):
            self.assertIn(a, checked, f"anchor pair {a} must be checked before "
                                      "the batch may report pair checks complete")
        self.assertTrue(done["complete"])
        self.assertTrue(done["pair_checks_ok"])
        self.assertTrue(done["anchor_stage_covered"],
                        "anchor stage recorded as covered before completion")

    def test_partial_reactive_failure_within_block_budget(self):
        """R1A2 §3: the reactive detector checks TWO anchor pairs, then
        raises an injected exception before finishing the anchor list, and
        the scheduled discovery stage runs in the SAME process() invocation.
        The combined actual pair-call count of that invocation stays within
        the 30-pair budget, the scheduled fallback does not repeat the full
        anchor list immediately, pair_checks_ok stays false while the stage
        is pending, and a subsequent invocation completes the missing anchor
        and supplementary stages before success is reported."""
        from bounded_discovery import anchor_pairs
        with patch_quote_path():
            engine = self._both_detector_engine()
            rec = CrossRecorder(engine)
            counter = _FailingPairGraph(self.graph)
            counter.fail_after = 2  # two successful anchor lookups, then raise
            engine.detectors[1].graph = counter

            calls_before = len(counter.calls)
            engine.process(100, touched_pools=frozenset({P1}))  # batch start
            attempted = len(counter.calls) - calls_before
            live = engine.last_scheduled_batch_info
            # R1A3 §3: the reactive invocation issued THREE pair-lookup
            # invocations — two completed successfully, the third raised
            # mid-anchor-list. Budget assertions count ATTEMPTS.
            self.assertEqual(attempted, 3,
                             "two successful anchor lookups + one failing invocation "
                             "= three attempted all_edges_for_pair invocations")
            self.assertEqual(len(counter.completed), 2,
                             "exactly two lookups completed successfully")
            checked = set(counter.calls)
            self.assertTrue(checked <= set(anchor_pairs(1)),
                            "the partial progress was inside the anchor list")
            # combined block budget: reactive partial + deferred scheduled = 3 <= 30
            self.assertLessEqual(attempted, 30,
                                 "combined attempted pair calls in the invocation within budget")
            # the scheduled fallback did NOT repeat the full anchor list
            self.assertEqual(rec.scheduled_calls, [],
                             "no scheduled anchor repetition after the partial reactive failure")
            # the stage stays explicitly pending and visible
            self.assertFalse(live["pair_checks_ok"], "pair checks pending after partial failure")
            self.assertFalse(live["complete"])
            self.assertFalse(live["anchor_stage_covered"])

            # subsequent invocation (reactive not invoked): completes the
            # missing anchor + supplementary stages. The injected partial
            # failure applied only to the reactive invocation of block 100.
            counter.fail_after = None
            calls_before = len(counter.calls)
            engine.process(101, touched_pools=frozenset())
            retry_calls = len(counter.calls) - calls_before
            self.assertLessEqual(retry_calls, 30, "retry invocation within budget")
            self.assertEqual(rec.scheduled_calls, [(101, True)],
                             "one scheduled retry completes anchors + supplementary")
            live2 = engine.last_scheduled_batch_info
            self.assertTrue(live2["pair_checks_ok"],
                            "pair-check success only after the stages actually succeeded")
            self.assertTrue(live2["anchor_stage_covered"])
            block = 102
            while engine._scheduled_batch is not None:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info
        # every anchor pair was checked across the two invocations
        checked = set(counter.calls)
        for a in anchor_pairs(1):
            self.assertIn(a, checked, f"anchor pair {a} must be checked before completion")
        self.assertTrue(done["complete"])
        self.assertTrue(done["pair_checks_ok"])
        self.assertTrue(done["anchor_stage_covered"])

    def test_reactive_success_retains_suppression_and_budget(self):
        """R1A1 §3.3: when the reactive cycle invocation SUCCEEDS, the
        existing duplicate-work suppression stays in effect (a touched pool
        is not scheduled-searched again) and the block-wide pair-check
        budget stays within 30."""
        with patch_quote_path():
            engine = self._both_detector_engine()
            rec = SliceRecorder(engine)
            counter = _PairCountingGraph(self.graph)
            engine.detectors[1].graph = counter
            engine.process(100, touched_pools=frozenset())
            calls_after_start = len(counter.calls)
            self.assertLessEqual(calls_after_start, 30, "batch-start pair budget")
            touched_pool = _addr(600)
            add_v2_pool(self.graph, touched_pool, _token(600), WETH, 2.0)
            engine.process(101, touched_pools=frozenset({touched_pool}))
            block_calls = len(counter.calls) - calls_after_start
            self.assertLessEqual(block_calls, 30, "block-wide pair-check budget on success")
            self.assertNotIn(touched_pool, rec.seeds,
                             "successfully reactively-scanned pool is not scheduled-searched again")
            block = 102
            while engine._scheduled_batch is not None:
                engine.process(block, touched_pools=frozenset())
                block += 1
            done = engine.last_scheduled_batch_info
        self.assertGreaterEqual(done["seeds_skipped_reactive_overlap"], 1,
                                "the touched pool was suppressed as reactive overlap")
        self.assertTrue(done["complete"])
        self.assertEqual(done["seeds_processed"] + done["seeds_skipped_reactive_overlap"]
                         + done["seeds_deduped"], done["seeds_total"])


# ═════════════════════════════════════════════════════════════════════════════
# R1A3 §2 — seed exhaustion: a pending pair-check stage survives and recovers
# ═════════════════════════════════════════════════════════════════════════════

class TestPairStageSeedExhaustion(unittest.TestCase):
    """R1A3 §2: a pending pair-check stage must NOT be discarded when the
    cycle seeds run out — the batch stays active for the bounded recovery
    policy (three unresolved invocations, then a visible terminal failure),
    and the next safe invocation completes the missing anchor and
    supplementary stages under the R1A2 sequencing rules."""

    def _graph_with_one_searchable_pool(self):
        """One searchable pool (the only possible scheduled seed) plus one
        below the searchability gate (usable as a touched pool without
        adding universe seeds)."""
        from tvl import set_graph_ref
        g = TokenGraph(chain_id=1)
        init_prices(2000.0, WETH, [USDC])
        add_v2_pool(g, P1, WETH, A, 2.0)                            # searchable seed
        add_v2_pool(g, _addr(901), _token(9), _token(10), 2.0, tvl=100.0)  # below the gate
        set_graph_ref(g)
        return g

    def _graph_with_no_searchable_pools(self):
        """Every pool below the searchability TVL gate — the ranked universe
        and the registration queue can provide ZERO cycle-search seeds."""
        from tvl import set_graph_ref
        g = TokenGraph(chain_id=1)
        init_prices(2000.0, WETH, [USDC])
        add_v2_pool(g, P1, WETH, A, 2.0, tvl=100.0)
        add_v2_pool(g, P2, A, _token(11), 2.0, tvl=100.0)
        set_graph_ref(g)
        return g

    def _engine(self, g):
        return make_engine(
            g, detectors=[CycleDetector(StubW3(), g, None, None, 1),
                          CrossProtocolDetector(StubW3(), g, None, None, 1)])

    def test_zero_seed_batch_survives_pending_pair_stage(self):
        """R1A3 §2.1: zero cycle-search seeds (universe and registration
        queue both provide nothing) + reactive CrossProtocol failure at
        batch start. The batch remains active with pair_checks_ok=False,
        then recovers on the next safe invocation and completes only after
        anchors AND supplementary pairs are checked."""
        from bounded_discovery import anchor_pairs
        g = self._graph_with_no_searchable_pools()
        with patch_quote_path():
            engine = self._engine(g)
            rec = CrossRecorder(engine)
            rec.fail_reactive = True
            counter = _PairCountingGraph(g)
            engine.detectors[1].graph = counter
            engine.process(100, touched_pools=frozenset({P1}))
            live = engine.last_scheduled_batch_info
            self.assertEqual(live["seeds_total"], 0, "universe + queue provided zero seeds")
            self.assertEqual(live["main_pending"], 0, "no cycle queue to exhaust")
            self.assertFalse(live["complete"])
            self.assertFalse(live["pair_checks_ok"])
            self.assertFalse(live["anchor_stage_covered"])
            self.assertEqual(live["pair_stage_pending_invocations"], 1,
                             "the batch-start invocation is the first unresolved one")
            self.assertEqual(live["pair_stage_deferrals"], 1,
                             "the reactive failure deferred the scheduled stage")
            self.assertIsNotNone(engine._scheduled_batch,
                                 "seed exhaustion must not discard the pending pair stage")
            self.assertEqual(rec.scheduled_calls, [],
                             "no scheduled anchor repetition in the failing block")
            rec.fail_reactive = False
            engine.process(101, touched_pools=frozenset())
            live2 = engine.last_scheduled_batch_info
            self.assertTrue(live2["complete"], "the recovery invocation completed the batch")
            self.assertTrue(live2["pair_checks_ok"])
            self.assertTrue(live2["anchor_stage_covered"])
            self.assertEqual(rec.scheduled_calls, [(101, True)],
                             "the next safe invocation checks anchors + supplementary")
            self.assertEqual(live2["slices"], 0, "no cycle-search work existed")
        checked = set(counter.calls)
        for a in anchor_pairs(1):
            self.assertIn(a, checked, f"anchor pair {a} must be checked before completion")

    def test_single_seed_exhaustion_preserves_pending_pair_stage(self):
        """R1A3 §2.2: exactly ONE scheduled seed with slice size one; the
        reactive CrossProtocol failure defers pair checks in the SAME
        invocation that exhausts the only seed. Exhausting the seed must
        not discard the pending stage; the next safe invocation recovers it."""
        from bounded_discovery import anchor_pairs
        g = self._graph_with_one_searchable_pool()
        touched_gate = _addr(901)
        with patch_quote_path():
            engine = self._engine(g)
            rec = CrossRecorder(engine)
            rec.fail_reactive = True
            counter = _PairCountingGraph(g)
            engine.detectors[1].graph = counter
            engine.process(100, touched_pools=frozenset({touched_gate}))
            live = engine.last_scheduled_batch_info
            self.assertEqual(live["seeds_total"], 1, "exactly one scheduled seed")
            self.assertEqual(live["seeds_processed"], 1,
                             "the only seed was searched in the batch-start invocation")
            self.assertEqual(live["main_pending"], 0, "cycle queue exhausted")
            self.assertFalse(live["pair_checks_ok"])
            self.assertEqual(live["pair_stage_pending_invocations"], 1)
            self.assertEqual(live["pair_stage_deferrals"], 1)
            self.assertIsNotNone(engine._scheduled_batch,
                                 "exhausting the only seed must not discard the pending stage")
            self.assertEqual(rec.scheduled_calls, [],
                             "no scheduled anchor repetition in the failing block")
            rec.fail_reactive = False
            engine.process(101, touched_pools=frozenset())
            live2 = engine.last_scheduled_batch_info
            self.assertTrue(live2["complete"])
            self.assertTrue(live2["pair_checks_ok"])
            self.assertTrue(live2["anchor_stage_covered"])
            self.assertEqual(rec.scheduled_calls, [(101, True)])
        checked = set(counter.calls)
        for a in anchor_pairs(1):
            self.assertIn(a, checked, f"anchor pair {a} must be checked before completion")

    def test_scheduled_pair_failure_on_final_slice_is_retained(self):
        """R1A3 §2.3: the scheduled CrossProtocol call fails on the
        invocation where the cycle queue is exhausted. The pending stage is
        retained for its bounded retry opportunity — the batch is NOT
        cleared — and a later invocation completes it."""
        from bounded_discovery import anchor_pairs
        g = self._graph_with_one_searchable_pool()
        touched_gate = _addr(901)
        with patch_quote_path():
            engine = self._engine(g)
            rec = CrossRecorder(engine)
            rec.fail_reactive = True
            counter = _PairCountingGraph(g)
            engine.detectors[1].graph = counter
            engine.process(100, touched_pools=frozenset({touched_gate}))  # defer + exhaust
            self.assertIsNotNone(engine._scheduled_batch)
            rec.fail_reactive = False
            rec.fail_next = 1
            engine.process(101, touched_pools=frozenset())                # scheduled retry fails
            live = engine.last_scheduled_batch_info
            self.assertFalse(live["complete"])
            self.assertFalse(live["pair_checks_ok"])
            self.assertEqual(live["pair_checks_attempted"], 1,
                             "exactly one scheduled pair-check attempt was issued")
            self.assertEqual(live["pair_stage_pending_invocations"], 2)
            self.assertEqual(live["pair_stage_deferrals"], 1)
            self.assertIsNotNone(engine._scheduled_batch,
                                 "a scheduled failure on the final slice must retain the "
                                 "pending stage for its bounded retry, not clear the batch")
            self.assertEqual(rec.scheduled_calls, [(101, True)])
            engine.process(102, touched_pools=frozenset())                # recovery
            live2 = engine.last_scheduled_batch_info
            self.assertTrue(live2["complete"])
            self.assertTrue(live2["pair_checks_ok"])
            self.assertTrue(live2["anchor_stage_covered"])
            self.assertEqual(rec.scheduled_calls, [(101, True), (102, True)])
        checked = set(counter.calls)
        for a in anchor_pairs(1):
            self.assertIn(a, checked, f"anchor pair {a} must be checked before completion")

    def test_persistent_pair_stage_failure_terminates_within_bound(self):
        """R1A3 §2.4: repeated reactive deferrals AND scheduled pair-check
        failures must respect the explicit three-unresolved-invocation
        bound (including the invocation that first leaves the stage
        pending): the batch reaches a terminal failed state, never reports
        successful completion and does not stay active indefinitely."""
        g = self._graph_with_one_searchable_pool()
        touched_gate = _addr(901)
        with patch_quote_path():
            engine = self._engine(g)
            rec = CrossRecorder(engine)
            counter = _PairCountingGraph(g)
            engine.detectors[1].graph = counter
            # invocation 1: reactive failure defers the stage at batch start
            rec.fail_reactive = True
            engine.process(100, touched_pools=frozenset({touched_gate}))
            self.assertEqual(engine.last_scheduled_batch_info["pair_stage_pending_invocations"], 1)
            # invocation 2: the scheduled recovery attempt fails
            rec.fail_reactive = False
            rec.fail_next = 10 ** 6
            engine.process(101, touched_pools=frozenset())
            self.assertEqual(engine.last_scheduled_batch_info["pair_stage_pending_invocations"], 2)
            self.assertIsNotNone(engine._scheduled_batch)
            # invocation 3: another reactive deferral — the bound is reached
            rec.fail_next = 0
            rec.fail_reactive = True
            engine.process(102, touched_pools=frozenset({touched_gate}))
            done = engine.last_scheduled_batch_info
            self.assertIsNone(engine._scheduled_batch, "the bound terminates the batch")
            self.assertTrue(done["failed"])
            self.assertFalse(done["complete"])
            self.assertFalse(done["pair_checks_ok"])
            self.assertEqual(done["pair_stage_pending_invocations"], 3,
                             "exactly three unresolved invocations, then termination")
            self.assertEqual(done["pair_stage_deferrals"], 2)
            self.assertEqual(done["seeds_processed"], 1)
            # a later invocation cannot resurrect it or report success
            engine.process(103, touched_pools=frozenset())
            done2 = engine.last_scheduled_batch_info
            self.assertFalse(done2["complete"])
            self.assertTrue(done2["failed"])
            self.assertFalse(done2["pair_checks_ok"])


# ═════════════════════════════════════════════════════════════════════════════
# R1A1 §4 — failure visibility: addresses, truncation indicator, full summary
# ═════════════════════════════════════════════════════════════════════════════

class TestFailureVisibility(unittest.TestCase):
    def test_failed_seed_addresses_visible_and_fully_accounted(self):
        """Failed-seed ADDRESSES appear in the live batch status (bounded
        display with an explicit truncation indicator), and the final
        summary accounts for EVERY failed seed with the full list."""
        graph = build_profitable_graph()
        for i in range(40):
            add_v2_pool(graph, _addr(700 + i), _token(700 + i), WETH, 2.0)
        with patch_quote_path():
            engine = make_engine(graph)
            rec = SliceRecorder(engine)
            rec.fail_next = 10**6  # every scheduled slice fails, persistently
            engine.process(100, touched_pools=frozenset())
            saw_truncated = False
            saw_addresses = False
            block = 101
            while engine._scheduled_batch is not None and block < 800:
                engine.process(block, touched_pools=frozenset())
                if engine._scheduled_batch is None:
                    break  # went terminal this invocation — summary checked below
                live = engine.last_scheduled_batch_info
                if live["failed_seeds"] > 32:
                    self.assertTrue(live["failed_seed_list_truncated"],
                                    "bounded display must flag truncation explicitly")
                    self.assertEqual(len(live["failed_seed_list"]), 32)
                    saw_truncated = True
                if live["failed_seeds"] > 0:
                    self.assertTrue(live["failed_seed_list"],
                                    "live status must carry failed-seed addresses")
                    saw_addresses = True
                block += 1
            done = engine.last_scheduled_batch_info
        self.assertTrue(saw_addresses, "failed-seed addresses observed in live status")
        self.assertTrue(saw_truncated, "truncation indicator observed in live status")
        self.assertTrue(done["failed"], "terminal failed state preserved")
        self.assertFalse(done["complete"])
        self.assertFalse(done["failed_seed_list_truncated"],
                         "final summary list is never truncated")
        self.assertEqual(done["failed_seeds"], len(done["failed_seed_list"]),
                         "final summary accounts for EVERY failed seed")
        failed_observed = {s for _, seeds in rec.failed_slices for s in seeds}
        self.assertEqual(set(done["failed_seed_list"]), failed_observed,
                         "full failure list matches the actually failed seeds")


# ═════════════════════════════════════════════════════════════════════════════
# §4 — Strengthened pair-check budget assertions
# ═════════════════════════════════════════════════════════════════════════════

class _PairCountingGraph:
    """Counts EVERY all_edges_for_pair call (not just distinct pairs)."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def all_edges_for_pair(self, a, b):
        self.calls.append(normalize_pair(a, b))
        return self._inner.all_edges_for_pair(a, b)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FailingPairGraph(_PairCountingGraph):
    """_PairCountingGraph with injectable PARTIAL failure: when
    ``fail_after`` is set to N, the first N all_edges_for_pair invocations
    succeed and every later invocation raises — simulating a reactive
    detector that made real pair-check progress and then failed
    mid-anchor-list.

    R1A3 §3: EVERY invocation is recorded as an ATTEMPT in ``calls`` BEFORE
    delegating or raising — the failing invocation is a real pair-lookup
    invocation the detector issued, so per-block budget assertions count
    attempts, not just successful returns. Successful delegated completions
    are additionally recorded in ``completed``, keeping the
    attempted-versus-completed distinction available."""

    def __init__(self, inner):
        super().__init__(inner)
        self.fail_after = None   # raise on the (N+1)-th invocation
        self.completed = []      # successful delegated lookups only

    def all_edges_for_pair(self, a, b):
        self.calls.append(normalize_pair(a, b))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("injected partial pair-check failure")
        result = self._inner.all_edges_for_pair(a, b)
        self.completed.append(self.calls[-1])
        return result


class TestPairCheckCallBudget(unittest.TestCase):
    def setUp(self):
        # fixture with several anchor and ranked pairs
        self.graph = build_profitable_graph()
        add_v2_pool(self.graph, P2, WETH, USDC, 1.0 / 2000.0)
        add_v2_pool(self.graph, P3, WETH, USDC, 1.0 / 1900.0)
        init_prices(2000.0, WETH, [USDC])

    def _engine_with_counter(self, **kw):
        from bounded_discovery import anchor_pairs
        engine = make_engine(
            self.graph,
            detectors=[CycleDetector(StubW3(), self.graph, None, None, 1),
                       CrossProtocolDetector(StubW3(), self.graph, None, None, 1)],
            **kw)
        counter = _PairCountingGraph(self.graph)
        engine.detectors[1].graph = counter
        return engine, counter, set(anchor_pairs(1))

    def test_event_free_block_call_count_and_uniqueness(self):
        with patch_quote_path():
            engine, counter, anchors = self._engine_with_counter()
            engine.process(100, touched_pools=frozenset())
        calls = counter.calls
        self.assertLessEqual(len(calls), 30, f"total CALL count must be <= 30: {len(calls)}")
        self.assertEqual(len(calls), len(set(calls)), "no duplicate pair checks in the block")
        for a in anchors:
            self.assertEqual(calls.count(a), 1, f"anchor {a} checked exactly once")
        self.assertGreaterEqual(len(set(calls)), len(anchors))

    def test_touched_plus_scheduled_block_call_count_and_uniqueness(self):
        with patch_quote_path():
            engine, counter, anchors = self._engine_with_counter()
            engine.process(100, touched_pools=frozenset({P1}))
        calls = counter.calls
        self.assertLessEqual(len(calls), 30, f"total CALL count must be <= 30: {len(calls)}")
        self.assertEqual(len(calls), len(set(calls)), "no duplicate pair checks block-wide")
        for a in anchors:
            self.assertEqual(calls.count(a), 1, f"anchor {a} checked exactly once (reactive)")
        # supplementary pairs were still checked by the scheduled start
        additional = {p for p in calls if p not in anchors}
        self.assertTrue(additional, "supplementary pairs preserved")


if __name__ == "__main__":
    unittest.main()
