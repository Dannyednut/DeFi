"""F-20 regression (DSS-WO-MI-102A + R1): a genuine cache restart must restore
V2/V3 pool TVL so the graph is visible to cycle search immediately.

Original failure at the ENG-017 baseline: the V2 warm branch computed the
warm-time TVL estimate for the minimum-liquidity gate but did not attach any TVL
to the constructed ``PoolEdge`` (cache.py:405-406), so every restored V2 pool
re-entered the graph with ``tvl_usd=0.0`` and the cycle DFS pruned it
(graph.py:756, threshold MIN_POOL_LIQUIDITY_USD * 0.05). Restored pools only
became searchable again after the GeckoTerminal loop re-priced them at ~50 pools
per 60-second pass (crawler.py:746, 760-780) — hours on a production graph.

R1 review finding: the MI-102A tests warmed a fresh TokenGraph from the SAME
in-memory PoolCache object, which never exercised the disk round-trip. Every
test here now runs the full restart sequence:

  1. create a PoolCache in a temp directory
  2. insert representative V2/V3 records
  3. save()  -> 4. discard the cache object
  5. construct a fresh PoolCache on the same directory
  6. load() with a deterministic offline GraphClient stub (subgraph
     unavailable -> empty results -> update_pools keeps the persisted values)
  7. fresh TokenGraph + warm_graph()
  8. assert restored edge TVL and immediate route discoverability

(GraphClient.query swallows transport errors and get_pools_data returns [] on
failure, so the stub's empty results are exactly the real offline behaviour;
verified while writing these tests — no load-path defect to fix.)

Semantics preserved: the minimum-liquidity GATE stays on the warm-time estimate
(dust and genuinely unpriceable pools stay excluded — including ones carrying a
stale positive persisted TVL); only the value attached to the edge changes.
"""
import tempfile
import unittest
from unittest import mock

import cache as cache_mod
from cache import PoolCache
from graph import TokenGraph
from tvl import estimate_tvl_v2, estimate_tvl_v3, init_prices
import tvl as tvl_mod

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
A = "0x" + "11" * 20
B = "0x" + "22" * 20
UNK1 = "0x" + "66" * 20  # never priced: no metadata, no fallback entry
UNK2 = "0x" + "77" * 20
P1 = "0x" + "01" * 20
P2 = "0x" + "02" * 20
P3 = "0x" + "03" * 20

# sqrtPriceX96 for a 2.0 token1-per-token0 pool (18/18 decimals).
SQRT_P2 = int((2.0 ** 0.5) * (2 ** 96))


class OfflineGraphClientStub:
    """Deterministic offline stand-in for GraphClient: subgraph unreachable.

    update_pools() asks get_pools_data() per dex; empty results leave every
    persisted record untouched — the existing fallback semantics."""

    async def get_pools_data(self, dex_name: str, pool_addresses: list, intersect: bool = True):
        assert isinstance(dex_name, str)
        return []

    async def close(self):
        return None


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


def save_cache_to_disk(records) -> str:
    """Steps 1-4: create cache in a temp dir, insert records, save, discard."""
    tmp = tempfile.mkdtemp(prefix="f20_restart_")
    cache = PoolCache(chain_id=1, cache_dir=tmp)
    for addr, rec in records.items():
        cache.set_pool(addr, rec)
    cache.save()
    del cache  # the in-memory object must not survive into the restart
    return tmp


def restart_from_disk(directory):
    """Steps 5-7: fresh PoolCache on the same directory, load() via the offline
    stub, then a fresh TokenGraph warmed from the freshly loaded state."""
    fresh = PoolCache(chain_id=1, cache_dir=directory)
    with mock.patch.object(cache_mod, "GraphClient", OfflineGraphClientStub):
        loaded_ok = fresh.load()
    graph = TokenGraph(w3=None, cache=None, chain_id=1)
    tvl_mod.set_graph_ref(graph)
    warmed = fresh.warm_graph(graph)
    return fresh, graph, loaded_ok, warmed


class TestWarmGraphTVLRestore(unittest.TestCase):
    """All tests exercise the genuine disk save -> fresh load -> warm sequence."""

    def setUp(self):
        init_prices(2000.0, WETH, [])

    def test_v2_restore_prefers_persisted_tvl(self):
        """A persisted (authoritative) TVL survives save -> fresh load -> warm."""
        directory = save_cache_to_disk(
            {P1: _v2_record(WETH, A, 10**21, 2 * 10**21, 77_777.0)})
        cache, graph, loaded_ok, warmed = restart_from_disk(directory)
        self.assertTrue(loaded_ok)
        self.assertEqual(warmed, 1)
        self.assertEqual(len(cache._pools), 1, "fresh cache must load from disk")
        edges = graph.get_edges_for_pool(P1)
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0].tvl_usd, 77_777.0)
        self.assertEqual(edges[1].tvl_usd, 77_777.0)

    def test_v2_restore_falls_back_to_estimate(self):
        """Without a persisted value, the existing estimation path is used."""
        directory = save_cache_to_disk(
            {P1: _v2_record(WETH, A, 10**21, 2 * 10**21, 0.0)})
        _, graph, loaded_ok, warmed = restart_from_disk(directory)
        self.assertTrue(loaded_ok)
        self.assertEqual(warmed, 1)
        expected = estimate_tvl_v2(WETH.lower(), A.lower(), 10**21, 2 * 10**21, dex="UniswapV2")
        self.assertGreater(expected, 10_000.0, "fixture must clear the warm gate on its estimate")
        for edge in graph.get_edges_for_pool(P1):
            self.assertAlmostEqual(edge.tvl_usd, expected, places=4)

    def test_v3_restore_prefers_persisted_tvl(self):
        """V3 restored edges keep the persisted TVL rather than estimate-only."""
        directory = save_cache_to_disk(
            {P1: _v3_record(WETH, A, SQRT_P2, 10**21, 88_888.0)})
        _, graph, loaded_ok, warmed = restart_from_disk(directory)
        self.assertTrue(loaded_ok)
        self.assertEqual(warmed, 1)
        edges = graph.get_edges_for_pool(P1)
        self.assertEqual(len(edges), 2)
        for edge in edges:
            self.assertEqual(edge.sqrt_price_x96, SQRT_P2)
            self.assertEqual(edge.tvl_usd, 88_888.0)

    def test_restarted_graph_is_discoverable_without_repricing(self):
        """Requirement 6: after a genuine restart, a profitable route is found
        by find_cycles immediately — no GeckoTerminal re-pricing involved."""
        records = {
            P1: _v2_record(WETH, A, 10**21, 2 * 10**21, 50_000.0),   # rate 2.0
            P2: _v2_record(A, B, 2 * 10**24, 4 * 10**24, 50_000.0),  # rate 2.0
            P3: _v2_record(B, WETH, 4 * 10**24, int(0.26 * 10**24), 50_000.0),  # rate 0.26
        }
        _, graph, loaded_ok, warmed = restart_from_disk(save_cache_to_disk(records))
        self.assertTrue(loaded_ok)
        self.assertEqual(warmed, 3)
        cycles = graph.find_cycles(max_hops=3)
        route = [c for c in cycles if {p.lower() for p in c.pools} == {P1, P2, P3}]
        self.assertTrue(route, "profitable restored route must be discoverable right after restart")
        self.assertGreater(route[0].estimated_profit_ratio, 0.0)

    def test_unpriced_pools_remain_excluded(self):
        """Genuinely unpriceable pools (no persisted TVL, no price data) must
        not be misclassified as liquid: the estimate is 0 and the gate excludes
        them, exactly as before the fix."""
        cache, graph, loaded_ok, warmed = restart_from_disk(save_cache_to_disk(
            {P1: _v2_record(UNK1, UNK2, 10**21, 2 * 10**21, 0.0, with_meta=False)}))
        self.assertTrue(loaded_ok)
        self.assertEqual(warmed, 0)
        self.assertEqual(graph.pool_count, 0)
        self.assertTrue(cache._pools[P1].get("below_threshold"))

    def test_unpriced_pool_with_stale_positive_persisted_tvl_stays_excluded(self):
        """A positive but stale persisted TVL must not bypass the estimate-based
        gate: with no price data the warm estimate is 0 and the pool stays out."""
        cache, graph, loaded_ok, warmed = restart_from_disk(save_cache_to_disk(
            {P1: _v2_record(UNK1, UNK2, 10**21, 2 * 10**21, 50_000.0, with_meta=False)}))
        self.assertTrue(loaded_ok)
        self.assertEqual(warmed, 0)
        self.assertEqual(graph.pool_count, 0)
        self.assertEqual(cache._pools[P1].get("tvl_usd"), 50_000.0,
                         "stale persisted value stays in the cache, not on the graph")
        self.assertTrue(cache._pools[P1].get("below_threshold"))

    def test_dust_reserves_with_stale_persisted_tvl_remain_excluded(self):
        """The minimum-liquidity GATE stays on the warm-time estimate: a stale
        persisted TVL must not rescue a dust pool into the searchable graph."""
        cache, graph, loaded_ok, warmed = restart_from_disk(save_cache_to_disk(
            {P1: _v2_record(WETH, A, 10**12, 2 * 10**12, 50_000.0)}))
        self.assertTrue(loaded_ok)
        self.assertEqual(warmed, 0)
        self.assertEqual(graph.pool_count, 0)
        self.assertTrue(cache._pools[P1].get("below_threshold"))

    def test_v3_estimate_fallback_removes_nothing(self):
        """V3 without a persisted value keeps attaching the existing estimate."""
        directory = save_cache_to_disk(
            {P1: _v3_record(WETH, A, SQRT_P2, 10**21, 0.0)})
        _, graph, loaded_ok, warmed = restart_from_disk(directory)
        self.assertTrue(loaded_ok)
        self.assertEqual(warmed, 1)
        expected = estimate_tvl_v3(WETH.lower(), A.lower(), SQRT_P2, 10**21, dex="UniswapV3")
        self.assertGreater(expected, 10_000.0, "fixture must clear the warm gate on its estimate")
        for edge in graph.get_edges_for_pool(P1):
            self.assertAlmostEqual(edge.tvl_usd, expected, places=4)


if __name__ == "__main__":
    unittest.main()
