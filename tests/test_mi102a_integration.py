"""DSS-WO-MI-102A integration: F-19 + F-20 together.

A cache is restored after restart (F-20 path); the restored graph contains a
profitable route whose surviving cycle is rooted at a long-tail token (F-19
path); the canonical OpportunityEngine must produce a candidate through the
Python fallback quote path — with no GeckoTerminal re-pricing and no contract
quoting involved.

Baseline behaviour (both defects present): 0 candidates — the restored graph was
DFS-pruned for lack of TVL (F-20), and the rotated cycle lost its input amount
(F-19). Either defect alone is sufficient to lose the candidate.
"""
import unittest
from unittest import mock

from cache import PoolCache
from graph import TokenGraph
from opportunity_engine import OpportunityEngine
from detectors import base as detectors_base
from detectors import cycles as cycles_mod
from tvl import init_prices
import tvl as tvl_mod
import validation as validation_mod

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
A = "0x" + "11" * 20
B = "0x" + "22" * 20
P1 = "0x" + "01" * 20  # WETH/A, rate 2.0
P2 = "0x" + "02" * 20  # A/B,   rate 2.0
P3 = "0x" + "03" * 20  # B/WETH, rate 0.26


class StubEth:
    gas_price = 30 * 10**9

    def get_block(self, n):
        return {"hash": str(n).rjust(30, "0").encode()}


class StubW3:
    eth = StubEth()


def _rec(t0, t1, r0, r1, tvl_usd):
    derived = {WETH.lower(): 1.0, A.lower(): 0.5, B.lower(): 0.25}
    return {
        "token0": t0.lower(), "token1": t1.lower(),
        "dex": "UniswapV2", "protocol": "uniswap_v2", "version": 2,
        "fee_bps": 30, "r0": r0, "r1": r1, "last_updated": 1.0,
        "state_block": 99, "state_block_hash": "0x" + "ab" * 32,
        "state_source": "block_information", "state_status": "FRESH",
        "tvl_usd": tvl_usd,
        "token0_meta": {"symbol": t0[2:6], "decimals": 18, "derivedETH": derived.get(t0.lower(), 0.0)},
        "token1_meta": {"symbol": t1[2:6], "decimals": 18, "derivedETH": derived.get(t1.lower(), 0.0)},
    }


class TestRestartedDiscoveryEndToEnd(unittest.TestCase):
    def test_candidate_built_from_restarted_graph_via_fallback_quote(self):
        init_prices(2000.0, WETH, [])
        import tempfile
        cache = PoolCache(chain_id=1, cache_dir=tempfile.mkdtemp(prefix="mi102a_"))
        cache.set_pool(P1, _rec(WETH, A, 10**21, 2 * 10**21, 50_000.0))
        cache.set_pool(P2, _rec(A, B, 2 * 10**24, 4 * 10**24, 50_000.0))
        cache.set_pool(P3, _rec(B, WETH, 4 * 10**24, int(0.26 * 10**24), 50_000.0))
        cache.save()

        graph = TokenGraph(w3=None, cache=None, chain_id=1)
        tvl_mod.set_graph_ref(graph)
        self.assertEqual(cache.warm_graph(graph), 3)

        with mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""), \
                mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""), \
                mock.patch.object(cycles_mod, "MIN_PROFIT_USD", 0.0), \
                mock.patch.object(validation_mod, "validate_many",
                                  lambda w3, opps, chain_id, **kw: list(opps)):
            detector = cycles_mod.CycleDetector(StubW3(), graph, None, None, 1)
            engine = OpportunityEngine(w3=StubW3(), chain_id=1, detectors=[detector], graph=graph)

            quiet = engine.process(block_number=100, touched_pools=frozenset())
            result = engine.process(block_number=101, touched_pools=frozenset({P3}))

        # Scheduler semantics intact: event-gated detector skips the no-event block.
        self.assertEqual(len(quiet.candidates), 0)
        # Restored graph + rotated long-tail-rooted cycle -> one candidate.
        self.assertEqual(len(result.candidates), 1)
        opp = result.candidates[0]
        self.assertEqual({p.lower() for p in opp.pools}, {P1, P2, P3})
        self.assertGreater(opp.net_profit_usd, 0.0)
        self.assertIn("route_state_status", opp.metadata)


if __name__ == "__main__":
    unittest.main()
