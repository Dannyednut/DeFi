"""F-19 regression (DSS-WO-MI-102A + R1): the detector pipeline must size the
nominal USD probe on the FINAL rotated starting token, and rotation must not
corrupt route identity or lifecycle state.

MI-102A original failure: rotate_to_best_start returned a reconstructed copy
that dropped amount_in_wei entirely, so rotated cycles simulated with a 0 input
amount (fixed in MI-102A by returning self).

R1 review finding: the pipeline still ran set_amount_in_wei() BEFORE rotation,
so the sized integer stayed denominated in the PREVIOUS starting token — e.g. a
$10 probe on a 6-decimal stable (10,000,000 base units) was then interpreted as
WETH-wei (≈ $0.00002 at $2000/WETH, ~500,000x off). The corrected pipeline
rotates first, then sizes the probe on the actual starting token:
rotate_to_best_start() -> set_amount_in_wei() -> simulate_cycle().

The contract-batch path is independent of this field (_encode_yieldOut
hardcodes amountIn: 0 = on-chain auto-optimal quoting) and the mempool
simulator already sizes its own TEST_AMOUNT after rotation; both are covered
below to pin those invariants.
"""
import unittest
from unittest import mock

from detectors import base as detectors_base
from detectors import cycles as cycles_mod
from graph import ArbitrageCycle, PoolEdge, TokenGraph
from tvl import init_prices

# Real chain-1 wrapped-native and a real 6-decimal stable: rotation scoring
# (_WETH_ADDRS / STABLE_TOKEN_SETS, populated by init_prices) ranks WETH above
# stables above long-tail metadata tokens.
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"   # 6 decimals, $1.00
B18 = "0x" + "44" * 20                                 # 18 decimals, $50.00
A = "0x" + "11" * 20
P1 = "0x" + "01" * 20  # WETH/A
P2 = "0x" + "02" * 20  # A/root
P3 = "0x" + "03" * 20  # root/WETH — only profitable direction

WETH_PRICE = 2000.0


def probe_wei(price_usd: float, decimals: int, target_usd: float = 10.0) -> int:
    """Nominal probe in base units, matching set_amount_in_wei()."""
    return int((target_usd / price_usd) * (10 ** decimals))


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


def build_profitable_graph(root, root_price, root_decimals):
    """{P3, P1, P2} loop whose only profitable direction is root -> WETH -> A
    -> root (product ~1.041 after fees). Per-wei rates are price-consistent:
    1 root token = root_price USD = root_price/2000 WETH; A is $1000. The loop
    result is independent of the root token's decimals and price."""
    g = TokenGraph()
    g.token_metadata[B18.lower()] = {"symbol": "B18", "decimals": 18, "usd_price": 50.0}

    r_root_to_weth = (root_price / WETH_PRICE) * 10 ** (18 - root_decimals)
    r_a_to_root = (1000.0 / root_price) * 10 ** (root_decimals - 18) * 1.05

    # P3: root -> WETH (1M root tokens in)
    g.add_or_update_pool(
        _edge(P3, "PancakeV2", root, WETH, 10**6 * 10**root_decimals,
              int(10**6 * 10**root_decimals * r_root_to_weth)),
        _edge(P3, "PancakeV2", WETH, root,
              int(10**6 * 10**root_decimals * r_root_to_weth), 10**6 * 10**root_decimals))
    # P1: WETH -> A (rate 2.0)
    g.add_or_update_pool(
        _edge(P1, "UniswapV2", WETH, A, 10**21, 2 * 10**21),
        _edge(P1, "UniswapV2", A, WETH, 2 * 10**21, 10**21))
    # P2: A -> root (rate r_a_to_root, +5% vs breakeven)
    g.add_or_update_pool(
        _edge(P2, "SushiswapV2", A, root, 10**24, int(10**24 * r_a_to_root)),
        _edge(P2, "SushiswapV2", root, A, int(10**24 * r_a_to_root), 10**24))
    return g


def build_cycle(root):
    return ArbitrageCycle(
        tokens=[root, WETH, A, root],
        pools=[P3, P1, P2],
        dexes=["PancakeV2", "UniswapV2", "SushiswapV2"],
        fees=[30, 30, 30],
        versions=[2, 2, 2],
        log_weight_sum=-0.0315,
        estimated_profit_ratio=0.032,
        hop_count=3,
        is_cross_dex=True,
    )


def assert_close(self, amount, expected, msg=None):
    """Within integer rounding of the intended probe (float truncation slack),
    but far tighter than any cross-denomination error (~5 orders of magnitude)."""
    self.assertLessEqual(abs(amount - expected), max(1, expected // 10**12), msg)


class TestRotationRouteIdentity(unittest.TestCase):
    """Rotation is a pure re-ordering: route identity invariants hold and the
    raw integer is not mutated by rotation itself (denomination is the
    pipeline's responsibility, tested in TestProbeDenomination)."""

    def setUp(self):
        init_prices(WETH_PRICE, WETH, [USDC])
        self.graph = build_profitable_graph(USDC, 1.0, 6)

    def test_rotation_preserves_route_identity(self):
        cycle = build_cycle(USDC)
        rotated = cycle.rotate_to_best_start(self.graph)

        # The in-place rotation already carries the full state; no reconstruction.
        self.assertIs(rotated, cycle)
        self.assertEqual([t.lower() for t in rotated.tokens],
                         [WETH.lower(), A.lower(), USDC.lower(), WETH.lower()])
        self.assertEqual([p.lower() for p in rotated.pools],
                         [P1.lower(), P2.lower(), P3.lower()])
        for i in range(rotated.hop_count):
            edge = self.graph.get_edge_by_pool_and_tokens(
                rotated.pools[i], rotated.tokens[i], rotated.tokens[i + 1])
            self.assertIsNotNone(edge, f"hop {i} broken after rotation")
            self.assertEqual(edge.fee_bps, rotated.fees[i])
        self.assertEqual(rotated.log_weight_sum, cycle.log_weight_sum)
        self.assertEqual(rotated.estimated_profit_ratio, cycle.estimated_profit_ratio)
        self.assertEqual(rotated.hop_count, 3)
        self.assertTrue(rotated.is_cross_dex)

    def test_start_unchanged_keeps_cycle_unchanged(self):
        cycle = ArbitrageCycle(
            tokens=[WETH, A, USDC, WETH],
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
        self.assertEqual(rotated.amount_in_wei, 7 * 10**15)  # same token, same integer
        self.assertEqual([t.lower() for t in rotated.tokens][0], WETH.lower())

    def test_rotation_preserves_lifecycle_fields(self):
        """Fields the mempool simulator relies on survive rotation too."""
        cycle = build_cycle(USDC)
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


class TestProbeDenomination(unittest.TestCase):
    """R1 core invariant: after the pipeline (rotate -> size), the amount is
    denominated in the FINAL starting token."""

    def setUp(self):
        init_prices(WETH_PRICE, WETH, [USDC])

    def test_6dec_root_rotated_to_weth_is_sized_for_weth(self):
        graph = build_profitable_graph(USDC, 1.0, 6)
        cycle = build_cycle(USDC)
        # Pipeline parity: rotate first, then size (post-R1 order).
        rotated = cycle.rotate_to_best_start(graph)
        rotated.set_amount_in_wei()

        self.assertEqual(rotated.tokens[0].lower(), WETH.lower())
        assert_close(self, rotated.amount_in_wei, probe_wei(WETH_PRICE, 18),
                     "$10 probe must be sized for WETH (18 dec, $2000), not for USDC")
        # Discriminator: the pre-R1 order produced the USDC-sized integer.
        self.assertNotEqual(rotated.amount_in_wei, probe_wei(1.0, 6))

    def test_18dec_50usd_root_rotated_to_weth_is_sized_for_weth(self):
        graph = build_profitable_graph(B18, 50.0, 18)
        cycle = build_cycle(B18)
        rotated = cycle.rotate_to_best_start(graph)
        rotated.set_amount_in_wei()

        self.assertEqual(rotated.tokens[0].lower(), WETH.lower())
        assert_close(self, rotated.amount_in_wei, probe_wei(WETH_PRICE, 18),
                     "$10 probe must be sized for WETH, not for the $50 18-dec token")
        self.assertNotEqual(rotated.amount_in_wei, probe_wei(50.0, 18))

    def test_start_unchanged_is_sized_for_its_own_token(self):
        graph = build_profitable_graph(B18, 50.0, 18)
        cycle = ArbitrageCycle(
            tokens=[WETH, A, B18, WETH],
            pools=[P1, P2, P3],
            dexes=["UniswapV2", "SushiswapV2", "PancakeV2"],
            fees=[30, 30, 30],
            versions=[2, 2, 2],
            log_weight_sum=-0.0315,
            estimated_profit_ratio=0.032,
            hop_count=3,
            is_cross_dex=True,
        )
        rotated = cycle.rotate_to_best_start(graph)  # no-op: WETH already best
        rotated.set_amount_in_wei()
        self.assertIs(rotated, cycle)
        assert_close(self, rotated.amount_in_wei, probe_wei(WETH_PRICE, 18),
                     "start token unchanged -> probe denominated in that token")


class TestFallbackQuotePath(unittest.TestCase):
    """End-to-end through CycleDetector with contract quoting disabled."""

    def test_detector_sizes_probe_for_final_start_token(self):
        """R1 red/green discriminator: the amount handed to the simulator must
        correspond to the nominal $10 probe at the final starting token."""
        init_prices(WETH_PRICE, WETH, [USDC])
        graph = build_profitable_graph(USDC, 1.0, 6)
        captured = []
        original = cycles_mod.CycleDetector.simulate_cycle

        def spy(self_det, cycle, amount_in_wei):
            captured.append((cycle.tokens[0].lower(), amount_in_wei))
            return original(self_det, cycle, amount_in_wei)

        with mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""), \
                mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""), \
                mock.patch.object(cycles_mod, "MIN_PROFIT_USD", 0.0), \
                mock.patch.object(cycles_mod.CycleDetector, "simulate_cycle", spy):
            detector = cycles_mod.CycleDetector(StubW3(), graph, None, None, 1)
            opportunities = detector.on_block(100, touched_pools=frozenset({P3}))

        self.assertEqual(len(opportunities), 1)
        self.assertEqual(captured, [(WETH.lower(), probe_wei(WETH_PRICE, 18))],
                         "simulator must receive the $10 WETH-denominated probe "
                         "for the rotated cycle, not the pre-rotation integer")
        self.assertGreater(opportunities[0].net_profit_usd, 0.0)

    def test_rotated_cycle_survives_fallback_quote_path(self):
        init_prices(WETH_PRICE, WETH, [USDC])
        graph = build_profitable_graph(USDC, 1.0, 6)
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
        c_low = build_cycle(USDC)
        c_low.amount_in_wei = 0
        c_high = build_cycle(USDC)
        c_high.amount_in_wei = 123 * 10**18
        self.assertEqual(_encode_yieldOut(w3, c_low), _encode_yieldOut(w3, c_high),
                         "yieldOut calldata must not depend on amount_in_wei")


if __name__ == "__main__":
    unittest.main()
