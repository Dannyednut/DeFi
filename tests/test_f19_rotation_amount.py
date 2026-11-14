"""F-19 regression (DSS-WO-MI-102A): ``ArbitrageCycle.rotate_to_best_start`` must
preserve the complete cycle state — in particular ``amount_in_wei`` — so rotated
cycles survive the Python fallback quote path.

Original failure at the ENG-017 baseline: rotation mutated the cycle in place and
then returned a reconstructed copy that omitted ``amount_in_wei``, so
``_simulate_slippage_profit(cycle, 0)`` returned profit 0 and every rotated cycle
was silently dropped in ``CycleDetector.simulate_and_build``
(detectors/cycles.py:184, 231-241; graph.py:260-274).

The contract-batch path is independent of this field (``_encode_yieldOut``
hardcodes ``amountIn: 0`` for on-chain auto-optimal quoting) and the mempool
simulator computes its own probe; both are covered below to pin that invariant.
"""
import unittest
from unittest import mock

from detectors import base as detectors_base
from detectors import cycles as cycles_mod
from graph import ArbitrageCycle, PoolEdge, TokenGraph
from tvl import init_prices

# Real chain-1 wrapped-native so rotation scoring (_WETH_ADDRS, populated by
# init_prices) ranks it above the long-tail tokens.
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
A = "0x" + "11" * 20
B = "0x" + "22" * 20
P1 = "0x" + "01" * 20  # WETH/A, rate 2.0
P2 = "0x" + "02" * 20  # A/B,   rate 2.0
P3 = "0x" + "03" * 20  # B/WETH, rate 0.26 — the only profitable direction
# of the {P1,P2,P3} loop is B -> WETH -> A -> B (product ~1.032 after fees),
# so the surviving cycle is rooted at a long-tail token and needs rotation.


def _edge(pool, dex, t_in, t_out, r_in, r_out, tvl=250_000.0):
    return PoolEdge(
        pool_address=pool, dex_name=dex, token_in=t_in.lower(), token_out=t_out.lower(),
        reserve_in=r_in, reserve_out=r_out, fee_bps=30, version=2, tvl_usd=tvl,
        protocol="uniswap_v2", pool_token0=min(t_in.lower(), t_out.lower()),
        pool_token1=max(t_in.lower(), t_out.lower()),
    )


class StubEth:
    gas_price = 30 * 10**9

    def get_block(self, n):
        return {"hash": str(n).rjust(30, "0").encode()}


class StubW3:
    eth = StubEth()


def build_graph():
    g = TokenGraph()
    g.add_or_update_pool(_edge(P1, "UniswapV2", WETH, A, 10**21, 2 * 10**21),
                         _edge(P1, "UniswapV2", A, WETH, 2 * 10**21, 10**21))
    g.add_or_update_pool(_edge(P2, "SushiswapV2", A, B, 2 * 10**24, 4 * 10**24),
                         _edge(P2, "SushiswapV2", B, A, 4 * 10**24, 2 * 10**24))
    g.add_or_update_pool(_edge(P3, "PancakeV2", B, WETH, 4 * 10**24, 0.26 * 10**24),
                         _edge(P3, "PancakeV2", WETH, B, 0.26 * 10**24, 4 * 10**24))
    return g


def build_b_rooted_cycle():
    """The profitable direction of the {P1,P2,P3} loop, starting at long-tail B."""
    c = ArbitrageCycle(
        tokens=[B, WETH, A, B],
        pools=[P3, P1, P2],
        dexes=["PancakeV2", "UniswapV2", "SushiswapV2"],
        fees=[30, 30, 30],
        versions=[2, 2, 2],
        log_weight_sum=-0.0315,
        estimated_profit_ratio=0.032,
        hop_count=3,
        is_cross_dex=True,
    )
    c.amount_in_wei = 5 * 10**15
    return c


class TestRotationPreservesState(unittest.TestCase):
    def setUp(self):
        init_prices(2000.0, WETH, [])
        self.graph = build_graph()

    def test_rotation_preserves_amount_and_route_identity(self):
        cycle = build_b_rooted_cycle()
        rotated = cycle.rotate_to_best_start(self.graph)

        # The in-place rotation already carries the full state; no reconstruction.
        self.assertIs(rotated, cycle)
        # F-19 core invariant: the non-zero input amount survives rotation.
        self.assertEqual(rotated.amount_in_wei, 5 * 10**15)
        # Best start (wrapped native) is now first, cycle properly closed.
        self.assertEqual([t.lower() for t in rotated.tokens],
                         [WETH.lower(), A.lower(), B.lower(), WETH.lower()])
        self.assertEqual([p.lower() for p in rotated.pools],
                         [P1.lower(), P2.lower(), P3.lower()])
        # Every hop still resolves to a real graph edge: pool ordering stayed
        # consistent with token ordering.
        for i in range(rotated.hop_count):
            edge = self.graph.get_edge_by_pool_and_tokens(
                rotated.pools[i], rotated.tokens[i], rotated.tokens[i + 1])
            self.assertIsNotNone(edge, f"hop {i} broken after rotation")
            self.assertEqual(edge.fee_bps, rotated.fees[i])
        # Cycle-level metadata untouched by rotation.
        self.assertEqual(rotated.log_weight_sum, cycle.log_weight_sum)
        self.assertEqual(rotated.estimated_profit_ratio, cycle.estimated_profit_ratio)
        self.assertEqual(rotated.hop_count, 3)
        self.assertTrue(rotated.is_cross_dex)

    def test_rotation_noop_keeps_cycle_unchanged(self):
        cycle = ArbitrageCycle(
            tokens=[WETH, A, B, WETH],
            pools=[P1, P2, P3],
            dexes=["UniswapV2", "SushiswapV2", "PancakeV2"],
            fees=[30, 30, 30],
            versions=[2, 2, 2],
            log_weight_sum=-0.0315,
            estimated_profit_ratio=0.032,
            hop_count=3,
            is_cross_dex=True,
        )
        cycle.amount_in_wei = 7 * 10**15
        rotated = cycle.rotate_to_best_start(self.graph)
        self.assertIs(rotated, cycle)
        self.assertEqual(rotated.amount_in_wei, 7 * 10**15)
        self.assertEqual([t.lower() for t in rotated.tokens][0], WETH.lower())

    def test_rotation_preserves_lifecycle_fields(self):
        """Fields the mempool simulator relies on survive rotation too."""
        cycle = build_b_rooted_cycle()
        cycle.simulated_via_contract = True
        cycle.net_profit_usd = 12.5
        cycle.estimated_gas_usd = 3.3
        cycle.is_cross_chain = False
        rotated = cycle.rotate_to_best_start(self.graph)
        self.assertIs(rotated, cycle)
        self.assertTrue(rotated.simulated_via_contract)
        self.assertEqual(rotated.net_profit_usd, 12.5)
        self.assertEqual(rotated.estimated_gas_usd, 3.3)
        self.assertFalse(rotated.is_cross_chain)


class TestFallbackQuotePath(unittest.TestCase):
    """End-to-end through CycleDetector with contract quoting disabled: the
    rotated, long-tail-rooted cycle must produce a candidate (0 at baseline)."""

    def test_rotated_cycle_survives_fallback_quote_path(self):
        init_prices(2000.0, WETH, [])
        graph = build_graph()
        with mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""), \
                mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""), \
                mock.patch.object(cycles_mod, "MIN_PROFIT_USD", 0.0):
            detector = cycles_mod.CycleDetector(StubW3(), graph, None, None, 1)
            opportunities = detector.on_block(100, touched_pools=frozenset({P3}))

        self.assertEqual(len(opportunities), 1,
                         "rotated long-tail-rooted cycle must survive the fallback quote path")
        opp = opportunities[0]
        self.assertEqual({p.lower() for p in opp.pools}, {P1, P2, P3})
        self.assertGreater(opp.net_profit_usd, 0.0)

    def test_contract_batch_path_is_amount_independent(self):
        """The contract batch path never consumed amount_in_wei (amountIn is
        hardcoded to 0 = on-chain auto-optimal), so the fix cannot affect it."""
        from utils.multicall3 import _encode_yieldOut
        from web3 import Web3

        w3 = Web3()
        c_low = build_b_rooted_cycle()
        c_low.amount_in_wei = 0
        c_high = build_b_rooted_cycle()
        c_high.amount_in_wei = 123 * 10**18
        self.assertEqual(_encode_yieldOut(w3, c_low), _encode_yieldOut(w3, c_high),
                         "yieldOut calldata must not depend on amount_in_wei")


if __name__ == "__main__":
    unittest.main()
