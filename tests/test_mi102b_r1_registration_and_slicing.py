"""DSS-WO-MI-102B-R1 regression: registration reliability and bounded
scheduled search.

Covers the R1 requirements:

- §2 lossless registration backlog: >256-entry bursts are retained, a
  1,000-pool backlog drains over successive scheduled passes with every
  pool returned exactly once, per-pass consumption and per-slice processing
  stay bounded, case differences cannot duplicate a registration, and a
  registration scanned reactively in the same block is never re-seeded.
- §3 incremental scheduled batch: seeds are processed in deterministic
  slices per engine invocation, unprocessed seeds are retained, batch start
  and completion are reported separately, and no overlapping sweep starts.
- §4 liquidity-gate consistency: cycle-search origins obey the same
  MIN_POOL_LIQUIDITY_USD gate as the ranked pair selection while ALL venues
  stay visible to the pair-search logic.
- §5 duplicate-work prevention: anchors are checked once per block even
  when a scheduled batch starts on a block that also has touched pools, and
  the block-wide pair-check total respects the 30-pair budget.
"""
import unittest
from collections import Counter
from unittest import mock

from bounded_discovery import (
    BoundedDiscoveryConfig,
    RankedPairUniverse,
    normalize_pair,
    pool_is_searchable,
)
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


def _addr(i: int) -> str:
    return f"0x{0xd000 + i:064x}"


def _token(i: int) -> str:
    return f"0x{0xe000 + i:064x}"


class SeedRecorder:
    """Captures every scheduled cycle-search slice (block -> seed set)."""

    def __init__(self, engine):
        self.slices = []
        self.engine = engine
        self._original = engine.detectors[0].on_block
        engine.detectors[0].on_block = self._capture

    def _capture(self, block_number, *args, **kwargs):
        if kwargs.get("scheduled_seeds") is not None:
            self.slices.append((block_number, set(kwargs["scheduled_seeds"])))
        return self._original(block_number, *args, **kwargs)

    @property
    def flat(self):
        return [s for _, seeds in self.slices for s in seeds]


class _PairCountingGraph:
    """Counts CrossProtocolDetector pair checks without touching the engine
    or the shared universe (which uses the real graph object)."""

    def __init__(self, inner):
        self._inner = inner
        self.pair_checks = []

    def all_edges_for_pair(self, a, b):
        self.pair_checks.append(normalize_pair(a, b))
        return self._inner.all_edges_for_pair(a, b)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ═════════════════════════════════════════════════════════════════════════════
# §2 — Lossless registration backlog
# ═════════════════════════════════════════════════════════════════════════════

class TestLosslessRegistrationBacklog(unittest.TestCase):
    def setUp(self):
        init_prices(2000.0, WETH, [USDC])
        self.graph = TokenGraph(chain_id=1)  # bare graph: the ring counts only what we register

    def test_burst_larger_than_256_entries_is_retained(self):
        engine = make_engine(self.graph, scheduled_discovery=False)
        ring = self.graph.recently_registered
        for i in range(300):  # > the retired 256-entry deque capacity
            add_v2_pool(self.graph, _addr(i), _token(i), WETH, 2.0)
        self.assertEqual(len(ring), 300, "no registration may be lost to overflow")
        self.assertEqual(ring.received, 300)
        # FIFO order preserved (oldest registration first)
        first = ring.pop_upto(3)
        self.assertEqual(first, [_addr(0), _addr(1), _addr(2)])

    def test_case_differences_cannot_duplicate_registrations(self):
        engine = make_engine(self.graph, scheduled_discovery=False)
        ring = self.graph.recently_registered
        mixed = _addr(1)
        ring.record(mixed)
        ring.record(mixed.upper())       # same pool, different case
        ring.record(mixed.replace("0x", "0X", 1).upper())  # and again
        self.assertEqual(len(ring), 1)
        add_v2_pool(self.graph, _addr(2), _token(2), WETH, 2.0)
        ring.record(_addr(2).upper())
        self.assertEqual(len(ring), 2)

    def test_1000_pool_backlog_drains_exactly_once_over_successive_passes(self):
        """1,000 searchable pools registered before any consumption: the
        backlog drains over successive scheduled passes — every pool seeded
        exactly once, per-pass consumption and per-slice processing bounded,
        nothing dropped."""
        self.graph = build_profitable_graph()  # priced fixture: 3 pools pre-registered
        # slice=2 halves this heavy test's runtime; the per-slice bound (<=2)
        # and the default slice=1 are covered by the lighter tests above
        engine = make_engine(self.graph,
                             bounded_config=BoundedDiscoveryConfig(scheduled_seeds_per_slice=2))
        recorder = SeedRecorder(engine)
        ring = self.graph.recently_registered
        registered = [_addr(i) for i in range(1000)]
        for i in range(1000):
            add_v2_pool(self.graph, _addr(i), _token(i), WETH, 2.0)
        self.assertEqual(len(ring), 1003, "3 fixture pools + 1000 new registrations, none lost")

        ring_lens = []
        batch = 100
        slices_at_drain_end = None
        drain_summary = None
        while True:
            before = len(ring)
            engine.process(batch, touched_pools=frozenset())
            ring_lens.append(before - len(ring))
            batch += 1
            info = engine.last_scheduled_batch_info
            if info.get("complete") and len(ring) == 0 and not engine._scheduled_batch:
                slices_at_drain_end = len(recorder.slices)
                drain_summary = dict(info)
                # post-drain quiet blocks: a fresh cadence batch may start
                # (periodic ranked-universe re-scan) — the backlog must stay
                # empty throughout
                engine.process(batch, touched_pools=frozenset())
                engine.process(batch + 1, touched_pools=frozenset())
                break
            self.assertLess(batch, 100 + 1200, "backlog must drain in bounded time")

        self.assertTrue(drain_summary["complete"])
        self.assertEqual(drain_summary["seeds_processed"] + drain_summary["seeds_skipped_reactive_overlap"]
                         + drain_summary["seeds_deduped"], drain_summary["seeds_total"],
                         "batch accounting must be consistent")
        drain_searched = [s for _, seeds in recorder.slices[:slices_at_drain_end] for s in seeds]
        per_pool = Counter(drain_searched)
        self.assertTrue(all(per_pool[p] == 1 for p in registered),
                        f"every registered pool seeded exactly once during the drain: "
                        f"{[p for p in registered if per_pool[p] != 1][:5]}")
        self.assertEqual(len(ring), 0, "backlog fully consumed and stays consumed")
        self.assertEqual(sum(1 for d in ring_lens if d > 24), 0,
                         f"per-pass consumption must stay <= 24: {sorted(set(ring_lens))}")
        self.assertTrue(all(len(seeds) <= 2 for _, seeds in recorder.slices),
                        "per-invocation slice bound respected (all batches)")

    def test_registration_scanned_reactively_same_block_not_reseeded(self):
        """A pool registered AND touched in the same block is searched by the
        reactive detector; the scheduled batch must not search it again."""
        self.graph = build_profitable_graph()  # priced fixture so other seeds exist too
        new_pool = _addr(999)
        new_token = _token(999)
        recorded_seeds = []
        with patch_quote_path():
            engine = make_engine(self.graph)
            detector = engine.detectors[0]
            original = detector.on_block

            def capturing(block_number, *args, **kwargs):
                if kwargs.get("scheduled_seeds") is not None:
                    recorded_seeds.append(set(kwargs["scheduled_seeds"]))
                return original(block_number, *args, **kwargs)

            detector.on_block = capturing
            add_v2_pool(self.graph, new_pool, new_token, WETH, 2.0)
            engine.process(100, touched_pools=frozenset({new_pool}))
        self.assertFalse(any(new_pool in s for s in recorded_seeds),
                         "reactively-scanned pool must not become a scheduled seed")
        self.assertTrue(recorded_seeds, "scheduled batch still ran (other seeds)")


# ═════════════════════════════════════════════════════════════════════════════
# §3 — Incremental scheduled batch
# ═════════════════════════════════════════════════════════════════════════════

class TestIncrementalScheduledBatch(unittest.TestCase):
    def setUp(self):
        self.graph = build_profitable_graph()

    def test_batch_spans_invocations_with_bounded_slices(self):
        # slice=2 keeps the arithmetic assertions tight; the production
        # default is 1 (see BoundedDiscoveryConfig.scheduled_seeds_per_slice)
        recorder = SeedRecorder(make_engine(self.graph, bounded_config=BoundedDiscoveryConfig(scheduled_seeds_per_slice=2)))
        with patch_quote_path():
            engine = recorder.engine
            engine.process(100, touched_pools=frozenset())
            info = engine.last_scheduled_batch_info
            self.assertFalse(info["complete"], "partial batch must not be labelled complete")
            self.assertEqual(info["start_block"], 100)
            self.assertEqual(info["seeds_processed"], 2, "deterministic slice: 2 seeds")
            self.assertGreater(info["main_pending"], 0)
            while engine._scheduled_batch is not None:
                engine.process(100 + engine.last_scheduled_batch_info["slices"], touched_pools=frozenset())
            done = engine.last_scheduled_batch_info
        self.assertTrue(done["complete"])
        self.assertEqual(done["end_block"], done["start_block"] + done["slices"] - 1)
        self.assertEqual(done["seeds_processed"], done["seeds_total"])
        self.assertEqual(done["slices"], -(-done["seeds_total"] // 2), "ceil(total / slice_size)")
        self.assertLess(done["batch_ms"], done["slices"] * 60_000, "sanity: per-slice ms recorded")
        self.assertTrue(all(len(s) <= 2 for _, s in recorder.slices))

    def test_no_overlapping_sweep_while_batch_active(self):
        recorder = SeedRecorder(make_engine(self.graph))
        with patch_quote_path():
            engine = recorder.engine
            engine.process(100, touched_pools=frozenset())
            start_block = engine.last_scheduled_pass_block
            seeds_seen_after_first = len(recorder.flat)
            # cadence would be due far later, but force the check: the active
            # batch must continue, never restart
            for b in range(101, 105):
                engine.process(b, touched_pools=frozenset())
                self.assertEqual(engine.last_scheduled_pass_block, start_block,
                                 "no new batch may start while one is active")
                self.assertGreaterEqual(len(recorder.flat), seeds_seen_after_first)
        self.assertTrue(engine.last_scheduled_batch_info["complete"])

    def test_unprocessed_seeds_retained_until_searched(self):
        recorder = SeedRecorder(make_engine(self.graph))
        with patch_quote_path():
            engine = recorder.engine
            engine.process(100, touched_pools=frozenset())
            total = engine.last_scheduled_batch_info["seeds_total"]
            processed = engine.last_scheduled_batch_info["seeds_processed"]
            self.assertLess(processed, total, "slice smaller than batch")
            while engine._scheduled_batch is not None:
                engine.process(200, touched_pools=frozenset())
            # nothing skipped, nothing lost: every seed eventually searched
            self.assertEqual(len(recorder.flat), len(set(recorder.flat)))
            self.assertEqual(len(recorder.flat), total)


# ═════════════════════════════════════════════════════════════════════════════
# §4 — Liquidity-gate consistency
# ═════════════════════════════════════════════════════════════════════════════

class TestLiquidityGateConsistency(unittest.TestCase):
    def setUp(self):
        init_prices(2000.0, WETH, [USDC])
        self.graph = TokenGraph(chain_id=1)
        self.universe = RankedPairUniverse(self.graph, 1)

    def test_sub_threshold_venues_not_seed_origins_but_visible_to_pair_search(self):
        hi, lo, dust = _addr(1), _addr(2), _addr(3)
        add_v2_pool(self.graph, hi, WETH, A, 2.0, tvl=50_000.0)   # qualifying venue
        add_v2_pool(self.graph, lo, WETH, A, 2.0, tvl=5_000.0)    # positive, below gate
        add_v2_pool(self.graph, dust, WETH, A, 2.0, tvl=0.0)      # unpriced
        self.universe.refresh(100, force=True)
        # the pair remains selected (its best venue clears the gate)
        self.assertIn(normalize_pair(WETH, A), self.universe.additional_pairs())
        # only the eligible venue becomes a DFS origin
        seeds = self.universe.seed_pools()
        self.assertEqual(seeds, [hi])
        # ALL venues remain visible to the pair-search logic
        venues = self.graph.all_edges_for_pair(WETH, A)
        self.assertEqual({e.pool_address for e in venues}, {hi, lo, dust})
        self.assertFalse(pool_is_searchable(self.graph, lo), "sub-threshold pool not search-eligible")
        self.assertTrue(pool_is_searchable(self.graph, hi))

    def test_newly_registered_sub_threshold_pool_not_a_seed(self):
        sub = _addr(10)
        recorder = SeedRecorder(make_engine(self.graph, scheduled_pass_interval=1))
        with patch_quote_path():
            engine = recorder.engine
            add_v2_pool(self.graph, sub, _token(10), WETH, 2.0, tvl=5_000.0)
            self.assertFalse(pool_is_searchable(self.graph, sub))
            for b in range(100, 108):
                engine.process(b, touched_pools=frozenset())
        self.assertFalse(any(sub in s for _, s in recorder.slices),
                         "positive-but-sub-threshold registration must not seed discovery")


# ═════════════════════════════════════════════════════════════════════════════
# §5 — Duplicate-work prevention (pair-check budget per block)
# ═════════════════════════════════════════════════════════════════════════════

class TestPairCheckBudgetPerBlock(unittest.TestCase):
    def setUp(self):
        # fixture: anchor pair WETH/USDC with two venues (spread candidate),
        # plus non-anchor ranked pairs
        self.graph = build_profitable_graph()
        add_v2_pool(self.graph, P2, WETH, USDC, 1.0 / 2000.0)   # reuse addresses as venues
        add_v2_pool(self.graph, P3, WETH, USDC, 1.0 / 1900.0)
        init_prices(2000.0, WETH, [USDC])

    def _engine_with_pair_counter(self, **kw):
        engine = make_engine(self.graph,
                             detectors=[
                                 __import__("detectors.cycles", fromlist=["CycleDetector"]).CycleDetector(
                                     StubW3(), self.graph, None, None, 1),
                                 CrossProtocolDetector(StubW3(), self.graph, None, None, 1),
                             ],
                             **kw)
        counter = _PairCountingGraph(self.graph)
        engine.detectors[1].graph = counter
        return engine, counter

    def test_event_free_scheduled_block_checks_each_pair_once_within_budget(self):
        with patch_quote_path():
            engine, counter = self._engine_with_pair_counter()
            engine.process(100, touched_pools=frozenset())
        distinct = set(counter.pair_checks)
        anchors = {normalize_pair(WETH, USDC)}
        self.assertLessEqual(len(distinct), BoundedDiscoveryConfig().max_pair_checks,
                             f"pair-check budget exceeded: {len(distinct)}")
        self.assertIn(normalize_pair(WETH, USDC), distinct, "anchors still checked")
        # anchors exactly once per block
        self.assertEqual(sum(1 for p in counter.pair_checks if p in anchors), 1)
        # the ranked additional pairs were checked too
        self.assertGreater(len(distinct), len(anchors))

    def test_scheduled_block_with_touched_pools_checks_anchors_once_total(self):
        with patch_quote_path():
            engine, counter = self._engine_with_pair_counter()
            # touched pools -> reactive cross_proto runs (anchors) AND the
            # scheduled batch starts (additional only, check_anchors=False)
            engine.process(100, touched_pools=frozenset({P1}))
        anchors = {normalize_pair(WETH, USDC)}
        distinct = set(counter.pair_checks)
        self.assertEqual(sum(1 for p in counter.pair_checks if p in anchors), 1,
                         "each anchor pair checked exactly once per block (reactive)")
        self.assertLessEqual(len(distinct), BoundedDiscoveryConfig().max_pair_checks,
                             f"block-wide pair-check budget exceeded: {len(distinct)}")
        # the supplementary pairs were still checked by the scheduled start
        additional = {p for p in distinct if p not in anchors}
        self.assertTrue(additional, "scheduled start must check the additional pairs")


if __name__ == "__main__":
    unittest.main()
