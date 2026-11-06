import unittest
from log import get_logger
from graph import TokenGraph, PoolEdge

log = get_logger("test.cycles")

class TestCycleDetection(unittest.TestCase):
    def test_triangular_arbitrage(self):
        # Setup: A -> B -> C -> A
        # A: WETH, B: USDC, C: DAI
        # Rates (incl fee): 1 WETH = 3000 USDC, 1 USDC = 1.01 DAI, 1 DAI = 1/3000 WETH
        # If we get > 1 WETH back, it's arb.
        # Let's make it 1 DAI = 1/2900 WETH -> Huge profit
        
        graph = TokenGraph()

        # All pools carry realistic TVL: find_cycles prunes edges below
        # MIN_POOL_LIQUIDITY_USD * 0.05 (currently $500) as a paper-profit
        # guard, and PoolEdge defaults to tvl_usd=0.0.
        TVL = 250_000.0

        # Edge 1: WETH -> USDC (3000)
        e1a = PoolEdge("pool1", "UniV2", "WETH", "USDC", 10**18, 3000 * 10**6, 30, 2, tvl_usd=TVL)
        e1b = PoolEdge("pool1", "UniV2", "USDC", "WETH", 3000 * 10**6, 10**18, 30, 2, tvl_usd=TVL)

        # Edge 2: USDC -> DAI (1.01)
        e2a = PoolEdge("pool2", "UniV2", "USDC", "DAI", 10**6, int(1.01 * 10**18), 30, 2, tvl_usd=TVL)
        e2b = PoolEdge("pool2", "UniV2", "DAI", "USDC", int(1.01 * 10**18), 10**6, 30, 2, tvl_usd=TVL)

        # Edge 3: DAI -> WETH (1/2900)
        e3a = PoolEdge("pool3", "UniV2", "DAI", "WETH", 2900 * 10**18, 10**18, 30, 2, tvl_usd=TVL)
        e3b = PoolEdge("pool3", "UniV2", "WETH", "DAI", 10**18, 2900 * 10**18, 30, 2, tvl_usd=TVL)
        
        graph.add_or_update_pool(e1a, e1b)
        graph.add_or_update_pool(e2a, e2b)
        graph.add_or_update_pool(e3a, e3b)
        
        cycles = graph.find_cycles(source_tokens=["WETH"], max_hops=3)
        
        self.assertTrue(len(cycles) > 0)
        best = cycles[0]
        self.assertEqual(best.hop_count, 3)
        # Expected tokens in path (from WETH)
        self.assertEqual(best.tokens[0], "WETH")
        self.assertGreater(best.estimated_profit_ratio, 0.0)
        log.research(f"Found triangular arb. Tokens: {best.tokens} | Profit: {best.estimated_profit_ratio*100:.2f}%")

if __name__ == "__main__":
    unittest.main()
