import unittest
from unittest.mock import MagicMock, patch
from registry import discover_from_swap
from graph import TokenGraph, PoolEdge
from mempool.decoder import DecodedSwap

class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.w3 = MagicMock()
        self.graph = TokenGraph()
        self.cache = MagicMock()
        # Mock CHAIN_ID in local config
        import config
        config.CHAIN_ID = 1

    @patch('registry._load_v2_pair')
    def test_discover_v2(self, mock_load):
        # Setup mock factory and discovered pair
        token1 = "0x" + "1"*40
        token2 = "0x" + "2"*40
        pool_addr = "0x" + "3"*40
        
        # Router map Mock
        # registry.get_factory_by_name(1, "UniswapV2") -> return factory
        factory_addr = "0xFactory"
        
        # Decoded swap
        swap = DecodedSwap(
            tx_hash="0xhash", router="0xRouter", router_name="UniswapV2",
            version=2, function_name="swapExactTokensForTokens",
            token_path=[token1, token2], amount_in=1000,
            amount_out_min=900, amount_in_is_exact=True,
            sender="0xSender", gas_price=20*10**9
        )
        
        # Mock Get factory address
        with patch('registry.get_factory_by_name', return_value=factory_addr):
            # Mock Factory contract
            factory = MagicMock()
            self.w3.eth.contract.return_value = factory
            factory.functions.getPair.return_value.call.return_value = pool_addr
            
            # Mock _load_v2_pair return val
            edge_a = PoolEdge(pool_addr, "UniswapV2", token1, token2, 1000, 1000, 30, 2, tvl_usd=100000)
            edge_b = PoolEdge(pool_addr, "UniswapV2", token2, token1, 1000, 1000, 30, 2, tvl_usd=100000)
            mock_load.return_value = (edge_a, edge_b)
            
            discover_from_swap(self.w3, self.graph, self.cache, swap)
            
            # Verify graph was updated
            edges = self.graph.get_edges_from(token1)
            self.assertTrue(any(e.pool_address == pool_addr for e in edges))
            self.cache.set_pool.assert_called()

if __name__ == "__main__":
    unittest.main()
