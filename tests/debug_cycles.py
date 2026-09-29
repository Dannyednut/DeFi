import asyncio
from cache import PoolCache
from graph import TokenGraph
from config import CHAIN_ID, NATIVE_PRICE_USD, get_chain
from registry import bootstrap_graph
from utils.rpc_manager import MultiRPCManager
from tvl import init_prices

async def main():
    print("Initializing environment...")
    chain = get_chain()
    init_prices(NATIVE_PRICE_USD, chain.wrapped_native, chain.stablecoins)
    
    print("Loading pools into cache...")
    cache = PoolCache(chain_id=CHAIN_ID)
    cache.load()
    
    graph = TokenGraph()
    print("Bootstrapping graph...")
    # Mocking w3 so we don't need real RPC for this test
    bootstrap_graph(None, graph, cache)
            
    print(f"Graph ready. Nodes: {graph.token_count}, Edges: {graph.edge_count}")
    
    WETH = chain.wrapped_native.lower()
    print(f"\nScanning for cycles from WETH...")
    cycles = graph.find_cycles(source_tokens=[WETH], max_hops=3, min_profit_ratio=0.0)
    print(f"Found {len(cycles)} cycles.")
    
    if cycles:
        c = cycles[0]
        # Use ASCII representation for Windows terminal
        path_ascii = " -> ".join([t[:8] for t in c.tokens])
        print(f"Sample cycle: {path_ascii} | profit={c.estimated_profit_ratio*100:.4f}%")
        
        # Test if we can find it again with must_include_pool
        pool_addr = c.pools[0]
        print(f"\nTesting must_include_pool={pool_addr}")
        inc_cycles = graph.find_cycles(max_hops=3, min_profit_ratio=0.0, must_include_pool=pool_addr)
        print(f"Found {len(inc_cycles)} cycles with must_include_pool.")
        for ic in inc_cycles:
            ic_path_ascii = " -> ".join([t[:8] for t in ic.tokens])
            print(f"  {ic_path_ascii} | {ic.pools}")

if __name__ == "__main__":
    asyncio.run(main())
