"""DSS-WO-MI-102B regression: bounded event-independent opportunity discovery.

Covers the four required regression groups:

- Pair selection: eligible non-anchor pairs supplement (never replace) the
  anchor pairs; direction-normalized keys; multi-venue preservation; the
  24-additional / 30-total budget; dust and unpriced pools stay filtered.
- Event-independent discovery: the scheduled bounded pass finds profitable
  routes with no touched pools; ordinary quiet blocks do not rescan; the
  scheduled path shares validation/identity with the reactive path; a
  dual-path candidate enters the pipeline once.
- New-pool discovery: a valid newly registered pool is considered before its
  first swap; incomplete/zero-reserve state triggers nothing; unchanged pools
  are not retried block after block.
- Existing behaviour: reactive discovery unchanged, four-hop and 250-pair
  caps unchanged, engine dedup unchanged.
"""
import unittest
from contextlib import ExitStack
from unittest import mock

import bounded_discovery as bd
from bounded_discovery import (
    BoundedDiscoveryConfig,
    RankedPairUniverse,
    anchor_pairs,
    normalize_pair,
    pool_is_searchable,
)
from detectors import base as detectors_base
from detectors import cycles as cycles_mod
from detectors import protocol_opportunities as po_mod
from detectors.advanced import CandidateConfig
from detectors.protocol_opportunities import CrossProtocolDetector
from graph import PoolEdge, TokenGraph
from opportunity_engine import OpportunityEngine
from tvl import init_prices
import tvl as tvl_mod
import validation as validation_mod
from config import MAX_HOPS, MIN_POOL_LIQUIDITY_USD

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
A = "0x" + "11" * 20
B = "0x" + "22" * 20
UNK1 = "0x" + "66" * 20  # unpriceable: no metadata, no fallback
UNK2 = "0x" + "77" * 20

P1 = "0x" + "01" * 20  # WETH/A, rate 2.0
P2 = "0x" + "02" * 20  # A/B,   rate 2.0
P3 = "0x" + "03" * 20  # B/WETH, rate 0.26 — only profitable direction


class StubEth:
    gas_price = 30 * 10**9

    def get_block(self, n):
        return {"hash": str(n).rjust(30, "0").encode()}


class StubW3:
    eth = StubEth()


def _edge(pool, dex, t_in, t_out, r_in, r_out, tvl=50_000.0, version=2):
    return PoolEdge(
        pool_address=pool, dex_name=dex, token_in=t_in.lower(), token_out=t_out.lower(),
        reserve_in=r_in, reserve_out=r_out, fee_bps=30, version=version, tvl_usd=tvl,
        protocol="uniswap_v2", pool_token0=min(t_in.lower(), t_out.lower()),
        pool_token1=max(t_in.lower(), t_out.lower()),
    )


def add_v2_pool(graph, pool, t_in, t_out, rate, tvl=50_000.0):
    """Register a V2 pool at `rate` (out-wei per in-wei), both directions."""
    r_in = 10**22
    r_out = int(r_in * rate)
    graph.add_or_update_pool(
        _edge(pool, "UniswapV2", t_in, t_out, r_in, r_out, tvl),
        _edge(pool, "UniswapV2", t_out, t_in, r_out, r_in, tvl),
    )


def build_profitable_graph():
    """{P3, P1, P2} loop, only profitable direction B -> WETH -> A -> B."""
    init_prices(2000.0, WETH, [USDC, USDT, DAI])
    g = TokenGraph(chain_id=1)
    add_v2_pool(g, P1, WETH, A, 2.0)
    add_v2_pool(g, P2, A, B, 2.0)
    add_v2_pool(g, P3, B, WETH, 0.26)
    tvl_mod.set_graph_ref(g)
    return g


def make_engine(graph, detectors=None, **kw):
    defaults = dict(scheduled_pass_interval=50)
    defaults.update(kw)
    return OpportunityEngine(
        w3=StubW3(), chain_id=1,
        detectors=detectors if detectors is not None else [cycles_mod.CycleDetector(StubW3(), graph, None, None, 1)],
        graph=graph, **defaults,
    )


def patch_quote_path(min_profit=0.0, validate=None):
    """Disable contract quoting, zero the profit floor, pass validation.

    Returns an ``ExitStack`` usable as ``with patch_quote_path():``."""
    stack = ExitStack()
    stack.enter_context(mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""))
    stack.enter_context(mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""))
    stack.enter_context(mock.patch.object(cycles_mod, "MIN_PROFIT_USD", min_profit))
    stack.enter_context(mock.patch.object(
        validation_mod, "validate_many",
        validate or (lambda w3, opps, chain_id, **kw: list(opps))))
    return stack


# ═════════════════════════════════════════════════════════════════════════════
# Pair selection
# ═════════════════════════════════════════════════════════════════════════════

class TestPairSelection(unittest.TestCase):
    def setUp(self):
        init_prices(2000.0, WETH, [USDC, USDT, DAI])
        self.graph = TokenGraph(chain_id=1)
        self.universe = RankedPairUniverse(self.graph, 1)

    def test_eligible_non_anchor_pair_selected(self):
        add_v2_pool(self.graph, P1, WETH, A, 2.0)
        self.universe.refresh(100, force=True)
        pairs = self.universe.additional_pairs()
        self.assertIn(normalize_pair(WETH, A), pairs)
        # anchors are preserved separately, never re-listed as additional
        anchors = {normalize_pair(*p) for p in anchor_pairs(1)}
        self.assertFalse(anchors & {normalize_pair(*p) for p in pairs})
        self.assertEqual(len(anchor_pairs(1)), 6)

    def test_opposite_directions_normalize_to_one_pair_key(self):
        """Same pair registered in different direction orders -> one key,
        both venues visible."""
        add_v2_pool(self.graph, P1, WETH, A, 2.0)
        # P2 registered with the OPPOSITE canonical direction first
        add_v2_pool(self.graph, P2, A, WETH, 2.0)
        self.universe.refresh(100, force=True)
        self.assertEqual([p for p in self.universe.additional_pairs()
                          if p == normalize_pair(WETH, A)].count(normalize_pair(WETH, A)), 1)
        venues = self.graph.all_edges_for_pair(WETH, A)
        self.assertEqual({e.pool_address.lower() for e in venues}, {P1, P2})
        self.assertEqual(len(venues), 4)  # both directions of both venues

    def test_multi_venue_pair_all_venues_preserved(self):
        for i, pool in enumerate(("0x" + "a0" * 20, "0x" + "a1" * 20, "0x" + "a2" * 20)):
            add_v2_pool(self.graph, pool, WETH, A, 2.0 + i * 0.01)
        venues = self.graph.all_edges_for_pair(WETH, A)
        self.assertEqual(len({e.pool_address.lower() for e in venues}), 3)
        self.universe.refresh(100, force=True)
        seeds = self.universe.seed_pools()
        # the pair's venues are the only pools in this graph: all 3 seeded
        self.assertEqual(len(seeds), 3)
        # the pair search sees every venue (3 pools x both directions = 6)
        self.assertEqual(len(venues), 6)

    def test_additional_pair_limit_and_total_budget(self):
        for i in range(40):
            add_v2_pool(self.graph, "0x" + f"{i:04d}" * 10, WETH, "0x" + f"{i + 1:04d}" * 10, 2.0)
        self.universe.refresh(100, force=True)
        self.assertEqual(len(self.universe.additional_pairs()),
                         BoundedDiscoveryConfig().additional_pair_limit)
        self.assertLessEqual(self.universe.last_stats["total_pair_checks"],
                             BoundedDiscoveryConfig().max_pair_checks)
        self.assertEqual(self.universe.last_stats["total_pair_checks"],
                         len(anchor_pairs(1)) + len(self.universe.additional_pairs()))

    def test_anchors_remain_available(self):
        add_v2_pool(self.graph, P1, WETH, A, 2.0)
        add_v2_pool(self.graph, P2, WETH, USDC, 1.0 / 2000.0)  # anchor pair venue
        add_v2_pool(self.graph, P3, WETH, USDC, 1.0 / 1900.0)  # second venue
        self.universe.refresh(100, force=True)
        seeds = self.universe.seed_pools()
        # anchor pairs contribute their single top venue; the pair as a whole
        # stays covered (both venues are still visible to pair searches)
        anchor_venues = {P2, P3} & set(seeds)
        self.assertEqual(len(anchor_venues), 1, "one seed venue per anchor pair")
        self.assertIn(P1, seeds, "additional ranked pairs still seeded")
        # CrossProtocol still checks anchor pairs when given extra pairs
        detector = CrossProtocolDetector(StubW3(), self.graph, None, None, 1)
        with mock.patch.object(self.graph, "all_edges_for_pair",
                               wraps=self.graph.all_edges_for_pair) as spy:
            detector.on_block(100, extra_pairs=self.universe.additional_pairs())
            called = {tuple(sorted((c.args[0].lower(), c.args[1].lower())))
                      for c in spy.call_args_list}
        self.assertIn(normalize_pair(WETH, USDC), called)

    def test_dust_and_unpriced_pools_do_not_bypass_filtering(self):
        add_v2_pool(self.graph, P1, WETH, A, 2.0, tvl=50_000.0)
        add_v2_pool(self.graph, P2, WETH, UNK1, 2.0, tvl=0.0)      # unpriceable pair
        add_v2_pool(self.graph, P3, WETH, UNK2, 2.0, tvl=5_000.0)  # dust below gate
        self.universe.refresh(100, force=True)
        pairs = self.universe.additional_pairs()
        self.assertEqual(pairs, [normalize_pair(WETH, A)])
        seeds = self.universe.seed_pools()
        self.assertEqual(seeds, [P1])
        self.assertEqual(self.universe.last_stats["additional_pairs"], 1)


# ═════════════════════════════════════════════════════════════════════════════
# Event-independent discovery (scheduled bounded pass)
# ═════════════════════════════════════════════════════════════════════════════

class TestScheduledPass(unittest.TestCase):
    def setUp(self):
        self.graph = build_profitable_graph()

    def _run(self, block, touched=frozenset()):
        with patch_quote_path():
            engine = make_engine(self.graph)
            return engine, engine.process(block, touched_pools=touched)

    def test_profitable_route_found_without_any_event(self):
        engine, result = self._run(100)
        self.assertEqual(len(result.candidates), 1)
        opp = result.candidates[0]
        self.assertEqual({p.lower() for p in opp.pools}, {P1, P2, P3})
        self.assertEqual(opp.metadata["discovery_mode"], "scheduled")
        self.assertTrue(opp.metadata["scheduled_pass"])
        self.assertEqual(opp.metadata["scheduled_pass_reason"], "cadence")
        self.assertEqual(engine.last_scheduled_pass_block, 100)
        info = engine.last_scheduled_pass_info
        self.assertEqual(info["additional_pairs"], 3)  # WETH/A, A/B, B/WETH
        self.assertLessEqual(info["total_pair_checks"], 30)
        self.assertGreater(info["seed_pools_selected"], 0)

    def test_ordinary_quiet_block_does_not_rescan(self):
        engine, _ = self._run(100)
        detector = engine.detectors[0]
        calls = {"n": 0}
        original = detector.on_block

        def counting(*a, **k):
            calls["n"] += 1
            return original(*a, **k)

        detector.on_block = counting
        with patch_quote_path():
            engine.process(101, touched_pools=frozenset())
        self.assertEqual(calls["n"], 0, "quiet block must not re-run the cycle search")
        self.assertEqual(engine.last_scheduled_pass_block, 100)

    def test_cadence_interval_respected(self):
        with patch_quote_path():
            engine = make_engine(self.graph)
            engine.process(100, touched_pools=frozenset())
            self.assertEqual(engine.last_scheduled_pass_block, 100)
            engine.process(149, touched_pools=frozenset())
            self.assertEqual(engine.last_scheduled_pass_block, 100, "before interval: no pass")
            engine.process(150, touched_pools=frozenset())
            self.assertEqual(engine.last_scheduled_pass_block, 150, "interval due: pass")

    def test_scheduled_shares_validation_and_identity_with_reactive(self):
        validated = []
        with patch_quote_path(validate=lambda w3, opps, chain_id, **kw:
                              (validated.extend(opps), list(opps))[1]):
            engine = make_engine(self.graph)
            scheduled = engine.process(100, touched_pools=frozenset())
            reactive = engine.process(101, touched_pools=frozenset({P3}))
        self.assertEqual(len(validated), 2, "validation must run for both paths")
        self.assertEqual(len(scheduled.candidates), 1)
        self.assertEqual(len(reactive.candidates), 1)
        s, r = scheduled.candidates[0], reactive.candidates[0]
        self.assertEqual(OpportunityEngine._identity_key(s), OpportunityEngine._identity_key(r))
        self.assertEqual(s.metadata["route_state_status"], r.metadata["route_state_status"])
        self.assertEqual(s.metadata["detector"], r.metadata["detector"])

    def test_dual_path_candidate_enters_pipeline_once(self):
        with patch_quote_path():
            engine = make_engine(self.graph)
            result = engine.process(100, touched_pools=frozenset({P3}))
        self.assertEqual(len(result.candidates), 1, "same route via reactive + scheduled must dedupe")
        self.assertEqual({p.lower() for p in result.candidates[0].pools}, {P1, P2, P3})
        self.assertEqual(engine.last_scheduled_pass_block, 100, "pass still ran alongside reactive")

    def test_scheduled_discovery_can_be_disabled(self):
        with patch_quote_path():
            engine = make_engine(self.graph, scheduled_discovery=False)
            result = engine.process(100, touched_pools=frozenset())
        self.assertEqual(len(result.candidates), 0)
        self.assertIsNone(engine.last_scheduled_pass_block)


# ═════════════════════════════════════════════════════════════════════════════
# New-pool discovery
# ═════════════════════════════════════════════════════════════════════════════

class TestNewPoolDiscovery(unittest.TestCase):
    def setUp(self):
        self.graph = build_profitable_graph()

    def test_newly_registered_pool_discoverable_before_first_swap(self):
        """A new pool completes a NEW profitable route the ranked universe
        does not know yet; it must be considered without any swap event."""
        with patch_quote_path():
            engine = make_engine(self.graph)
            first = engine.process(100, touched_pools=frozenset())
            self.assertEqual(engine.last_scheduled_pass_info["reason"], "cadence")

            # X/WETH pool making WETH -> X -> ... profitable with P2/P3:
            # cycle [X, WETH, B, X]: X->WETH 1/0.3, WETH->B 1/0.26... build a
            # simple 2-hop cross-dex spread instead: X->WETH (P4) vs WETH->X
            # is same pool -- use two venues for pair X/WETH with a spread.
            P4 = "0x" + "04" * 20
            P5 = "0x" + "05" * 20
            X = "0x" + "88" * 20
            add_v2_pool(self.graph, P4, X, WETH, 1.0 / 2000.0)     # 1 X = 0.0005 WETH
            add_v2_pool(self.graph, P5, WETH, X, 2000.0 * 1.05)    # 1 WETH = 2100 X
            # (2-hop X -> WETH -> X: 1 * 0.0005 * 2100 = 1.05 profitable)

            result = engine.process(101, touched_pools=frozenset())
            self.assertEqual(engine.last_scheduled_pass_info["reason"], "new_pools")
            pools_of = [{p.lower() for p in c.pools} for c in result.candidates]
            self.assertTrue(any({P4, P5} <= pools for pools in pools_of),
                            f"new pools must be discoverable pre-swap: {pools_of}")

    def test_incomplete_pool_state_triggers_no_false_opportunity(self):
        bad = "0x" + "09" * 20
        with patch_quote_path():
            engine = make_engine(self.graph)
            engine.process(100, touched_pools=frozenset())  # drains fixture registrations
            # zero-reserve registration (invalid search state)
            self.graph.add_or_update_pool(
                _edge(bad, "UniswapV2", WETH, A, 0, 0, tvl=0.0),
                _edge(bad, "UniswapV2", A, WETH, 0, 0, tvl=0.0),
            )
            self.assertFalse(pool_is_searchable(self.graph, bad))
            result = engine.process(101, touched_pools=frozenset())
        info = engine.last_scheduled_pass_info
        self.assertEqual(info["reason"], "new_pools", "registration must trigger a pass")
        self.assertEqual(info["new_pool_seeds"], 0, "unsearchable state must not seed discovery")
        for c in result.candidates:
            self.assertNotIn(bad, {p.lower() for p in c.pools})
            self.assertNotIn(bad, {p.lower() for p in c.tokens})

    def test_pending_waiting_room_pool_is_not_searchable(self):
        p = "0x" + "0a" * 20
        e = _edge(p, "UniswapV2", WETH, A, 10**22, 2 * 10**22)
        e = PoolEdge(**{**e.__dict__, "is_pending": True})
        self.graph.add_or_update_pool(e, e)
        self.assertFalse(pool_is_searchable(self.graph, p))

    def test_registration_burst_is_spaced_not_stalling(self):
        """30 registrations > one pass's 24-pool budget: consecutive
        new-pool passes are spaced (5 blocks), spreading the burst over the
        cadence scale instead of stalling consecutive blocks."""
        with patch_quote_path():
            engine = make_engine(self.graph)
            engine.process(100, touched_pools=frozenset())  # cadence pass
            for i in range(30):
                addr = f"0x{0xb000 + i:064x}"
                add_v2_pool(self.graph, addr, WETH, f"0x{0xc000 + i:064x}", 2.0)
            blocks_with_pass = []
            for b in range(101, 110):
                engine.process(b, touched_pools=frozenset())
                blocks_with_pass.append((b, engine.last_scheduled_pass_block))
            self.assertEqual(blocks_with_pass[0][1], 101, "first burst pass: immediate")
            for b, last in blocks_with_pass[1:5]:
                self.assertEqual(last, 101, f"block {b}: consecutive burst pass spaced out")
            self.assertEqual(blocks_with_pass[5][1], 106, "spacing elapsed: burst pass resumes")
            self.assertEqual(blocks_with_pass[8][1], 106, "burst drained: no further passes")

    def test_unchanged_pool_not_retriggered_block_after_block(self):
        with patch_quote_path():
            engine = make_engine(self.graph)
            engine.process(100, touched_pools=frozenset())
            P4 = "0x" + "04" * 20
            add_v2_pool(self.graph, P4, WETH, A, 2.0)  # registration event
            engine.process(101, touched_pools=frozenset())
            self.assertEqual(engine.last_scheduled_pass_block, 101)
            detector = engine.detectors[0]
            calls = {"n": 0}
            original = detector.on_block

            def counting(*a, **k):
                calls["n"] += 1
                return original(*a, **k)

            detector.on_block = counting
            engine.process(102, touched_pools=frozenset())
            engine.process(103, touched_pools=frozenset())
            self.assertEqual(calls["n"], 0, "no pending growth: quiet blocks cost nothing")
            # re-registering (updating) the same pool does not re-trigger either
            add_v2_pool(self.graph, P4, WETH, A, 2.0)
            engine.process(104, touched_pools=frozenset())
            self.assertEqual(calls["n"], 0, "unchanged pool must not re-trigger discovery")


# ═════════════════════════════════════════════════════════════════════════════
# Existing behaviour preserved
# ═════════════════════════════════════════════════════════════════════════════

class TestExistingBehaviour(unittest.TestCase):
    def setUp(self):
        self.graph = build_profitable_graph()

    def test_reactive_path_still_functional_and_tagged(self):
        with patch_quote_path():
            engine = make_engine(self.graph, scheduled_discovery=False)
            result = engine.process(100, touched_pools=frozenset({P3}))
        self.assertEqual(len(result.candidates), 1)
        opp = result.candidates[0]
        self.assertEqual(opp.metadata["discovery_mode"], "reactive")
        self.assertFalse(opp.metadata.get("scheduled_pass", False))
        self.assertEqual({p.lower() for p in opp.pools}, {P1, P2, P3})

    def test_four_hop_limit_unchanged(self):
        # 4-pool ring: the 4-hop cycle must still be found...
        g4 = TokenGraph(chain_id=1)
        tokens4 = [WETH] + ["0x" + f"{i:02d}" * 20 for i in range(1, 4)]
        pools4 = ["0x" + f"{i:02d}" * 20 for i in range(1, 5)]
        for i, pool in enumerate(pools4):
            add_v2_pool(g4, pool, tokens4[i % 4], tokens4[(i + 1) % 4], 1.5)
        cycles4 = g4.find_cycles(source_tokens=list(g4.tokens), min_profit_ratio=0.0001)
        self.assertTrue(any(c.hop_count == 4 for c in cycles4),
                        "the existing four-hop route limit must still admit 4-hop cycles")
        # ...and a 5-ring must never yield a 5-hop cycle (cap intact)
        g5 = TokenGraph(chain_id=1)
        tokens5 = [WETH] + ["0x" + f"{i:02d}" * 20 for i in range(1, 5)]
        pools5 = ["0x" + f"{i:02d}" * 20 for i in range(1, 6)]
        for i, pool in enumerate(pools5):
            add_v2_pool(g5, pool, tokens5[i % 5], tokens5[(i + 1) % 5], 1.5)
        cycles5 = g5.find_cycles(source_tokens=list(g5.tokens), min_profit_ratio=0.0001)
        # The 5-ring contains exactly one cycle: the profitable 5-hop
        # (1.5^5 > 1). An empty result is therefore the cap working: the
        # profitable 5-hop exists in the graph by construction but is never
        # returned, and no shorter closed walk exists in a 5-ring.
        self.assertEqual(cycles5, [])
        self.assertEqual(MAX_HOPS, 4)

    def test_pair_and_liquidity_caps_unchanged(self):
        self.assertEqual(CandidateConfig().max_pairs, 250)
        self.assertEqual(BoundedDiscoveryConfig().max_pair_checks, 30)
        self.assertEqual(BoundedDiscoveryConfig().additional_pair_limit, 24)
        self.assertEqual(MIN_POOL_LIQUIDITY_USD, 10_000.0)

    def test_engine_dedup_unchanged(self):
        with patch_quote_path():
            engine = make_engine(self.graph, scheduled_discovery=False)
            r1 = engine.process(100, touched_pools=frozenset({P3}))
            # identical route discovered twice inside ONE pipeline call would
            # still collapse: feed the same candidate back through the engine
            dup = engine.process_candidates(list(r1.candidates) * 2, 100)
        self.assertEqual(len(dup.candidates), 1)


if __name__ == "__main__":
    unittest.main()
