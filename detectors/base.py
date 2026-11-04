"""
Enhanced Base Detector
==================
Base class for all opportunity detectors with shared utilities.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from web3 import Web3
    from graph import TokenGraph, ArbitrageCycle, PoolEdge
    from logger import OppLogger, Opportunity
    from cache import PoolCache

from config import (
    get_chain, ARB_EXEC_ABI,
    NATIVE_PRICE_USD, MIN_PROFIT_USD,
    DEFAULT_GAS_UNITS, GAS_MULTIPLIER,
)
from tvl import token_price as _oracle_price
from optimizer.quoter import quote_exact_input
from executor.builder import build_arb_data
from log import get_logger
log = get_logger("base")

ARB_EXEC_ADDRESS= get_chain().arb_exec_address

class BaseDetector(ABC):
    """Base class for all opportunity detectors."""
    
    name: str = "base"
    
    def __init__(self, w3: "Web3", graph: "TokenGraph", logger: "OppLogger", cache: "PoolCache", chain_id: int):
        self.w3 = w3
        self.graph = graph
        self.logger = logger
        self.cache = cache
        self.chain_id = chain_id
        self._arb_exec = None
        
        if ARB_EXEC_ADDRESS:
            try:
                self._arb_exec = w3.eth.contract(
                    address=w3.to_checksum_address(ARB_EXEC_ADDRESS),
                    abi=ARB_EXEC_ABI,
                )
            except Exception as e:
                log.warning(f"Could not load ArbExec contract: {e}")
    
    @abstractmethod
    def on_block(self, block_number: int) -> list["Opportunity"]:
        """Called on every new block."""
        ...
    
    def on_new_pool(self, pool_address: str, dex_name: str) -> None:
        """Called when a new pool is detected."""
        pass
    
    # ══════════════════════════════════════════════════════════════════════════════
    # SIMULATION UTILITIES
    # ══════════════════════════════════════════════════════════════════════════════
    
    def simulate_cycle(
        self,
        cycle: "ArbitrageCycle",
        amount_in_wei: int,
    ) -> tuple[tuple | None, int, bool]:
        """
        Simulate arbitrage cycle.
        Returns (ad_tuple, profit_raw, via_contract).
        ad_tuple: sorted ArbData from yieldOut() — None for 3+ pools or fallback
        profit_raw: profit in token base units
        via_contract: True if contract simulation was used
        """
        # 2-pool: use yieldOut(amountIn=0) — auto-calculates optimal on-chain
        if self._arb_exec:
            ad, profit = self._simulate_via_contract(cycle, 0)
            if ad is not None and profit > 0:
                return ad, profit, True
    
        # contract unavailable: slippage math with probe amount
        # multicall3_bulk_refresh(self.w3, self.graph, self.cache, cycle.pools)
        profit_raw = self._simulate_slippage_profit(cycle, amount_in_wei)
        if profit_raw and profit_raw > 0:
            return None, profit_raw, False
        return None, 0, False
    
    def _simulate_via_contract(
        self,
        cycle: "ArbitrageCycle",
        amount_in_wei: int,
    ) -> tuple[tuple | None, int]:
        """
        Call ArbExec.yieldOut() via eth_call.
        Returns (ad_tuple, profit_wei) where ad_tuple is the full sorted ArbData.
        Returns (None, 0) on failure.
        """
        try:
            # arb_data = {
            #     "amountIn":   amount_in_wei,
            #     "minProfit":  0,
            #     "tokens":     [self.w3.to_checksum_address(t) for t in cycle.tokens],
            #     "pools":      [self.w3.to_checksum_address(p) for p in cycle.pools],
            #     "fees":       [f * 100 for f in cycle.fees],
            #     "tokenIn":    self.w3.to_checksum_address(cycle.tokens[0]),
            #     "mode":       0,
            # }
            arb_data = (
                int(amount_in_wei),
                0,
                [self.w3.to_checksum_address(t) for t in cycle.tokens],
                [self.w3.to_checksum_address(p) for p in cycle.pools],
                [int(f * 100) for f in cycle.fees],
                self.w3.to_checksum_address(cycle.tokens[0]),
                0,
            )
            # arb_data = build_arb_data(
            #     ad_tuple=None, 
            #     opp_tokens=cycle.tokens, 
            #     opp_pools=cycle.pools, 
            #     opp_fees_pip=[f * 100 for f in cycle.fees],
            #     amount_in=amount_in_wei,
            #     token_in=cycle.tokens[0],
            #     mode=0,
            # )
            ad, profit = self._arb_exec.functions.yieldOut(arb_data).call()
            return ad, profit
        except Exception as e:
            # print((f"Contract simulation error: {e}"))
            log.debug(f"Contract simulation error: {e}")
            return None, 0
    
    def _wei_to_usd(self, wei: int, token_address: str) -> float:
        """Convert wei to USD."""
        price = _oracle_price(token_address) or NATIVE_PRICE_USD
        return (wei / 1e18) * price
    
    def _estimate_gas_usd(self, gas_units: int = DEFAULT_GAS_UNITS) -> float:
        """Estimate gas cost in USD."""
        try:
            from config import get_chain
            native_addr = get_chain().wrapped_native
            native_price = _oracle_price(native_addr) or NATIVE_PRICE_USD
            gas_price_wei = self.w3.eth.gas_price
            gas_cost_eth = (gas_price_wei * gas_units * GAS_MULTIPLIER) / 1e18
            return gas_cost_eth * native_price
        except Exception as e:
            log.debug(f"Gas cost estimate failed: {e}")
            return 0.0
    
    def _reserve_math_out(
        self,
        amount_in: int,
        reserve_in: int,
        reserve_out: int,
        fee_bps: int = 30,
    ) -> int:
        """Standard x*y=k output with fee."""
        if reserve_in == 0 or reserve_out == 0:
            return 0
        fee_num = 10_000 - fee_bps
        num = amount_in * fee_num * reserve_out
        den = reserve_in * 10_000 + amount_in * fee_num
        return num // den if den > 0 else 0
    
    def _is_above_min_profit(self, net_profit_usd: float) -> bool:
        return net_profit_usd >= MIN_PROFIT_USD

    def _simulate_v3_swap_exact_in(
        self,
        amount_in: int,
        sqrt_price_x96: int,
        liquidity: int,
        fee_bps: int,
        zero_for_one: bool
    ) -> int:
        """
        PRODUCTION SPEC 4.2 (CRITICAL): Exact V3 EVM Math.
        Computes the exact slippage output mathematically avoiding off-by-one tick reverts.
        Since layer 1 nominal amount is $10, we compute strictly within the current tick range.
        Optimality plugin handles multi-tick calculus.
        """
        if amount_in <= 0 or liquidity <= 0 or sqrt_price_x96 <= 0:
            return 0
            
        fee_amount = (amount_in * fee_bps) // 10000
        amount_in_less_fee = amount_in - fee_amount
        
        Q96 = 2**96
        
        if zero_for_one: # Token0 -> Token1 (Price moves DOWN)
            # P_next = (L * Q96 * P_old) / (L * Q96 + amt_in * P_old)
            numerator = liquidity * Q96 * sqrt_price_x96
            denominator = (liquidity * Q96) + (amount_in_less_fee * sqrt_price_x96)
            sqrt_price_next = numerator // denominator
            
            if sqrt_price_next >= sqrt_price_x96: 
                return 0 # Underflow safety shield
            
            # Amount Out (Token1) = L * (P_old - P_next) / Q96
            amount_out = (liquidity * (sqrt_price_x96 - sqrt_price_next)) // Q96
            
        else: # Token1 -> Token0 (Price moves UP)
            # P_next = P_old + (amount_in * Q96 / L)
            delta_p = (amount_in_less_fee * Q96) // liquidity
            sqrt_price_next = sqrt_price_x96 + delta_p
            
            # Amount Out (Token0) = L * Q96 * (P_next - P_old) / (P_next * P_old)
            numerator = liquidity * Q96 * (sqrt_price_next - sqrt_price_x96)
            denominator = sqrt_price_next * sqrt_price_x96
            amount_out = numerator // denominator
            
        return amount_out

    def _simulate_slippage_profit(self, cycle: "ArbitrageCycle", amount_in: int) -> Optional[int]:
        """
        Walks the cycle path and computes real output using AMM reserves (simulates slippage).
        Returns net profit in raw token units.
        """
        try:
            if all(x == "UniswapV3" for x in cycle.dexes):
                current_amount = quote_exact_input(
                    self.w3,
                    cycle.tokens,
                    [int(cycle.fees[i] * 100) for i in range(len(cycle.fees))],
                    int(amount_in)
                )
                if current_amount:
                    # log.info(f"Final amount: {current_amount}, amount_in: {amount_in}, profit: {current_amount - amount_in}")
                    return current_amount - amount_in
                return None

            current_amount = amount_in
            for i, pool_addr in enumerate(cycle.pools):
                t_in = cycle.tokens[i]
                t_out = cycle.tokens[i + 1]
                edge = self.graph.get_edge_by_pool_and_tokens(pool_addr, t_in, t_out)
                
                if not edge:
                    return None
                    
                if edge.version == 2:
                    if edge.reserve_in <= 0 or edge.reserve_out <= 0:
                        return None
                    current_amount = self._reserve_math_out(
                        current_amount, edge.reserve_in, edge.reserve_out, edge.fee_bps
                    )
                elif edge.version == 3:
                    # Production Spec 4.2 - Exact V3 EVM tick simulation instead of spot-rate approximation
                    # Ascertain zeroForOne direction mathematically matching Uniswap pool logic
                    # token0 = min(edge.token_in.lower(), edge.token_out.lower())
                    # zero_for_one = (edge.token_in.lower() == token0)
                    
                    # current_amount = self._simulate_v3_swap_exact_in(
                    #     amount_in=current_amount,
                    #     sqrt_price_x96=edge.sqrt_price_x96,
                    #     liquidity=edge.liquidity,
                    #     fee_bps=edge.fee_bps,
                    #     zero_for_one=zero_for_one
                    # )
                    current_amount = quote_exact_input(
                        self.w3,
                        [t_in, t_out],
                        [int(cycle.fees[i] * 100)],
                        int(current_amount)
                    )
                else:
                    return None
                    
                if current_amount is None or current_amount <= 0:
                    return None
            log.info(f"Final amount: {current_amount}, amount_in: {amount_in}, profit: {current_amount - amount_in}")
            return current_amount - amount_in
        except Exception as e:
            log.error(f"Error simulating slippage profit: {e}")
            return None

    # def _simulate_slippage_profit(self, cycle: "ArbitrageCycle", amount_in: int) -> Optional[int]:
    #     """
    #     Walks the cycle path and computes real output using AMM reserves (simulates slippage).
    #     Returns net profit in raw token units.
    #     """
    #     current_amount = amount_in
    #     for i, pool_addr in enumerate(cycle.pools):
    #         t_in = cycle.tokens[i]
    #         t_out = cycle.tokens[i + 1]
    #         edge = self.graph.get_edge_by_pool_and_tokens(pool_addr, t_in, t_out)
            
    #         if not edge:
    #             return None
                
    #         if edge.version == 2:
    #             if edge.reserve_in <= 0 or edge.reserve_out <= 0:
    #                 return None

    #             r0 = edge.reserve_in
    #             r1 = edge.reserve_out
    #         elif edge.version == 3:
    #             r0 = (edge.liquidity << 96) // edge.sqrt_price_x96 if edge.sqrt_price_x96 else 0
    #             r1 = (edge.liquidity * edge.sqrt_price_x96) >> 96
    #         else:
    #             return None

    #         res_in, res_out = (r0, r1) if t_in == edge.token_in else (r1, r0)
    #         current_amount = self._reserve_math_out(current_amount, res_in, res_out, int(edge.fee_bps))
                
    #         if current_amount <= 0:
    #             log.warning(f"Current amount is zero or negative: {current_amount}")
    #             return None
    #     log.info(f"Final amount: {current_amount}, amount_in: {amount_in}, profit: {current_amount - amount_in}")
    #     return current_amount - amount_in
    
    # ══════════════════════════════════════════════════════════════════════════════
    # COMPETITION ANALYSIS
    # ══════════════════════════════════════════════════════════════════════════════
    
    def _estimate_competition(
        self,
        pools: list[str],
        dexes: list[str],
    ) -> str:
        """
        Estimate competition level for an opportunity.
        Returns: "low", "medium", or "high"
        """
        # Known high-competition pools
        HIGH_COMPETITION_POOLS = {
            "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",  # USDC/ETH UniV3 5bp
            "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8",  # USDC/ETH UniV3 30bp
        }
        
        high_comp_count = sum(1 for p in pools if p.lower() in HIGH_COMPETITION_POOLS)
        
        if high_comp_count > 0:
            return "high"
        
        # Check for common DEX patterns
        unique_dexes = set(dexes)
        if len(unique_dexes) > 1:
            return "medium"  # Cross-DEX is more competitive
        
        return "low"
