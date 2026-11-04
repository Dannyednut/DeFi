import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from mempool.simulator import MempoolSimulator, PendingOpportunity
from graph import TokenGraph, PoolEdge
from tests.test_simulator_flow import build_triangular_graph, WETH, TOKEN_B, TOKEN_C, _make_decoded_swap
from tvl import init_prices, token_decimals

init_prices(2000.0, WETH, [])
graph = build_triangular_graph()
sim = MempoolSimulator(graph, native_price_usd=2000.0)
ea = next(e for e in graph.get_edges_from(WETH) if e.token_out == TOKEN_C)
swap = _make_decoded_swap(ea, 5.0)

amount_in = swap.amount_in
fee_num = 10000 - ea.fee_bps
amt_with_fee = amount_in * fee_num
amount_out = amt_with_fee * ea.reserve_out // (ea.reserve_in * 10000 + amt_with_fee)
r_in_after = ea.reserve_in + amount_in
r_out_after = ea.reserve_out - amount_out

cycles = sim._find_cycles_with_projected_rate(ea, r_in_after, r_out_after)
cycle = cycles[0]

root_token = cycle.tokens[0].lower()
root_dec   = token_decimals(root_token)
amount = 10 ** root_dec

for i, pool_addr in enumerate(cycle.pools):
    t_in_hop  = cycle.tokens[i].lower()
    t_out_hop = cycle.tokens[i + 1].lower()

    if pool_addr.lower() == ea.pool_address.lower():
        if t_in_hop == ea.token_in.lower():
            ri, ro = r_in_after, r_out_after
            direction = "forward"
        else:
            ri, ro = r_out_after, r_in_after
            direction = "reverse"
        print(f"Hop {i}: using IMPACTED pool in {direction} direction. ri={ri/1e18}, ro={ro/1e18}")
    else:
        hop_edge = sim._find_edge(pool_addr, t_in_hop, t_out_hop)
        ri, ro = hop_edge.reserve_in, hop_edge.reserve_out
        print(f"Hop {i}: using live pool. ri={ri/1e18}, ro={ro/1e18}")

    fee_bps = cycle.fees[i] if i < len(cycle.fees) else 30
    fn      = 10000 - fee_bps
    afee    = amount * fn
    out_amt  = afee * ro // (ri * 10000 + afee)
    print(f"  amount_in: {amount/1e18} -> out: {out_amt/1e18}")
    amount = out_amt

test_amt = 10 ** root_dec
print(f"final amount: {amount/1e18}. test_amt: {test_amt/1e18}. diff: {amount - test_amt}")
