import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from log import setup_logging
setup_logging()

from tvl import init_prices
init_prices(2000.0, "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", [])

from cache import PoolCache
from graph import TokenGraph

weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
cache = PoolCache(1, cache_dir=os.path.join(ROOT, "cache"))
cache.load()
graph = TokenGraph()

print("Loading pools...")
cache.warm_graph(graph)
print(f"Graph loaded: {graph.pool_count} pools, {graph.token_count} tokens")

print("Running find_cycles with max_hops=4...")
import time
t0 = time.time()
cycles = graph.find_cycles(source_tokens=[weth, "0x9f90038f"], max_hops=4, min_profit_ratio=0.0)
t1 = time.time()

print(f"Time taken: {t1-t0:.2f} seconds")
print(f"Cycles found: {len(cycles)}")
