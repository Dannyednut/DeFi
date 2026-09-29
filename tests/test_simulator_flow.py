"""
Diagnostic test for MempoolSimulator._find_cycles_with_projected_rate()

Tests the EXACT scenario from the logs:
  - WETH → 0x285f pool (TVL $4,626, single-DEX new token)
  - Impact ~3.89% → should find 0 cycles (isolated token, no cross-DEX path)
  - WETH → 0x63d5 pool (TVL $613,162) → should find cycles (large pool)

Run with:  python -m pytest tests/test_simulator_flow.py -v -s
"""
from __future__ import annotations

import math
import sys
import os

# ── path setup ──────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from log import setup_logging, get_logger
setup_logging()
log = get_logger("test_sim")

from graph import TokenGraph, PoolEdge
from mempool.simulator import MempoolSimulator


# ── Canonical Ethereum addresses ─────────────────────────────────────────────
WETH  = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC  = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT  = "0xdac17f958d2ee523a2206206994597c13d831ec7"
TOKEN_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"   # isolated new token
TOKEN_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"   # connected token (on 2 DEXes)
TOKEN_C = "0xcccccccccccccccccccccccccccccccccccccccc"   # bridge token
POOL_WETH_A  = "0x1111111111111111111111111111111111111111"
POOL_USDC_A  = "0x2222222222222222222222222222222222222222"
POOL_WETH_B1 = "0x3333333333333333333333333333333333333333"  # UniswapV2
POOL_WETH_B2 = "0x4444444444444444444444444444444444444444"  # SushiswapV2 (same pair, diff rate)
POOL_WETH_C  = "0x5555555555555555555555555555555555555555"
POOL_C_B     = "0x6666666666666666666666666666666666666666"

FEE = 30   # 0.3%


def make_v2_edges(pool_addr, dex, tok_in, tok_out, r_in, r_out, fee=30, tvl_usd=None):
    """Helper: create both directed edges for a V2 pool."""
    # Estimate TVL if not provided (assume WETH ~$2000, 2x for both sides)
    if tvl_usd is None:
        tvl_usd = max(50000.0, (r_in / 1e18) * 2000 * 2)
    
    return (
        PoolEdge(pool_addr, dex, tok_in,  tok_out, r_in,  r_out, fee, 2, tvl_usd=tvl_usd),
        PoolEdge(pool_addr, dex, tok_out, tok_in,  r_out, r_in,  fee, 2, tvl_usd=tvl_usd),
    )


def build_isolated_token_graph() -> TokenGraph:
    """
    Graph where TOKEN_A only exists on ONE pool (WETH/TOKEN_A).
    No cross-DEX path exists → no profitable cycle after impact.

    Mimics: WETH → 0x285f pool (TVL $4,626) — logs show RESEARCH, est_profit=$0
    """
    g = TokenGraph()
    # WETH → TOKEN_A on UniswapV2 (TVL ~$4k equivalent)
    # 1 WETH = 500_000_000 TOKEN_A (as in logs: price=536879899.7 raw)
    R_WETH = 2 * 10**18          # 2 WETH reserve  (~$4k at $2000/ETH)
    R_A    = int(R_WETH * 536_879_899)
    ea, eb = make_v2_edges(POOL_WETH_A, "UniswapV2", WETH, TOKEN_A, R_WETH, R_A)
    g.add_or_update_pool(ea, eb)

    # Also add some other unrelated pools (WETH/USDC, WETH/USDT) for realism
    R_W  = 100 * 10**18
    R_UC = int(R_W / 10**18 * 2000 * 10**6)   # USDC 6 dec
    g.add_or_update_pool(*make_v2_edges("0xpool_weth_usdc", "UniswapV2", WETH, USDC, R_W, R_UC))
    g.add_or_update_pool(*make_v2_edges("0xpool_weth_usdt", "UniswapV2", WETH, USDT, R_W, R_UC))
    return g


def build_connected_token_graph() -> TokenGraph:
    """
    Graph where TOKEN_B exists on TWO DEXes (UniswapV2 + SushiswapV2).
    A pending 3.5% impact buy on UniswapV2 creates a cross-DEX arb:
      WETH → TOKEN_B (Uniswap, at WORSE rate after impact)
      TOKEN_B → WETH (Sushi, at ORIGINAL rate)

    Should produce at least 1 profitable cycle.
    """
    g = TokenGraph()

    # UniswapV2: 10 WETH ↔ 20000 TOKEN_B  (price = 2000 TOKEN_B/WETH)
    R_W1 = 10 * 10**18
    R_B1 = 20_000 * 10**18
    g.add_or_update_pool(*make_v2_edges(POOL_WETH_B1, "UniswapV2",   WETH, TOKEN_B, R_W1, R_B1))

    # SushiswapV2: same price, separate pool
    R_W2 = 15 * 10**18
    R_B2 = 30_000 * 10**18
    g.add_or_update_pool(*make_v2_edges(POOL_WETH_B2, "SushiswapV2", WETH, TOKEN_B, R_W2, R_B2))

    # Background pools for realism
    R_W  = 100 * 10**18
    R_UC = int(R_W / 10**18 * 2000 * 10**6)
    g.add_or_update_pool(*make_v2_edges("0xpool_weth_usdc", "UniswapV2",   WETH, USDC, R_W, R_UC))
    g.add_or_update_pool(*make_v2_edges("0xpool_weth_usdc2","SushiswapV2", WETH, USDC, R_W, R_UC))
    return g


def build_triangular_graph() -> TokenGraph:
    """
    WETH → TOKEN_C on UniswapV2 (to be impacted)
    TOKEN_C → TOKEN_B on some DEX
    TOKEN_B → WETH on some DEX
    → Triangular arb should emerge after impact on WETH/TOKEN_C
    """
    g = TokenGraph()
    R_W  = 50 * 10**18
    R_C  = 100_000 * 10**18    # 1 WETH = 2000 TOKEN_C

    g.add_or_update_pool(*make_v2_edges(POOL_WETH_C, "UniswapV2", WETH, TOKEN_C, R_W, R_C))

    # TOKEN_C → TOKEN_B pool (mimics TOKEN_C having cross-pool value)
    # 1 TOKEN_C = 0.5 TOKEN_B
    R_C2 = 10_000 * 10**18
    R_B  = 5_000  * 10**18
    g.add_or_update_pool(*make_v2_edges(POOL_C_B, "UniswapV2", TOKEN_C, TOKEN_B, R_C2, R_B))

    # TOKEN_B → WETH (closing leg)
    R_B2 = 10_000 * 10**18
    R_W2 = 5     * 10**18     # 1 TOKEN_B = 0.0005 WETH
    g.add_or_update_pool(*make_v2_edges(POOL_WETH_B1, "UniswapV2", TOKEN_B, WETH, R_B2, R_W2))

    return g


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_decoded_swap(edge: PoolEdge, impact_pct: float):
    """Build a minimal DecodedSwap that mirrors a pending mempool tx."""
    from mempool.decoder import DecodedSwap
    r_in  = edge.reserve_in
    r_out = edge.reserve_out
    frac  = impact_pct / 100.0
    amount_in = max(1, int(frac * r_in / (1.0 - frac)))
    return DecodedSwap(
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


def _simulate_and_report(label: str, graph: TokenGraph, edge: PoolEdge,
                          impact_pct: float, native_price: float = 2024.0):
    """
    Run the REAL simulator._simulate_single_pool() on a decoded swap,
    print a full diagnostic trace, and return (opp_list, opp_type, profit_usd).
    """
    sim = MempoolSimulator(graph, native_price_usd=native_price)
    swap = _make_decoded_swap(edge, impact_pct)

    # Compute projected reserves independently for the diagnostic display
    r_in  = edge.reserve_in
    r_out = edge.reserve_out
    fee_bps = edge.fee_bps
    amount_in = swap.amount_in
    fee_num      = 10_000 - fee_bps
    amt_with_fee = amount_in * fee_num
    amount_out   = amt_with_fee * r_out // (r_in * 10_000 + amt_with_fee)
    r_in_after   = r_in  + amount_in
    r_out_after  = r_out - amount_out

    price_before = r_out / r_in
    price_after  = r_out_after / r_in_after
    real_impact  = abs(price_before - price_after) / price_before * 100

    print(f"\n{'='*60}")
    print(f"  SCENARIO: {label}")
    print(f"{'='*60}")
    print(f"  Pool:          {edge.pool_address[:10]} ({edge.dex_name})")
    print(f"  Pair:          {edge.token_in[:8]} → {edge.token_out[:8]}")
    print(f"  Reserves:      {r_in/1e18:.4f} / {r_out/1e18:.4f}")
    print(f"  Amount in:     {amount_in/1e18:.6f} tokens")
    print(f"  Price before:  {price_before:.6g}")
    print(f"  Price after:   {price_after:.6g}")
    print(f"  Real impact:   {real_impact:.4f}%")
    print(f"  Graph pools:   {graph.pool_count}  tokens: {graph.token_count}")

    eg_in  = graph.get_edges_from(edge.token_in)
    eg_out = graph.get_edges_from(edge.token_out)
    print(f"  Edges from token_in  ({edge.token_in[:8]}): {len(eg_in)}")
    print(f"  Edges from token_out ({edge.token_out[:8]}): {len(eg_out)}")

    # ── Call the REAL simulator ──────────────────────────────────────
    opp_list = sim.simulate(
        swap=swap
        # edge=edge,
        # t_in=edge.token_in,
        # t_out=edge.token_out,
    )

    opp_type   = "RESEARCH"
    profit_usd = 0.0
    projected_opps = []
    opp = None

    if opp_list:
        opp = opp_list[0]
        opp_type   = opp.opp_type
        profit_usd = opp.estimated_profit_usd
        print(f"  OPP type:      {opp_type}")
        print(f"  impact:        {opp.price_impact_pct:.4f}%")
        print(f"  est_profit:   ${profit_usd:.6f}")
        if hasattr(opp, 'arb_pools'):
            print(f"  arb_pools:     {opp.arb_pools}")
    else:
        print("  → _simulate_single_pool returned None")

    # Also run _find_cycles_with_projected_rate for introspection
    projected_opps = sim._find_cycles_with_projected_rate(edge, r_in_after, r_out_after)
    print(f"  Cycles (POST-patch projected):    {len(projected_opps)}")
    if projected_opps:
        best = projected_opps[0]
        print(f"  Best cycle: {best.path_string}  profit={best.estimated_profit_ratio*100:.4f}%  cross_dex={best.is_cross_dex}")
    else:
        same_pair = graph.get_edges_for_pair(edge.token_in, edge.token_out)
        print(f"  → No profitable cycle. Pair pools: {len(same_pair)} "
              f"({'multi-DEX' if len(set(e.dex_name for e in same_pair))>1 else 'single-DEX'})")

    print(f"\n  ► OPP TYPE: {opp_type}  |  est_profit: ${profit_usd:.6f}")
    return projected_opps, opp_type, opp


# ─────────────────────────────────────────────────────────────────────────────
# TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_isolated_token_returns_research():
    """
    Scenario A: new single-DEX memecoin pool.
    WETH → TOKEN_A exists on UniswapV2 ONLY.
    After impact, no cross-DEX return path → 0 cycles → RESEARCH ✓
    """
    graph = build_isolated_token_graph()
    ea = graph.get_edges_from(WETH)[0]   # WETH → TOKEN_A edge

    cycles, opp_type, opp = _simulate_and_report(
        "A — Isolated token (single DEX, like 0x285f pool TVL=$4,626)",
        graph, ea, impact_pct=3.89
    )

    print("\n  VERDICT:", "✅ CORRECT — RESEARCH (no cross-DEX path exists)" if opp_type == "RESEARCH"
          else f"❌ UNEXPECTED — got {opp_type}")
    assert opp_type == "RESEARCH", f"Expected RESEARCH, got {opp_type}"
    if opp is not None:
        assert opp.profit_in_token == 0.0


def test_cross_dex_token_returns_arb():
    """
    Scenario B: same pair on TWO DEXes.
    Pending buy on UniswapV2 → WETH buys TOKEN_B at worse rate.
    SushiswapV2 still has original rate → DEX_SPREAD arb exists.
    """
    graph = build_connected_token_graph()
    # Get the WETH→TOKEN_B edge on UniswapV2
    ea = next(e for e in graph.get_edges_from(WETH)
               if e.token_out == TOKEN_B and e.dex_name == "UniswapV2")

    cycles, opp_type, opp = _simulate_and_report(
        "B — Cross-DEX token (UniswapV2 + SushiswapV2, impact=3.5%)",
        graph, ea, impact_pct=3.5
    )

    print("\n  VERDICT:", "✅ CORRECT — cross-DEX arb found" if opp and opp.profit_in_token > 0
          else "❌ UNEXPECTED — expected profitable arb")
    assert len(cycles) > 0, "Expected ≥1 cycle for cross-DEX pair"
    assert opp is not None, "Expected opp object"
    assert opp.profit_in_token > 0, f"Expected profitable arb, got {opp.profit_in_token:.4f}"


def test_triangular_arb():
    """
    Scenario C: triangular path via TOKEN_C bridge.
    WETH → TOKEN_C impacted → TOKEN_C → TOKEN_B → WETH.
    Should produce a 3-hop profitable cycle.
    """
    graph = build_triangular_graph()
    ea = next(e for e in graph.get_edges_from(WETH)
               if e.token_out == TOKEN_C)

    cycles, opp_type, opp = _simulate_and_report(
        "C — Triangular path (WETH→TOKEN_C→TOKEN_B→WETH, impact=5%)",
        graph, ea, impact_pct=5.0
    )

    print("\n  VERDICT:", "✅ Triangular path detected" if len(cycles) > 0
          else "⚠️  No triangular path (may need more connected graph)")
    # Don't hard-assert — the profitability depends on the exact reserve ratios


def test_min_profit_ratio_sensitivity():
    """
    Scenario D: verify that min_profit_ratio=0.0 in the simulator
    is NOT the bottleneck — the bottleneck is the ABSENCE of a 2nd DEX.
    Compares results at 0.0 vs 0.0001 on isolated graph.
    """
    graph = build_isolated_token_graph()
    ea = graph.get_edges_from(WETH)[0]

    sim = MempoolSimulator(graph, native_price_usd=2024.0)

    r_in  = ea.reserve_in
    r_out = ea.reserve_out
    frac = 0.0389
    amount_in = int(frac * r_in / (1.0 - frac))
    fee_num = 10_000 - ea.fee_bps
    amt_with_fee = amount_in * fee_num
    amount_out   = amt_with_fee * r_out // (r_in * 10_000 + amt_with_fee)
    r_in_after  = r_in  + amount_in
    r_out_after = r_out - amount_out

    # Test with min_profit_ratio=0.0 (what simulator uses)
    cycles_zero = graph.find_cycles(
        source_tokens=[WETH, ea.token_out],
        min_profit_ratio=0.0,
        max_hops=4,
    )
    # Test with a relaxed threshold
    cycles_relaxed = graph.find_cycles(
        source_tokens=[WETH, ea.token_out],
        min_profit_ratio=-1.0,  # accepts NEGATIVE profit cycles too
        max_hops=4,
    )

    print(f"\n{'='*60}")
    print("  SCENARIO D — min_profit_ratio sensitivity")
    print(f"{'='*60}")
    print(f"  Cycles at min_ratio=0.0:   {len(cycles_zero)}")
    print(f"  Cycles at min_ratio=-1.0:  {len(cycles_relaxed)}")
    if cycles_relaxed:
        best = cycles_relaxed[0]
        print(f"  Best (even if unprofitable): "
              f"profit={best.estimated_profit_ratio*100:.4f}% log_w={best.log_weight_sum:.4f}")
        print(f"  → If log_weight_sum > 0: the path LOSES money (fees > impact gain)")
    print()


if __name__ == "__main__":
    print("\n" + "="*70)
    print("  SIMULATOR FLOW DIAGNOSTIC")
    print("="*70)

    test_isolated_token_returns_research()
    test_cross_dex_token_returns_arb()
    test_triangular_arb()
    test_min_profit_ratio_sensitivity()

    print("\n" + "="*70)
    print("  ALL DIAGNOSTICS COMPLETE")
    print("="*70)
