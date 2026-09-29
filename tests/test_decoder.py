import unittest
from unittest.mock import MagicMock
from mempool.decoder import SwapDecoder, build_router_map

class TestSwapDecoder(unittest.TestCase):
    def setUp(self):
        # Ethereum Chain ID 1
        router_map = {
            "0x7a250d5630b4cf539739df2c5dacb4c659f2488d".lower(): ("UniswapV2", 2),
            "0xe592427a0aece92de3edee1f18e0157c05861564".lower(): ("UniswapV3", 3),
        }
        self.decoder = SwapDecoder(router_map)

    def test_decode_v2_exact_tokens_for_tokens(self):
        # swapExactTokensForTokens(uint256,uint256,address[],address,uint256)
        # Selector: 0x38ed1739
        # Sample calldata (truncated/hand-crafted for test)
        # amountIn: 1000, amountOutMin: 900, path: [token1, token2]
        token1 = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        token2 = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        
        # 4 bytes selector + 5 * 32 bytes data
        # Note: address[] is dynamic, needs offset
        from eth_abi import encode
        data = encode(["uint256", "uint256", "address[]", "address", "uint256"], 
                      [1000, 900, [token1, token2], "0x" + "1"*40, 9999999999])
        calldata = bytes.fromhex("38ed1739") + data
        
        tx = {
            "to": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
            "input": calldata,
            "from": "0x" + "2"*40,
            "hash": "0x" + "3"*64,
            "gasPrice": 20000000000
        }
        
        swap = self.decoder.decode(tx)
        self.assertIsNotNone(swap)
        self.assertEqual(swap.router_name, "UniswapV2")
        self.assertEqual(swap.version, 2)
        self.assertEqual(swap.amount_in, 1000)
        self.assertEqual(swap.token_path, [token1.lower(), token2.lower()])

    def test_decode_v3_exact_input_single(self):
        # exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))
        # Selector: 0x414bf389
        token1 = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        token2 = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        
        from eth_abi import encode
        # struct params: tokenIn, tokenOut, fee, recipient, deadline, amountIn, amountOutMin, sqrtPriceLimitX96
        params_data = encode(["address", "address", "uint24", "address", "uint256", "uint256", "uint256", "uint160"],
                             [token1, token2, 3000, "0x" + "1"*40, 9999999999, 1000, 900, 0])
        calldata = bytes.fromhex("414bf389") + params_data
        
        tx = {
            "to": "0xe592427a0aece92de3edee1f18e0157c05861564",
            "input": calldata,
            "from": "0x" + "2"*40,
            "hash": "0x" + "3"*64,
            "gasPrice": 20000000000
        }
        
        swap = self.decoder.decode(tx)
        self.assertIsNotNone(swap)
        self.assertEqual(swap.router_name, "UniswapV3")
        self.assertEqual(swap.version, 3)
        self.assertEqual(swap.token_path, [token1.lower(), token2.lower()])
        self.assertEqual(swap.v3_fees, [3000])

if __name__ == "__main__":
    unittest.main()
