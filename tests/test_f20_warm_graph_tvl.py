"""F-20 regression (DSS-WO-MI-102A): ``PoolCache.warm_graph`` must restore V2/V3
pool TVL so the restored graph is visible to cycle search immediately after a
restart.

Original failure at the ENG-017 baseline: the V2 branch computed the warm-time
TVL estimate for the minimum-liquidity gate but did not attach any TVL to the
constructed ``PoolEdge`` (cache.py:405-406), so every restored V2 pool re-entered
the graph with ``tvl_usd=0.0`` and the cycle DFS pruned it
(graph.py:756, threshold MIN_POOL_LIQUIDITY_USD * 0.05). Restored pools only
became searchable again after the GeckoTerminal loop re-priced them at ~50 pools
per 60-second pass (crawler.py:746, 760-780) — hours on a production graph.

Semantics preserved by these tests: the minimum-liquidity GATE stays on the
warm-time estimate (dust and genuinely unpriceable pools must stay excluded);
only the value attached to the edge changes, preferring the persisted cache value
(GeckoTerminal-authoritative once the TVL loop has run, registration-time
otherwise) and falling back to the existing estimate.
"""
import tempfile
import unittest

from cache import PoolCache
from graph import TokenGraph
from tvl import estimate_tvl_v2, estimate_tvl_v3, init_prices
import tvl as tvl_mod

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
A = "0x" + "11" * 20
B = "0x" + "22" * 20
UNK1 = "0x" + "44" * 20  # never priced: no metadata, no fallback entry
UNK2 = "0x" + "55" * 20
P1 = "0x" + "01" * 20
P2 = "0x" + "02" * 20
P3 = "0x" + "03" * 20

# sqrtPriceX96 for a 2.0 token1-per-token0 pool (18/18 decimals).
SQRT_P2 = int((2.0 ** 0.5) * (2 ** 96))


def _meta(t):
    derived = {WETH.lower(): 1.0, A.lower(): 0.5, B.lower(): 0.25}.get(t.lower(), 0.0)
    return {"symbol": "T" + t[2:4], "decimals": 18, "derivedETH": derived}


def _v2_record(t0, t1, r0, r1, tvl_usd, with_meta=True):
    rec = {
        "token0": t0.lower(), "token1": t1.lower(),
        "dex": "UniswapV2", "protocol": "uniswap_v2", "version": 2,
        "fee_bps": 30, "r0": r0, "r1": r1, "last_updated": 1.0,
        "state_block": 99, "state_block_hash": "0x" + "ab" * 32,
        "state_source": "block_information", "state_status": "FRESH",
        "tvl_usd": tvl_usd,
    }
    if with_meta:
        rec["token0_meta"], rec["token1_meta"] = _meta(t0), _meta(t1)
    return rec


def _v3_record(t0, t1, sqrt, liq, tvl_usd, with_meta=True):
    rec = {
        "token0": t0.lower(), "token1": t1.lower(),
        "dex": "UniswapV3", "protocol": "uniswap_v3", "version": 3,
        "fee_bps": 30, "r0": 0, "r1": 0,
        "sqrt_price_x96": sqrt, "liquidity": liq, "last_updated": 1.0,
        "state_block": 99, "state_block_hash": "0x" + "ab" * 32,
        "state_source": "block_information", "state_status": "FRESH",
        "tvl_usd": tvl_usd,
    }
    if with_meta:
        rec["token0_meta"], rec["token1_meta"] = _meta(t0), _meta(t1)
    return rec


def warm_from(records):
    """Persist records, then warm a fresh graph exactly like a restart does
    (main.py order: init_prices -> TokenGraph -> set_graph_ref -> warm)."""
    tmp = tempfile.mkdtemp(prefix="f20_")
    cache = PoolCache(chain_id=1, cache_dir=tmp)
    for addr, rec in records.items():
        cache.set_pool(addr, rec)
    cache.save()
    graph = TokenGraph(w3=None, cache=None, chain_id=1)
    tvl_mod.set_graph_ref(graph)
    loaded = cache.warm_graph(graph)
    return cache, graph, loaded


class TestWarmGraphTVLRestore(unittest.TestCase):
    def setUp(self):
        init_prices(2000.0, WETH, [])

    def test_v2_restore_prefers_persisted_tvl(self):
        """A persisted (authoritative) TVL must survive save -> restore intact."""
        _, graph, loaded = warm_from({P1: _v2_record(WETH, A, 10**21, 2 * 10**21, 77_777.0)})
        self.assertEqual(loaded, 1)
        edges = graph.get_edges_for_pool(P1)
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0].tvl_usd, 77_777.0)
        self.assertEqual(edges[1].tvl_usd, 77_777.0)

    def test_v2_restore_falls_back_to_estimate(self):
        """Without a persisted value, the existing estimation path is used."""
        _, graph, loaded = warm_from({P1: _v2_record(WETH, A, 10**21, 2 * 10**21, 0.0)})
        self.assertEqual(loaded, 1)
        expected = estimate_tvl_v2(WETH.lower(), A.lower(), 10**21, 2 * 10**21, dex="UniswapV2")
        self.assertGreater(expected, 10_000.0, "fixture must clear the warm gate on its estimate")
        for edge in graph.get_edges_for_pool(P1):
            self.assertAlmostEqual(edge.tvl_usd, expected, places=4)

    def test_v3_restore_prefers_persisted_tvl(self):
        """V3 restored edges keep the persisted TVL rather than estimate-only."""
        _, graph, loaded = warm_from(
            {P1: _v3_record(WETH, A, SQRT_P2, 10**21, 88_888.0)})
        self.assertEqual(loaded, 1)
        edges = graph.get_edges_for_pool(P1)
        self.assertEqual(len(edges), 2)
        for edge in edges:
            self.assertEqual(edge.sqrt_price_x96, SQRT_P2)
            self.assertEqual(edge.tvl_usd, 88_888.0)

    def test_restarted_graph_is_discoverable_without_repricing(self):
        """Requirement 6: after save -> restore, a profitable route is found by
        find_cycles immediately — no GeckoTerminal re-pricing pass involved."""
        records = {
            P1: _v2_record(WETH, A, 10**21, 2 * 10**21, 50_000.0),   # rate 2.0
            P2: _v2_record(A, B, 2 * 10**24, 4 * 10**24, 50_000.0),  # rate 2.0
            P3: _v2_record(B, WETH, 4 * 10**24, int(0.26 * 10**24), 50_000.0),  # rate 0.26
        }
        _, graph, loaded = warm_from(records)
        self.assertEqual(loaded, 3)
        cycles = graph.find_cycles(max_hops=3)
        route = [c for c in cycles if {p.lower() for p in c.pools} == {P1, P2, P3}]
        self.assertTrue(route, "profitable restored route must be discoverable right after restart")
        self.assertGreater(route[0].estimated_profit_ratio, 0.0)

    def test_unpriced_pools_remain_excluded(self):
        """Genuinely unpriceable pools (no persisted TVL, no price data) must not
        be misclassified as liquid: the estimate is 0 and the gate excludes them,
        exactly as before the fix."""
        cache, graph, loaded = warm_from(
            {P1: _v2_record(UNK1, UNK2, 10**21, 2 * 10**21, 0.0, with_meta=False)})
        self.assertEqual(loaded, 0)
        self.assertEqual(graph.pool_count, 0)
        self.assertTrue(cache._pools[P1].get("below_threshold"))

    def test_dust_reserves_with_stale_persisted_tvl_remain_excluded(self):
        """The minimum-liquidity GATE stays on the warm-time estimate: a stale
        persisted TVL must not rescue a dust pool into the searchable graph."""
        cache, graph, loaded = warm_from(
            {P1: _v2_record(WETH, A, 10**12, 2 * 10**12, 50_000.0)})
        self.assertEqual(loaded, 0)
        self.assertEqual(graph.pool_count, 0)
        self.assertTrue(cache._pools[P1].get("below_threshold"))

    def test_v3_estimate_fallback_removes_nothing(self):
        """V3 without a persisted value keeps attaching the existing estimate."""
        _, graph, loaded = warm_from(
            {P1: _v3_record(WETH, A, SQRT_P2, 10**21, 0.0)})
        self.assertEqual(loaded, 1)
        expected = estimate_tvl_v3(WETH.lower(), A.lower(), SQRT_P2, 10**21, dex="UniswapV3")
        self.assertGreater(expected, 10_000.0, "fixture must clear the warm gate on its estimate")
        for edge in graph.get_edges_for_pool(P1):
            self.assertAlmostEqual(edge.tvl_usd, expected, places=4)


if __name__ == "__main__":
    unittest.main()
