import sys
import os
import asyncio
import logging

logging.basicConfig(level=logging.DEBUG)

from web3 import Web3

# Import from project
from config import DEFAULT_RPCS
from utils.multicall3 import multicall3_bulk_refresh, _get_multicall3, _encode_get_reserves, _decode_get_reserves

# Mock classes to pass to multicall3_bulk_refresh
class MockPoolEdge:
    def __init__(self, addr):
        self.address = addr
        # assign version based on address for test
        self.version = 3 if "8ad5" in addr.lower() else 2

class MockTokenGraph:
    def get_pool(self, addr):
        return MockPoolEdge(addr)

class MockPoolCache:
    def update_reserves(self, addr, r0=0, r1=0, sqrt_price_x96=0, tick=0, liquidity=0):
        print(f"CACHE UPDATED: {addr} | r0:{r0} r1:{r1} sqrt:{sqrt_price_x96} liquidity:{liquidity}")

def run_test():
    w3 = Web3(Web3.HTTPProvider("https://0xrpc.io/eth"))
    
    # Check if Multicall3 is accessible
    mc = _get_multicall3(w3)
    print("Multicall3 contract created:", mc.address)
    
    # USDC/WETH V2 Pair
    usdc_weth_v2 = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
    # WBTC/WETH V2 Pair
    wbtc_weth_v2 = "0xBb2b8038a1640196FbE3e38816F3e67Cba72D940"
    # USDC/WETH V3 Pool
    usdc_weth_v3 = "0x8ad599c3A0ff1De082011EFDDc58f1908eb6e6D8"
    
    pools = {usdc_weth_v2, wbtc_weth_v2, usdc_weth_v3}
    
    graph = MockTokenGraph()
    cache = MockPoolCache()
    
    import utils.multicall3
    utils.multicall3.log.setLevel(logging.DEBUG)
    utils.multicall3.log.addHandler(logging.StreamHandler(sys.stdout))

    print(f"Running bulk refresh on {len(pools)} pools...")
    
    try:
        multicall3_bulk_refresh(w3, graph, cache, pools)
        # Check graph state for v3
        v3_pool = graph.get_pool(usdc_weth_v3)
        if hasattr(v3_pool, 'sqrt_price_x96'):
            print(f"V3 POOL GRAPH UPDATED: {usdc_weth_v3} | sqrtPriceX96: {v3_pool.sqrt_price_x96}")
        print("Refresh executed successfully.")
    except Exception as e:
        print(f"Error during refresh: {e}")

if __name__ == "__main__":
    run_test()
