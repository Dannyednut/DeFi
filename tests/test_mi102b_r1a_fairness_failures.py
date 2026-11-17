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
    and supports injected detector failures."""

    def __init__(self, engine):
        self.events = []           # (block, seed) per booked slot
        self.calls = 0             # successful detector invocations
        self.fail_next = 0         # fail this many invocations, then succeed
        self.failed_slices = []    # (block, seeds) of failed invocations
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

    @property
    def seeds(self):
        return [s for _, s in self.events]


class CrossRecorder:
    """Wraps the engine's cross_proto detector: records every invocation
    (anchors checked or not) and supports injected failures."""

    def __init__(self, engine):
        self.invocations = []      # (block, check_anchors) — successful only
        self.failures = 0
        self.fail_next = 0
        self.engine = engine
        self._original = engine.detectors[1].on_block
        engine.detectors[1].on_block = self._hook

    def _hook(self, block_number, *args, **kwargs):
        anchors = kwargs.get("check_anchors", True)
        if self.fail_next > 0:
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
        self.graph = build_profitable_graph()

    def test_ranked_universe_progresses_under_sustained_arrivals(self):
        """Registrations arrive EVERY invocation while a batch is active —
        never a finite pre-existing backlog. The alternation rule must keep
        ranked-universe seeds advancing; once arrivals stop, every
        registration must have been seeded exactly once (FIFO), with the
        per-slice and per-pass bounds respected throughout."""
        with patch_quote_path():
            engine = make_engine(self.graph)
            rec = SliceRecorder(engine)
            engine.process(100, touched_pools=frozenset())  # batch starts
            batch = engine._scheduled_batch
            self.assertIsNotNone(batch)
            universe_seed_set = set(batch["seeds"])

            # SUSTAINED arrivals: one new searchable pool per invocation,
            # 60 invocations in a row while the batch is active.
            stream = [_addr(i) for i in range(60)]
            for i, addr in enumerate(stream):
                add_v2_pool(self.graph, addr, _token(i), WETH, 2.0)
                engine.process(101 + i, touched_pools=frozenset())
            mid = engine.last_scheduled_batch_info
            self.assertFalse(mid["complete"], "stream ongoing: batch still active")

            # universe seeds progressed DURING the stream: alternation means
            # roughly half of the stream window's slots went to the universe.
            searched_mid = set(rec.seeds)
            self.assertTrue(universe_seed_set & searched_mid,
                            "ranked-universe seeds must advance while registrations keep arriving")
            # and the arrivals so far were seeded too (priority retained)
            self.assertTrue(set(stream) & searched_mid,
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
