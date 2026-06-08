import unittest
from log import get_logger
from unittest.mock import MagicMock, patch
from mempool.pipeline import MempoolPipeline
from graph import TokenGraph, PoolEdge
from mempool.decoder import DecodedSwap
import config

log = get_logger("test.integration")

class TestIntegrationArbDiscovery(unittest.TestCase):
    def setUp(self):
        self.w3 = MagicMock()
        self.graph = TokenGraph()
        self.tracker = MagicMock()
        self.cache = MagicMock()
        
        # Initialize pipeline with mocks
        self.pipeline = MempoolPipeline(self.w3, self.graph, self.tracker, cache=self.cache)

    @patch('registry.get_factory_by_name')
    @patch('registry._load_v2_pair')
    @patch('registry.CHAIN_ID', 1)
    def test_pipeline_discovers_and_simulates(self, mock_load, mock_get_factory):
        import asyncio
        # 1. Setup - swap through a pool NOT in graph
        token1 = "0x" + "1"*40
        token2 = "0x" + "2"*40
        pool_addr = "0x" + "3"*40
        
        # We need a dict because _process_tx decodes it
        tx = {
            "to": "0xRouter",
            "from": "0xSender",
            "hash": "0xhash",
            "input": b"dummy", # Decoder is mocked anyway
            "gasPrice": 20*10**9
        }
        
        # Mocking finding factory and pair
        mock_get_factory.return_value = "0xFactory"
        factory = MagicMock()
        self.w3.eth.contract.return_value = factory
        factory.functions.getPair.return_value.call.return_value = pool_addr
        
        # Mock decoder to return a swap
        swap = DecodedSwap(
            tx_hash="0xhash", router="0xRouter", router_name="UniswapV2",
            version=2, function_name="swapExactTokensForTokens",
            token_path=[token1, token2], amount_in=1000 * 10**18,
            amount_out_min=900 * 10**18, amount_in_is_exact=True,
            sender="0xSender", gas_price=20*10**9
        )
        self.pipeline._decoder.decode = MagicMock(return_value=swap)
        
        # Mock load valid pool
        edge_a = PoolEdge(pool_addr, "UniswapV2", token1, token2, 10**24, 10**24, 30, 2, tvl_usd=100000)
        edge_b = PoolEdge(pool_addr, "UniswapV2", token2, token1, 10**24, 10**24, 30, 2, tvl_usd=100000)
        mock_load.return_value = (edge_a, edge_b)
        
        # 2. Add an existing competitive pool to graph to ensure Arb can be found
        # token1 -> token2 at a slightly different price
        # Pool price 1.0 (1:1)
        # New pool price 1.0, but let's make the existing pool token2 -> token1 at 1.1:1
        existing_pool = "0x4444"
        ea_ex = PoolEdge(existing_pool, "SushiswapV2", token2, token1, 10**24, int(1.1 * 10**24), 30, 2, tvl_usd=100000)
        eb_ex = PoolEdge(existing_pool, "SushiswapV2", token1, token2, int(1.1 * 10**24), 10**24, 30, 2, tvl_usd=100000)
        self.graph.add_or_update_pool(ea_ex, eb_ex)

        # 3. Execution - Process tx through pipeline
        asyncio.run(self.pipeline._process_tx(tx))
        
        # 4. Validation
        # Check pool was discovered and added
        self.assertIn(pool_addr, self.graph._pool_index)
        
        # Check edges were created correctly
        edges = self.graph.get_edges_for_pair(token1, token2)
        self.assertTrue(any(e.pool_address == pool_addr for e in edges))
        
        log.info(f"Successfully discovered pool {pool_addr[:8]} and injected into graph.")

if __name__ == "__main__":
    unittest.main()
