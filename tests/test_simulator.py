import asyncio
import time
from cache import PoolCache
from graph import TokenGraph
from mempool.decoder import DecodedSwap
from mempool.simulator import MempoolSimulator
from config import CHAIN_ID, NATIVE_PRICE_USD, get_chain, get_rpc_configs
from prices import PriceOracle
from tvl import set_oracle, init_prices
from registry import bootstrap_graph
from utils.rpc_manager import MultiRPCManager

from log import setup_logging
setup_logging()

async def main():
    print("Initializing environment...")
    rpc_configs = get_rpc_configs()
    rpc_manager = MultiRPCManager.from_config(rpc_configs)
    w3 = rpc_manager.get_web3(ws=False)
    
    chain = get_chain()
    oracle = PriceOracle(
        chain_id=CHAIN_ID,
        native_price_usd=NATIVE_PRICE_USD,
        wrapped_native=chain.wrapped_native,
        stablecoins=chain.stablecoins,
    )
    set_oracle(oracle)
    init_prices(NATIVE_PRICE_USD, chain.wrapped_native, chain.stablecoins)
    
    print("Loading pools into cache...")
    cache = PoolCache(chain_id=CHAIN_ID)
    cache.load()
    print(f"Loaded {cache.pool_count()} pools into cache.")

    graph = TokenGraph()
    print("Bootstrapping graph...")
    bootstrap_graph(w3, graph, cache)
            
    print(f"Graph ready. Nodes: {graph.token_count}, Edges: {graph.edge_count}")
    
    WETH = chain.wrapped_native.lower()
    print("\nScanning for natural cycles from WETH...")
    # Get natural cycles with a low profit threshold
    all_natural = graph.find_cycles(source_tokens=[WETH], max_hops=3, min_profit_ratio=0.0001)
    
    # Take one unique sample of each type
    by_type = {}
    for c in all_natural:
        if c.cycle_type not in by_type and c.estimated_profit_ratio < 0.1: # avoid outliers
            by_type[c.cycle_type] = c
            
    print(f"Found {len(all_natural)} natural cycles. Testing samples of types: {list(by_type.keys())}")
    for t, c in by_type.items():
        print(f"  {t}: {c.path_string} | Profit: {c.estimated_profit_ratio*100:.4f}%")
            
    sim = MempoolSimulator(graph)
    
    print("\n--- RUNNING MOCK SIMULATIONS ---")
    for t, cycle in by_type.items():
        print(f"\n[Testing {t}] Cycle: {cycle.path_string}")
        
        # We want to mock a swap that makes a cycle hop MORE profitable.
        # If hop is A -> B, mock swap is B -> A.
        # We'll mock the FIRST hop.
        hop_a = cycle.tokens[0]
        hop_b = cycle.tokens[1]
        pool_addr = cycle.pools[0]
        dex_name = cycle.dexes[0]
        version = cycle.versions[0]
        
        # Get reserves to scale the mock swap
        edge = next((e for e in graph.get_edges_from(hop_b) if e.token_out == hop_a and e.pool_address == pool_addr), None)
        if not edge:
            print(f"   -> ERROR: Could not find reverse edge for pool {pool_addr}")
            continue
            
        # Swap 2% of reserves
        r_in = edge.reserve_in if edge.version == 2 else (edge.liquidity << 96) // edge.sqrt_price_x96 if edge.sqrt_price_x96 > 0 else 10**18
        mock_amount = max(int(r_in * 0.02), 10**15)
        
        mock_swap = DecodedSwap(
            tx_hash="0xmock_" + t,
            router="0xmock", 
            router_name=dex_name,
            version=version,
            function_name="swap",
            token_path=[hop_b, hop_a], # Reverse of first hop
            amount_in=mock_amount,
            amount_out_min=0,
            amount_in_is_exact=True,
            sender="0xsender",
            gas_price=20 * 10**9
        )
        
        print(f"   Mocking Swap on {dex_name}: {hop_b[:8]} -> {hop_a[:8]} | Amount: {mock_amount}")
        results = sim.simulate(mock_swap)
        
        if not results:
            print("   -> NO RESULT")
            continue
            
        found_verified = False
        for res in results:
            if res.opp_type != "RESEARCH":
                found_verified = True
                print(f"   -> SUCCESS: Found {res.opp_type} | Profit: {res.estimated_profit_ratio*100:.4f}% | Impact: {res.price_impact_pct:.4f}%")
                if res.verified_cycle:
                    print(f"      Verified Path: {res.verified_cycle.path_string}")
        
        if not found_verified:
            # If no named type, show the RESEARCH impact
            res = results[0]
            print(f"   -> RESEARCH ONLY | Impact: {res.price_impact_pct:.4f}% | Detected Cycles: {len(res.metadata.get('projected_cycles', []))}")

if __name__ == "__main__":
    asyncio.run(main())
