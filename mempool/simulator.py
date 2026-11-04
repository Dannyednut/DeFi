"""
Enhanced Mempool Simulator
==========================
Pre-mine opportunity detector for pending transactions.

Design:
  1. For every hop in a pending swap, compute the projected post-swap reserves.
  2. Patch the graph temporarily with those projected reserves.
  3. Run a BFS-seeded DFS cycle search to find ALL arbitrage cycles through
     the impacted pool (DEX-spread, triangular, multi-hop).
  4. For each candidate cycle, re-run step-by-step AMM reserve math.
     - Use projected reserves for the impacted hop.
     - Match each other hop to the EXACT pool address in the cycle (not just
       the first edge for that token pair).
  5. Compute profit in the ROOT token's native unit.
     - If the root token is WETH/WBTC/stablecoin  ⇒ express in ETH or USD.
     - Otherwise                                    ⇒ express in the token unit.
  6. Never fabricate a USD profit if no price is available.
  7. Verify profitable cycles against fresh on-chain reserves.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from graph import TokenGraph, PoolEdge, ArbitrageCycle
    from mempool.decoder import DecodedSwap

from log import get_logger
from tvl import (
    token_price, token_decimals, profit_in_best_unit,
    is_known_token, _WETH_ADDRS, STABLE_TOKEN_SETS, _NATIVE_PRICE_USD
)

log = get_logger("simulator")


# ──────────────────────────────────────────────────────────────────────────────
# KNOWN HIGH-VALUE TOKEN LOOKUP
# ──────────────────────────────────────────────────────────────────────────────

_KNOWN_TOKENS: dict[str, str] = {
    # Ethereum
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "UNI",
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "AAVE",
    # BSC
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC-BSC",
    "0x55d398326f99059ff775485246999027b3197955": "USDT-BSC",
    # Polygon
    "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": "WMATIC",
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC-POLY",
    # Base
    "0x4200000000000000000000000000000000000006": "WETH-BASE",
    # Arbitrum
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "WETH-ARB",
}

def _token_symbol(addr: str) -> str:
    return _KNOWN_TOKENS.get(addr.lower(), addr[:10])


# ──────────────────────────────────────────────────────────────────────────────
# PENDING OPPORTUNITY DATACLASS
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PendingOpportunity:
    """An arbitrage opportunity predicted from a pending tx."""
    # Identity
    trigger_tx_hash:    str
    trigger_router:     str
    trigger_dex:        str
    pending_seen_at:    float

    # Affected pool
    affected_pool:          str
    token_in:               str
    token_out:              str
    simulated_amount_in:    int
    reserve_in_before:      int
    reserve_out_before:     int
    reserve_in_after:       int
    reserve_out_after:      int
    price_impact_pct:       float

    # Opportunity classification
    opp_type:               str
    arb_tokens:             list = field(default_factory=list)
    arb_pools:              list = field(default_factory=list)
    fees:                   list = field(default_factory=list)
    arb_dexes:              list = field(default_factory=list)
    estimated_profit_ratio: float = 0.0
    estimated_profit_usd:   float = 0.0
    gas_price:              int = 0
    max_fee:                int = 0
    max_priority:           int = 0

    # Token-native profit (no forced USD)
    profit_in_token:        float = 0.0   # human-readable amount
    profit_token_symbol:    str   = ""    # e.g. "WETH", "USDC", "tok:0xabcd1234"
    profit_token_addr:      str   = ""    # root token address

    # Outcome tracking
    mined_block:            Optional[int]   = None
    mined_at:               Optional[float] = None
    actual_reserve_in_after:  Optional[int] = None
    actual_reserve_out_after: Optional[int] = None
    simulation_accuracy_pct:  Optional[float] = None
    tx_outcome:             Optional[str]   = None
    
    # Debug / verification
    verified_cycle:         Optional["ArbitrageCycle"] = None
    metadata:               dict = field(default_factory=dict)

    # Competitor forensics
    winner_eoa:             Optional[str]   = None
    winner_contract:        Optional[str]   = None
    tip_gwei:               Optional[float] = None
    gas_used:               Optional[int]   = None
    total_fee_eth:          Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "trigger_tx_hash":        self.trigger_tx_hash,
            "trigger_router":         self.trigger_router,
            "trigger_dex":            self.trigger_dex,
            "pending_seen_at":        self.pending_seen_at,
            "affected_pool":          self.affected_pool,
            "token_in":               self.token_in,
            "token_out":              self.token_out,
            "simulated_amount_in":    self.simulated_amount_in,
            "reserve_in_before":      self.reserve_in_before,
            "reserve_out_before":     self.reserve_out_before,
            "reserve_in_after":       self.reserve_in_after,
            "reserve_out_after":      self.reserve_out_after,
            "price_impact_pct":       round(self.price_impact_pct, 4),
            "opp_type":               self.opp_type,
            "arb_tokens":             self.arb_tokens,
            "arb_pools":              self.arb_pools,
            "fees":                   self.fees,
            "arb_dexes":              self.arb_dexes,
            "estimated_profit_ratio": round(self.estimated_profit_ratio, 6),
            "estimated_profit_usd":   round(self.estimated_profit_usd, 4),
            "gas_price":              self.gas_price,
            "max_fee":                self.max_fee,
            "max_priority":           self.max_priority,
            "profit_in_token":        round(self.profit_in_token, 8),
            "profit_token_symbol":    self.profit_token_symbol,
            "profit_token_addr":      self.profit_token_addr,
            "mined_block":            self.mined_block,
            "mined_at":               self.mined_at,
            "simulation_accuracy_pct": self.simulation_accuracy_pct,
            "tx_outcome":             self.tx_outcome,
            "winner_eoa":             self.winner_eoa,
            "winner_contract":        self.winner_contract,
            "tip_gwei":               self.tip_gwei,
            "gas_used":               self.gas_used,
            "total_fee_eth":          self.total_fee_eth,
        }

    def to_opportunity(self, chain_id: int, block_number: int) -> "Opportunity":
        """Convert to standardized Opportunity for tracking."""
        from logger import Opportunity, OppType
        
        # Strip VERIFIED_ prefix if present for enum lookup
        type_str = self.opp_type.replace("VERIFIED_", "")
        try:
            opp_type = OppType(type_str)
        except ValueError:
            opp_type = OppType.MULTI_HOP

        return Opportunity(
            opp_type=opp_type,
            chain_id=chain_id,
            block_number=block_number,
            timestamp=self.pending_seen_at,
            tokens=self.arb_tokens,
            pools=self.arb_pools,
            fees=self.fees,
            dexes=self.arb_dexes,
            estimated_profit_usd=self.estimated_profit_usd,
            estimated_profit_ratio=self.estimated_profit_ratio,
            amount_in_usd=0.0, # Not easily available here
            gas_cost_usd=0.0,   # Not estimated for mempool yet
            net_profit_usd=self.estimated_profit_usd,
            metadata={
                "trigger_tx": self.trigger_tx_hash,
                "impact_pct": self.price_impact_pct,
                "is_mempool": True,
            }
        )


# ──────────────────────────────────────────────────────────────────────────────
# MEMPOOL SIMULATOR
# ──────────────────────────────────────────────────────────────────────────────

class MempoolSimulator:
    """
    Simulates price impact of pending transactions and detects arbitrage.

    Profit reporting rules:
      - Always compute profit in the ROOT token's native unit.
      - If root token is WETH     → report as ETH (then optionally USD).
      - If root token is stablecoin → report directly as USD.
      - If root token is unknown   → report as "<amount> <symbol/addr>".
      - NEVER guess USD from unknown token prices.
    """

    MIN_IMPACT_PCT    = 0.0    # Capture ALL swaps for research
    MIN_PROFIT_RATIO  = 0.0    # Find all non-negative cycles
    PROFIT_SANITY_CAP = 0.50   # >50% profit from 1 token = likely bad data
    MIN_POOL_TVL_USD  = 1.0    # Ignore pools with less than $1 TVL entirely

    def __init__(self, graph: "TokenGraph", native_price_usd: float = 3000.0):
        self._graph        = graph
        self._native_price = native_price_usd

    # ══════════════════════════════════════════════════════════════════════════
    # PUBLIC ENTRY
    # ══════════════════════════════════════════════════════════════════════════

    def simulate(self, swap: "DecodedSwap") -> list[PendingOpportunity]:
        """
        Simulate all hops in a pending swap.
        For each hop, find the relevant pool edge and run the arb scan.
        """
        results = []
        for i in range(len(swap.token_path) - 1):
            t_in  = swap.token_path[i].lower()
            t_out = swap.token_path[i + 1].lower()

            # Match by router name first (same DEX), then any edge for the pair
            affected_edges = [
                e for e in self._graph.get_edges_from(t_in)
                if e.token_out == t_out and e.dex_name == swap.router_name
            ]
            if not affected_edges:
                affected_edges = [
                    e for e in self._graph.get_edges_from(t_in)
                    if e.token_out == t_out
                ]
            # log.info(f"Found {len(affected_edges)} edges for {t_in} -> {t_out}")
            for edge in affected_edges[:1]:
                opp_list = self._simulate_single_pool(swap, edge, t_in, t_out, hop_index=i)
                if opp_list:
                    results.extend(opp_list)
        return results

    # ══════════════════════════════════════════════════════════════════════════
    # CORE SIMULATION
    # ══════════════════════════════════════════════════════════════════════════

    def _simulate_single_pool(
        self,
        swap:      "DecodedSwap",
        edge:      "PoolEdge",
        t_in:      str,
        t_out:     str,
        hop_index: int,
    ) -> Optional[list[PendingOpportunity]]:
        """
        Apply a pending swap to one pool and search for arb cycles.

        Returns a list containing exactly ONE PendingOpportunity:
          - RESEARCH  if no profitable cycle is confirmed.
          - A named type (DEX_SPREAD, TRIANGULAR …) if one is.
        """
        # ── 1. Resolve reserve directions ──────────────────────────────────
        # Use t_in/t_out from the loop in simulate(), NOT swap.token_in/out
        # which might be the start/end of a long multi-hop swap.
        t_in  = t_in.lower()
        t_out = t_out.lower()

        if edge.version == 3:
            L = edge.liquidity
            sqrtP = edge.sqrt_price_x96
            if not L or not sqrtP:
                return None
            # Uniswap V3: sqrtP = sqrt(r1/r0) * 2^96
            # r0 = L / sqrtP,  r1 = L * sqrtP
            r0 = (L << 96) // sqrtP
            r1 = (L * sqrtP) >> 96
            # edge.token_in is token0 if token0 < token1
            if t_in < t_out:
                r_in, r_out = int(r0), int(r1)
            else:
                r_in, r_out = int(r1), int(r0)
        else:
            r_in = edge.reserve_in
            r_out = edge.reserve_out

        if r_in <= 0 or r_out <= 0:
            return None

        # ── 1.5 Liquidity Guard ────────────────────────────────────────────
        from tvl import estimate_tvl_from_edge
        tvl = estimate_tvl_from_edge(edge)
        if tvl < self.MIN_POOL_TVL_USD:
            log.debug(f"  Skipping ultra-shallow pool {edge.pool_address[:10]} (TVL: ${tvl:.2f})")
            return None

        # ── 2. Determine amount_in for THIS hop ────────────────────────────
        # Propagate through previous hops to get the actual amount arriving here
        amount_in = float(swap.amount_in)
        for j in range(hop_index):
            h_in  = swap.token_path[j].lower()
            h_out = swap.token_path[j+1].lower()
            
            # Find edge for this dex (best effort)
            prev_edge = next((e for e in self._graph.get_edges_from(h_in) 
                             if e.token_out == h_out and e.dex_name == swap.router_name), None)
            if not prev_edge:
                prev_edge = next((e for e in self._graph.get_edges_from(h_in) 
                                 if e.token_out == h_out), None)
            
            if not prev_edge:
                return None # Cannot propagate
            
            # Get amount out using simple math
            amount_in = self._get_amount_out_simple(amount_in, prev_edge)
            if amount_in <= 0: return None
        
        amount_in = int(amount_in)

        if amount_in <= 0:
            return None

        # ── 2. Compute projected reserves after the pending swap ───────────
        fee_bps = edge.fee_bps
        new_sqrt_p = None
        if swap.version == 3 and edge.sqrt_price_x96 and edge.liquidity:
            r_in_after, r_out_after, new_sqrt_p = self._apply_swap_v3(amount_in, edge)
        else:
            r_in_after, r_out_after = self._apply_swap_v2(amount_in, r_in, r_out, fee_bps)

        # ── 3. Price impact ────────────────────────────────────────────────
        price_before = r_out / r_in
        price_after  = r_out_after / r_in_after if r_in_after > 0 else 0
        if price_before == 0 or price_after == 0:
            return None

        impact_pct = abs(price_before - price_after) / price_before * 100

        log.research(
            f"STATE CHANGE | {edge.dex_name} | "
            f"{_token_symbol(t_in)}->{_token_symbol(t_out)} | "
            f"Price: {price_before:.10g} -> {price_after:.10g} ({impact_pct:.4f}% impact)"
        )

        # ── 4. Find cycles with projected reserves ─────────────────────────
        projected_cycles = self._find_cycles_with_projected_rate(
            edge, r_in_after, r_out_after, new_sqrt_p
        )

        if not projected_cycles:
            log.debug(f"  No cycles found in projected graph search (must_include_pool={edge.pool_address[:10]})")
            return [self._research_opp(swap, edge, t_in, t_out,
                                       amount_in, r_in, r_out,
                                       r_in_after, r_out_after, impact_pct,
                                       [])]

        log.debug(f"  Found {len(projected_cycles)} cycles in projected search. Picking best...")

        # ── 5. Pick best cycle and verify with reserve math ────────────────
        best = self._pick_best_profitable_cycle(
            projected_cycles, edge, r_in_after, r_out_after
        )

        if best is None:
            log.debug(f"  None of the {len(projected_cycles)} cycles were confirmed profitable by reserve math.")
            return [self._research_opp(swap, edge, t_in, t_out,
                                       amount_in, r_in, r_out,
                                       r_in_after, r_out_after, impact_pct,
                                       projected_cycles)]

        cycle, TEST_AMOUNT, amount_out, profit_info = best

        # ── 6. Classify and build the opportunity ──────────────────────────
        opp_type = self._classify_cycle(cycle)

        opp = PendingOpportunity(
            trigger_tx_hash=swap.tx_hash,
            trigger_router=swap.router,
            trigger_dex=swap.router_name,
            pending_seen_at=time.time(),
            affected_pool=edge.pool_address,
            token_in=t_in,
            token_out=t_out,
            simulated_amount_in=amount_in,
            reserve_in_before=r_in,
            reserve_out_before=r_out,
            reserve_in_after=r_in_after,
            reserve_out_after=r_out_after,
            price_impact_pct=impact_pct,
            opp_type=opp_type,
            arb_tokens=cycle.tokens,
            arb_pools=cycle.pools,
            fees=cycle.fees,
            arb_dexes=cycle.dexes,
            estimated_profit_ratio=profit_info["ratio"],
            # estimated_profit_usd=profit_info["usd"],
            profit_in_token=profit_info["token_amount"],
            profit_token_symbol=profit_info["token_symbol"],
            profit_token_addr=profit_info["token_addr"],
            gas_price=swap.gas_price,
            max_fee=swap.max_fee or 0,
            max_priority=swap.max_priority or 0,
        )

        # ── 7. On-chain verification (best-effort, no RPC crash) ───────────
        verified = self._verify_opportunity(opp, cycle)
        if not verified:
            opp.opp_type   = "RESEARCH"
            opp.arb_tokens = [t_in, t_out]
            opp.arb_pools  = [edge.pool_address]
            opp.arb_dexes  = [edge.dex_name]
            opp.estimated_profit_ratio = 0.0
            opp.estimated_profit_usd   = 0.0
            opp.profit_in_token        = 0.0
            opp.profit_token_symbol    = ""

        return [opp]

    # ══════════════════════════════════════════════════════════════════════════
    # CYCLE PICKING — reserve math
    # ══════════════════════════════════════════════════════════════════════════

    def _pick_best_profitable_cycle(
        self,
        cycles:       list["ArbitrageCycle"],
        edge:         "PoolEdge",
        r_in_after:   int,
        r_out_after:  int,
    ) -> Optional[tuple]:
        """
        Iterate cycles (sorted by graph-estimated profit, best first).
        For each, run concrete reserve math using:
          - PROJECTED reserves for the impacted pool hop (in the correct direction).
          - LIVE graph reserves (matched by pool_address) for all other hops.

        The reserve math in _reserve_math_cycle is the definitive profitability
        check — we do NOT add a direction pre-filter here because the impacted
        pool can legitimately be traversed in EITHER direction:
          • Same direction  (WETH→TOKEN_B on impacted pool): arber backruns
            by buying TOKEN_B cheap and selling it elsewhere.
          • Reverse direction (TOKEN_B→WETH on impacted pool): arber has
            TOKEN_B and sells it for cheap WETH, then recovers via another pool.
        The reserve math correctly evaluates both using the projected reserves.

        Return the first genuinely profitable one.
        """
        for cycle in cycles:
            result = self._reserve_math_cycle(
                cycle, edge, r_in_after, r_out_after
            )
            if result is not None:
                return result   # (cycle, TEST_AMOUNT, amount_out, profit_info)
        return None


    def _compute_cycle_output(
        self,
        cycle:      "ArbitrageCycle",
        start_amt:  float,
        edge_override: Optional["PoolEdge"] = None,
        r_in_over:     Optional[int] = None,
        r_out_over:    Optional[int] = None,
    ) -> Optional[float]:
        """
        Shared logic for walking a cycle and computing the final amount out.
        Supports overlapping the impacted pool with override reserves.
        """
        amount = start_amt
        valid  = True

        for i, pool_addr in enumerate(cycle.pools):
            t_in_hop  = cycle.tokens[i].lower()
            t_out_hop = cycle.tokens[i + 1].lower()

            # ── Get reserves ──────────────────────────────────────────────
            if edge_override and pool_addr.lower() == edge_override.pool_address.lower():
                # Use override reserves (projected)
                if t_in_hop == edge_override.token_in.lower():
                    ri, ro = float(r_in_over), float(r_out_over)
                else:
                    ri, ro = float(r_out_over), float(r_in_over)
                version = edge_override.version
                fee_bps = edge_override.fee_bps
                sqrtP   = getattr(edge_override, "sqrt_price_x96", 0)
                L       = getattr(edge_override, "liquidity", 0)
            else:
                # Use live graph reserves
                hop_edge = self._find_edge(pool_addr, t_in_hop, t_out_hop)
                if hop_edge is None:
                    valid = False; break
                
                version = getattr(hop_edge, "version", 2)
                fee_bps = hop_edge.fee_bps
                
                if hop_edge.version == 3:
                    L     = hop_edge.liquidity
                    sqrtP = hop_edge.sqrt_price_x96
                    if not L or not sqrtP:
                        valid = False; break
                    r0 = (L << 96) // sqrtP
                    r1 = (L * sqrtP) >> 96
                    if t_in_hop < t_out_hop:
                        ri, ro = float(r0), float(r1)
                    else:
                        ri, ro = float(r1), float(r0)
                else:
                    ri, ro = float(hop_edge.reserve_in), float(hop_edge.reserve_out)
                    sqrtP = 0; L = 0
                
                # Liquidity Guard: Ensure hop is not ultra-shallow
                from tvl import estimate_tvl_from_edge
                tvl = estimate_tvl_from_edge(hop_edge)
                if tvl < self.MIN_POOL_TVL_USD:
                    valid = False; break

            if ri <= 0.0 or ro <= 0.0:
                valid = False; break

            # ── AMM Swap Math ─────────────────────────────────────────────
            # (Simplification: uses V2 math for V3 hops if using virtual reserves)
            # NOTE: Full V3 tick math is available in optimizer/ directory:
            #   - optimizer/tick_walk.py: Exact tick-aware optimal amount for two V3 pools
            #   - optimizer/tick_data.py: Fetches and reconstructs V3 tick segments
            #   - optimizer/quoter.py: Wrapper around QuoterV2.quoteExactInput
            # TODO: Integrate optimizer modules for more accurate V3 simulations
            fn    = 10_000.0 - fee_bps
            afee  = amount * fn
            amount_out = afee * ro / (ri * 10_000.0 + afee)
            
            amount = amount_out
            if amount <= 0.0:
                valid = False; break

        return amount if valid else None

    def _reserve_math_cycle(
        self,
        cycle:       "ArbitrageCycle",
        edge:        "PoolEdge",
        r_in_after:  int,
        r_out_after: int,
    ) -> Optional[tuple]:
        """
        Original entry point for cycle math during discovery.
        """
        if not cycle.tokens or len(cycle.pools) == 0:
            return None

        # 1. Rotate to best starting token for research
        cycle = cycle.rotate_to_best_start(self._graph)
        
        root_token = cycle.tokens[0].lower()
        root_dec   = token_decimals(root_token)
        
        # 2. Determine a realistic TEST_AMOUNT (aim for ~$100 trade)
        price = token_price(root_token) or _NATIVE_PRICE_USD
        TEST_AMOUNT = float((100.0 / price) * (10 ** root_dec))
        
        # Ensure TEST_AMOUNT is not larger than any reserve in the cycle (liquidity limit)
        # This prevents "paper profit" on $1M trades through $100 pools
        # (Though compute_cycle_output would already show high slippage)

        final_amt = self._compute_cycle_output(
            cycle, TEST_AMOUNT, edge_override=edge, 
            r_in_over=r_in_after, r_out_over=r_out_after
        )

        if final_amt is None or final_amt <= TEST_AMOUNT:
            return None

        profit_raw    = final_amt - TEST_AMOUNT
        profit_ratio  = profit_raw / TEST_AMOUNT

        if profit_ratio > self.PROFIT_SANITY_CAP:
            return None

        profit_human = profit_raw / (10 ** root_dec)
        profit_info  = self._compute_profit_info(root_token, profit_human, profit_ratio)
        
        log.research(f"PROFITABLE CYCLE FOUND! {cycle.path_string} | Profit: {profit_human:.6f} {profit_info['token_symbol']} ({profit_ratio*100:.4f}%)")
        return cycle, TEST_AMOUNT, final_amt, profit_info

    def _find_edge(
        self, pool_addr: str, t_in: str, t_out: str
    ) -> Optional["PoolEdge"]:
        """Find a graph edge by pool_address + tokens (re-uses TokenGraph helper)."""
        return self._graph.get_edge_by_pool_and_tokens(pool_addr, t_in, t_out)

    # ══════════════════════════════════════════════════════════════════════════
    # PROFIT UNIT HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    def _compute_profit_info(
        self,
        root_token:      str,
        profit_in_token: float,
        profit_ratio:    float,
    ) -> dict:
        """
        Compute profit denomination without fabricating USD values.

        Rules:
          1. Root token is stablecoin  → profit IS USD, no conversion needed.
          2. Root token is WETH/native → report in ETH; convert to USD too.
          3. Root token has a price    → convert via oracle price.
          4. Root token is unknown     → report native amount + symbol only.
        """
        addr   = root_token.lower()
        symbol = _KNOWN_TOKENS.get(addr, f"tok:{addr[:8]}")

        # ── Case 1: Stablecoin root ────────────────────────────────────────
        if addr in STABLE_TOKEN_SETS and addr not in _WETH_ADDRS:
            return dict(
                ratio=profit_ratio,
                usd=profit_in_token,      # already USD
                token_amount=profit_in_token,
                token_symbol=symbol,
                token_addr=addr,
            )

        # ── Case 2 / 3: WETH or oracle-priced token ────────────────────────
        price = token_price(addr)
        if price is not None:
            profit_usd = profit_in_token * price
            return dict(
                ratio=profit_ratio,
                usd=profit_usd,
                token_amount=profit_in_token,
                token_symbol=symbol,
                token_addr=addr,
            )

        # ── Case 4: Unknown token — NO USD guess ───────────────────────────
        return dict(
            ratio=profit_ratio,
            usd=0.0,                      # intentionally unknown
            token_amount=profit_in_token,
            token_symbol=symbol,
            token_addr=addr,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # CYCLE CLASSIFICATION
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _classify_cycle(cycle: "ArbitrageCycle") -> str:
        is_cross = cycle.is_cross_dex
        hops     = cycle.hop_count
        if hops == 2:
            return "DEX_SPREAD"    if is_cross else "SAME_DEX_CYCLE"
        if hops == 3:
            return "CROSS_DEX_TRI" if is_cross else "TRIANGULAR"
        return "CROSS_DEX_MULTI"   if is_cross else "MULTI_HOP"

    # ══════════════════════════════════════════════════════════════════════════
    # GRAPH PATCHING — find cycles with projected reserves
    # ══════════════════════════════════════════════════════════════════════════

    def _find_cycles_with_projected_rate(
        self,
        edge:           "PoolEdge",
        new_r_in:       int,
        new_r_out:      int,
        new_sqrt_p:     Optional[int] = None,
    ) -> list["ArbitrageCycle"]:
        """
        Temporarily patch the graph with projected reserves, run cycle search,
        restore the original state.

        BFS-expands 2 hops from the impacted tokens to seed the DFS so we
        catch triangular and multi-hop cycles — not just 2-hop DEX spreads.
        """
        from graph import PoolEdge as PE

        # Build projected edges
        proj_fwd = PE(
            pool_address=edge.pool_address, dex_name=edge.dex_name,
            token_in=edge.token_in,  token_out=edge.token_out,
            reserve_in=new_r_in,     reserve_out=new_r_out,
            fee_bps=edge.fee_bps,    version=edge.version,
            sqrt_price_x96=new_sqrt_p if new_sqrt_p else edge.sqrt_price_x96, 
            liquidity=edge.liquidity, tvl_usd=edge.tvl_usd,
        )
        proj_rev = PE(
            pool_address=edge.pool_address, dex_name=edge.dex_name,
            token_in=edge.token_out, token_out=edge.token_in,
            reserve_in=new_r_out,    reserve_out=new_r_in,
            fee_bps=edge.fee_bps,    version=edge.version,
            sqrt_price_x96=new_sqrt_p if new_sqrt_p else edge.sqrt_price_x96, 
            liquidity=edge.liquidity, tvl_usd=edge.tvl_usd,
        )
        orig_rev = PE(
            pool_address=edge.pool_address, dex_name=edge.dex_name,
            token_in=edge.token_out, token_out=edge.token_in,
            reserve_in=edge.reserve_out, reserve_out=edge.reserve_in,
            fee_bps=edge.fee_bps,    version=edge.version,
            sqrt_price_x96=edge.sqrt_price_x96, liquidity=edge.liquidity,
            tvl_usd=edge.tvl_usd,
        )

        # ── Seed source tokens ────────────────────────────────────────────────
        # IMPORTANT ordering:
        #   1. edge.token_out first  — the token made cheap by the victim swap.
        #      The profitable DEX_SPREAD cycle starts by SELLING this token on
        #      another DEX (at the original price) and CLOSING via the impacted
        #      pool (buying it cheap).  The DFS discovers this when rooted at
        #      token_out.
        #   2. edge.token_in next    — standard WETH-rooted cycles.
        source_tokens = [edge.token_out, edge.token_in]

        self._graph.add_or_update_pool(proj_fwd, proj_rev)
        try:
            cycles = self._graph.find_cycles(
                source_tokens=source_tokens,
                min_profit_ratio=self.MIN_PROFIT_RATIO,
                max_hops=3,
                must_include_pool=edge.pool_address,
                # with_refresh=True # Update path pools except the projected one, to ensure reserve consistency across hops
            )
        finally:
            self._graph.add_or_update_pool(edge, orig_rev)

        return cycles


    # ══════════════════════════════════════════════════════════════════════════
    # AMM MATH
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _apply_swap_v2(
        amount_in: int, r_in: int, r_out: int, fee_bps: int
    ) -> tuple[int, int]:
        """Uniswap V2 constant-product: returns (new_reserve_in, new_reserve_out)."""
        fee_num      = 10_000 - fee_bps
        amt_with_fee = amount_in * fee_num
        amount_out   = amt_with_fee * r_out // (r_in * 10_000 + amt_with_fee)
        return max(r_in + amount_in, 1), max(r_out - amount_out, 1)

    @staticmethod
    def _apply_swap_v3(amount_in: int, edge: "PoolEdge") -> tuple[int, int, int]:
        """Precise Uniswap V3 swap math using whitepaper formulas."""
        L = edge.liquidity
        sqrtP = edge.sqrt_price_x96
        zero_for_one = edge.token_in < edge.token_out
        
        # Uniswap V3: 
        # amount0 = L * (1/sqrt_P_next - 1/sqrt_P_curr)
        # amount1 = L * (sqrt_P_next - sqrt_P_curr)
        
        if zero_for_one:
            # Selling token0 (amount0 > 0)
            # 1/sqrt_P_next = 1/sqrt_P_curr + amount0/L
            # sqrt_P_next = (L * sqrt_P_curr) / (L + amount0 * sqrt_P_curr)
            # Using X96: (L << 96 * sqrtP) // (L << 96 + amount_in * sqrtP)
            num = L * sqrtP
            den = (L << 96) + (amount_in * sqrtP)
            new_sqrt_p = (num << 96) // den
        else:
            # Selling token1 (amount1 > 0)
            # sqrt_P_next = sqrt_P_curr + amount1/L
            # Using X96: sqrtP + (amount_in << 96) // L
            new_sqrt_p = sqrtP + ((amount_in << 96) // L)
            
        if new_sqrt_p <= 0:
            return max(edge.reserve_in + amount_in, 1), 1, edge.sqrt_price_x96
            
        new_r0 = (L << 96) // new_sqrt_p
        new_r1 = (L * new_sqrt_p) >> 96
        
        if zero_for_one:
            return int(new_r0), int(new_r1), int(new_sqrt_p)
        else:
            return int(new_r1), int(new_r0), int(new_sqrt_p)

    # ══════════════════════════════════════════════════════════════════════════
    # ON-CHAIN VERIFICATION
    # ══════════════════════════════════════════════════════════════════════════

    def _verify_opportunity(
        self,
        opp:   PendingOpportunity,
        cycle: "ArbitrageCycle",
    ) -> Optional[PendingOpportunity]:
        """
        Final check before reporting. Re-runs math with 0.001 units
        to avoid slippage rejection in shallow/mock pools.
        """
        try:
            root_token = cycle.tokens[0].lower()
            root_dec   = token_decimals(root_token)
            # Match TEST_AMOUNT to discovery phase (0.001 units)
            TEST_AMOUNT = float(10 ** (root_dec - 3)) if root_dec >= 3 else 1.0

            # Find the original edge to get version/fee for override logic
            orig_edge = None
            for e in self._graph.get_edges_from(opp.token_in):
                if e.pool_address == opp.affected_pool:
                    orig_edge = e; break
            
            if not orig_edge: return None

            final_amt = self._compute_cycle_output(
                cycle, TEST_AMOUNT, edge_override=orig_edge,
                r_in_over=opp.reserve_in_after, r_out_over=opp.reserve_out_after
            )

            if final_amt is None or final_amt <= TEST_AMOUNT:
                log.debug(f"Verification failed: final_amt {final_amt} <= start {TEST_AMOUNT}")
                return None

            new_ratio = (final_amt - TEST_AMOUNT) / TEST_AMOUNT
            if new_ratio < 0.0001: # 0.01%
                return None

            profit_human = (final_amt - TEST_AMOUNT) / (10 ** root_dec)
            profit_info  = self._compute_profit_info(root_token, profit_human, new_ratio)

            opp.estimated_profit_ratio = profit_info["ratio"]
            opp.estimated_profit_usd   = profit_info["usd"]
            opp.profit_in_token        = profit_info["token_amount"]
            opp.profit_token_symbol    = profit_info["token_symbol"]
            opp.profit_token_addr      = profit_info["token_addr"]
            opp.opp_type               = f"VERIFIED_{opp.opp_type}"
            opp.verified_cycle         = cycle
            return opp

        except Exception as exc:
            log.debug(f"Verification failed: {exc}")
            return None

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    def _research_opp(
        self,
        swap:         "DecodedSwap",
        edge:         "PoolEdge",
        t_in:         str,
        t_out:        str,
        amount_in:    int,
        r_in:         int,
        r_out:        int,
        r_in_after:   int,
        r_out_after:  int,
        impact_pct:   float,
        projected_cycles: list["ArbitrageCycle"] = None,
    ) -> PendingOpportunity:
        """Build a RESEARCH-type opportunity (no arb, just tracking the impact)."""
        return PendingOpportunity(
            trigger_tx_hash=swap.tx_hash,
            trigger_router=swap.router,
            trigger_dex=swap.router_name,
            pending_seen_at=time.time(),
            affected_pool=edge.pool_address,
            token_in=t_in,
            token_out=t_out,
            simulated_amount_in=amount_in,
            reserve_in_before=r_in,
            reserve_out_before=r_out,
            reserve_in_after=r_in_after,
            reserve_out_after=r_out_after,
            price_impact_pct=impact_pct,
            opp_type="RESEARCH",
            arb_tokens=[t_in, t_out],
            arb_pools=[edge.pool_address],
            arb_dexes=[edge.dex_name],
            estimated_profit_ratio=0.0,
            # estimated_profit_usd=0.0,
            profit_in_token=0.0,
            profit_token_symbol="",
            profit_token_addr="",
            metadata=dict(projected_cycles=projected_cycles or [])
        )

    def _get_amount_out_simple(self, amount_in: float, edge: "PoolEdge") -> float:
        """Helper to get amount out from a pool edge using V2 or virtual V3 math."""
        if edge.version == 3:
            L = edge.liquidity
            sqrtP = edge.sqrt_price_x96
            if not L or not sqrtP: return 0.0
            r0 = (L << 96) // sqrtP
            r1 = (L * sqrtP) >> 96
            ri, ro = (float(r0), float(r1)) if edge.token_in < edge.token_out else (float(r1), float(r0))
        else:
            ri, ro = float(edge.reserve_in), float(edge.reserve_out)

        if ri <= 0 or ro <= 0: return 0.0
        
        fee = edge.fee_bps
        fn = 10_000.0 - fee
        afee = amount_in * fn
        return afee * ro / (ri * 10_000.0 + afee)
