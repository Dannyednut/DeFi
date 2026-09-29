"""
debug_sim.py — Diagnostic deep-dive for Scenario B failure.
Traces exactly where the pipeline returns RESEARCH instead of DEX_SPREAD.

Run: python tests/debug_sim.py
"""
import sys, os, math
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from log import setup_logging, get_logger
setup_logging()
log = get_logger("debug_sim")

from graph import TokenGraph, PoolEdge
from mempool.simulator import MempoolSimulator
from mempool.decoder import DecodedSwap
from tvl import token_decimals

WETH    = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
TOKEN_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
POOL_B1 = "0x3333333333333333333333333333333333333333"   # UniswapV2
POOL_B2 = "0x4444444444444444444444444444444444444444"   # SushiswapV2

def make_v2(pool, dex, ti, to, ri, ro, fee=30):
    return (
        PoolEdge(pool, dex, ti, to, ri, ro, fee, 2),
        PoolEdge(pool, dex, to, ti, ro, ri, fee, 2),
    )

# Build the same graph as Scenario B
g = TokenGraph()
R_W1, R_B1 = 10*10**18, 20_000*10**18
R_W2, R_B2 = 15*10**18, 30_000*10**18
g.add_or_update_pool(*make_v2(POOL_B1, "UniswapV2",   WETH, TOKEN_B, R_W1, R_B1))
g.add_or_update_pool(*make_v2(POOL_B2, "SushiswapV2", WETH, TOKEN_B, R_W2, R_B2))
R_W, R_UC = 100*10**18, int(100*10**18/10**18*2000*10**6)
g.add_or_update_pool(*make_v2("0xpool_weth_usdc",  "UniswapV2",   WETH, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", R_W, R_UC))
g.add_or_update_pool(*make_v2("0xpool_weth_usdc2", "SushiswapV2", WETH, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", R_W, R_UC))

print(f"\nGraph: {g.pool_count} pools, {g.token_count} tokens")
print(f"Edges from WETH: {len(g.get_edges_from(WETH))}")
for e in g.get_edges_from(WETH):
    print(f"  → {e.token_out[:10]} via {e.dex_name} pool={e.pool_address[:10]} r_in={e.reserve_in/1e18:.2f} r_out={e.reserve_out/1e18:.2f}")

# Target edge: WETH→TOKEN_B on UniswapV2 (pool B1)
edge = next(e for e in g.get_edges_from(WETH) if e.token_out==TOKEN_B and e.dex_name=="UniswapV2")
print(f"\nTarget edge: {edge.pool_address[:12]} {edge.dex_name}")
print(f"  reserve_in={edge.reserve_in/1e18} WETH  reserve_out={edge.reserve_out/1e18} TOKEN_B")

# Compute 3.5% impact swap amount
frac = 0.035
amount_in = int(frac * edge.reserve_in / (1.0 - frac))
fee_num = 10_000 - edge.fee_bps
amt_with_fee = amount_in * fee_num
amount_out   = amt_with_fee * edge.reserve_out // (edge.reserve_in * 10_000 + amt_with_fee)
r_in_after  = edge.reserve_in  + amount_in
r_out_after = edge.reserve_out - amount_out
price_before = edge.reserve_out / edge.reserve_in
price_after  = r_out_after / r_in_after
real_impact  = abs(price_before - price_after) / price_before * 100

print(f"\n--- Step 1: Swap projection ---")
print(f"  amount_in={amount_in/1e18:.6f} WETH")
print(f"  amount_out={amount_out/1e18:.4f} TOKEN_B")
print(f"  r_in_after={r_in_after/1e18:.6f}  r_out_after={r_out_after/1e18:.4f}")
print(f"  real_impact={real_impact:.4f}%")

# Step 2: Find projected cycles
sim = MempoolSimulator(g, native_price_usd=2024.0)
cycles = sim._find_cycles_with_projected_rate(edge, r_in_after, r_out_after)
print(f"\n--- Step 2: Cycles with projected reserves: {len(cycles)} ---")
for c in cycles:
    print(f"  {c.path_string}  profit={c.estimated_profit_ratio*100:.4f}%  cross={c.is_cross_dex}  pools={c.pools}")

# Step 3: _reserve_math_cycle for each
print(f"\n--- Step 3: Reserve math validation ---")
for c in cycles:
    result = sim._reserve_math_cycle(c, edge, r_in_after, r_out_after)
    if result:
        cyc, test_amt, out_amt, info = result
        print(f"  ✅ PROFITABLE: ratio={info['ratio']*100:.4f}%  usd={info['usd']}")
        print(f"     test_amt={test_amt}  out_amt={out_amt}")
    else:
        print(f"  ❌ FAILED reserve math for cycle: {c.path_string}")
        # Manual trace
        root_token = c.tokens[0].lower()
        root_dec = token_decimals(root_token)
        TEST_AMOUNT = 10 ** root_dec
        amount = TEST_AMOUNT
        print(f"  Tracing manually with TEST_AMOUNT={TEST_AMOUNT} (1 {root_token[:10]})")
        for i, pool_addr in enumerate(c.pools):
            t_in_hop  = c.tokens[i].lower()
            t_out_hop = c.tokens[i+1].lower()
            print(f"  Hop {i}: {t_in_hop[:10]} → {t_out_hop[:10]} via pool {pool_addr[:12]}")
            if pool_addr.lower() == edge.pool_address.lower():
                if t_in_hop == edge.token_in.lower():
                    ri, ro = r_in_after, r_out_after
                    print(f"    → PROJECTED reserves: ri={ri/1e18:.6f}  ro={ro/1e18:.4f}")
                else:
                    ri, ro = r_out_after, r_in_after
                    print(f"    → PROJECTED (reverse): ri={ri/1e18:.4f}  ro={ro/1e18:.6f}")
            else:
                hop_edge = sim._find_edge(pool_addr, t_in_hop, t_out_hop)
                if hop_edge is None:
                    print(f"    → ❌ EDGE NOT FOUND — _find_edge({pool_addr[:12]}, {t_in_hop[:10]}, {t_out_hop[:10]}) returned None")
                    # Check what IS available
                    avail = g.get_edges_from(t_in_hop)
                    print(f"    Available edges from {t_in_hop[:10]}: {[(e.token_out[:10], e.pool_address[:12], e.dex_name) for e in avail]}")
                    break
                else:
                    ri, ro = hop_edge.reserve_in, hop_edge.reserve_out
                    print(f"    → LIVE reserves: ri={ri/1e18:.6f}  ro={ro/1e18:.4f}  pool={hop_edge.pool_address[:12]} ({hop_edge.dex_name})")

            fee_bps = c.fees[i] if i < len(c.fees) else 30
            fn = 10_000 - fee_bps
            afee = amount * fn
            amount_new = afee * ro // (ri * 10_000 + afee)
            print(f"    amount {amount} → {amount_new}  (ratio={amount_new/amount:.6f})")
            amount = amount_new
        
        print(f"  Final amount: {amount}  vs start: {TEST_AMOUNT}")
        print(f"  Profitable? {amount > TEST_AMOUNT}  ratio={(amount-TEST_AMOUNT)/TEST_AMOUNT*100:.4f}%")

# Step 4: Full _simulate_single_pool call
print(f"\n--- Step 4: Full _simulate_single_pool ---")
swap = DecodedSwap(
    tx_hash="0x" + "e1" * 32,
    router="0x" + "00" * 20,
    router_name=edge.dex_name,
    version=edge.version,
    function_name="swapExactTokensForTokens",
    token_path=[edge.token_in, edge.token_out],
    amount_in=amount_in,
    amount_out_min=0,
    amount_in_is_exact=True,
    sender="0x" + "aa" * 20,
    gas_price=10**9,
)
result = sim._simulate_single_pool(swap=swap, edge=edge, t_in=edge.token_in, t_out=edge.token_out)
if result:
    opp = result[0]
    print(f"  opp_type={opp.opp_type}  profit_usd={opp.estimated_profit_usd:.6f}  profit_token={opp.profit_in_token:.8f} {opp.profit_token_symbol}")
else:
    print("  → returned None")
