"""
Enhanced Specialized Detectors
=========================
- New Pool Detector
- Cross-Protocol Detector  
- Liquidation Scanner
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from detectors.base import BaseDetector
from graph import PoolEdge, TokenGraph
from logger import Opportunity, OppType

if TYPE_CHECKING:
    from logger import OppLogger

from config import (
    get_chain, UNISWAP_V2_FACTORY_ABI, UNISWAP_V3_FACTORY_ABI,
    UNISWAP_V2_PAIR_ABI, UNISWAP_V3_POOL_ABI, ERC20_ABI,
    AAVE_POOL_ABI, MIN_LIQUIDITY_USD, NATIVE_PRICE_USD,
)
from tvl import estimate_tvl_v2, estimate_tvl_v3, is_above_min_liquidity
from log import get_logger
log = get_logger("specialized")


# ═══════════════════════════════════════════════════════════════════════════════
# NEW POOL DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════

class NewPoolDetector(BaseDetector):
    """
    Watches factory PairCreated/PoolCreated events.
    Detects new pools and flags new token listings.
    """
    name = "new_pool"
    
    def __init__(self, w3, graph, logger, cache, chain_id):
        super().__init__(w3, graph, logger, cache, chain_id)
        self._chain = get_chain()
        self._known_pools: set[str] = set()
        self._known_tokens: set[str] = set()
        self._factory_contracts: list = []
        
        for dex in self._chain.dexes:
            try:
                abi = UNISWAP_V2_FACTORY_ABI if dex.version == 2 else UNISWAP_V3_FACTORY_ABI
                c = w3.eth.contract(
                    address=w3.to_checksum_address(dex.factory),
                    abi=abi,
                )
                self._factory_contracts.append((c, dex.version, dex.name, dex.fee_bps))
            except Exception as e:
                log.warning(f"Could not load factory {dex.name}: {e}")
    
    def on_block(self, block_number: int) -> list[Opportunity]:
        opportunities = []
        
        for factory, version, dex_name, fee_bps in self._factory_contracts:
            try:
                event_name = "PairCreated" if version == 2 else "PoolCreated"
                event = getattr(factory.events, event_name)
                logs = event.get_logs(fromBlock=block_number, toBlock=block_number)
                
                for log in logs:
                    pool_addr = log["args"].get("pair") or log["args"].get("pool")
                    token0 = log["args"]["token0"]
                    token1 = log["args"]["token1"]
                    fee = log["args"].get("fee", fee_bps * 100) // 100 if version == 3 else fee_bps
                    
                    if pool_addr in self._known_pools:
                        continue
                    self._known_pools.add(pool_addr)
                    
                    # Load pool and add to graph
                    edge_a, edge_b = self._load_pool(
                        pool_addr, token0, token1, fee, version, dex_name
                    )
                    if edge_a and edge_b:
                        self.graph.add_or_update_pool(edge_a, edge_b)
                    
                    # Check for new token
                    is_new_token = (
                        token0 not in self._known_tokens or
                        token1 not in self._known_tokens
                    )
                    self._known_tokens.add(token0)
                    self._known_tokens.add(token1)
                    
                    # Calculate TVL
                    tvl = 0
                    if edge_a:
                        tvl = edge_a.tvl_usd
                    
                    opp = Opportunity(
                        opp_type=OppType.NEW_POOL,
                        chain_id=self.chain_id,
                        block_number=block_number,
                        timestamp=time.time(),
                        tokens=[token0, token1],
                        pools=[pool_addr],
                        dexes=[dex_name],
                        estimated_profit_usd=0,
                        net_profit_usd=0,
                        metadata={
                            "is_new_token": is_new_token,
                            "version": version,
                            "fee_bps": fee,
                            "tvl_usd": round(tvl, 2),
                            "note": "NEW TOKEN LISTING" if is_new_token else "New pool",
                        },
                    )
                    opportunities.append(opp)
            
            except Exception:
                pass
        
        # return opportunities
        return []
    
    def _load_pool(self, pool_addr, token0, token1, fee_bps, version, dex_name):
        try:
            if version == 2:
                c = self.w3.eth.contract(
                    address=self.w3.to_checksum_address(pool_addr),
                    abi=UNISWAP_V2_PAIR_ABI,
                )
                r0, r1, _ = c.functions.getReserves().call()
                tvl = estimate_tvl_v2(token0, token1, r0, r1)
                
                if not is_above_min_liquidity(tvl, MIN_LIQUIDITY_USD):
                    return None, None
                
                return (
                    PoolEdge(pool_addr, dex_name, token0, token1, r0, r1, fee_bps, 2, tvl_usd=tvl),
                    PoolEdge(pool_addr, dex_name, token1, token0, r1, r0, fee_bps, 2, tvl_usd=tvl),
                )
            else:
                c = self.w3.eth.contract(
                    address=self.w3.to_checksum_address(pool_addr),
                    abi=UNISWAP_V3_POOL_ABI,
                )
                slot0 = c.functions.slot0().call()
                liq = c.functions.liquidity().call()
                sqrt = slot0[0]
                tvl = estimate_tvl_v3(token0, token1, sqrt, liq)
                
                if not is_above_min_liquidity(tvl, MIN_LIQUIDITY_USD):
                    return None, None
                
                inv = int(2**192 / sqrt**2 * 2**96) if sqrt > 0 else 0
                return (
                    PoolEdge(pool_addr, dex_name, token0, token1, 0, 0, fee_bps, 3,
                            sqrt_price_x96=sqrt, liquidity=liq, tvl_usd=tvl),
                    PoolEdge(pool_addr, dex_name, token1, token0, 0, 0, fee_bps, 3,
                            sqrt_price_x96=inv, liquidity=liq, tvl_usd=tvl),
                )
        except:
            return None, None


# ═══════════════════════════════════════════════════════════════════════════════
# CROSS-PROTOCOL DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════

class CrossProtocolDetector(BaseDetector):
    """
    Detects price differences across protocol versions.
    Compares V2, V3, and different DEX prices for the same pair.
    """
    name = "cross_proto"
    
    MIN_SPREAD_PCT = 0.3
    
    def on_block(self, block_number: int) -> list[Opportunity]:
        opportunities = []
        chain = get_chain()
        
        anchor_tokens = [chain.wrapped_native] + chain.stablecoins
        
        checked: set = set()
        for i, t0 in enumerate(anchor_tokens):
            for t1 in anchor_tokens[i+1:]:
                key = frozenset([t0, t1])
                if key in checked:
                    continue
                checked.add(key)
                
                edges = self.graph.all_edges_for_pair(t0, t1)
                if len(edges) < 2:
                    continue
                
                edges_sorted = sorted(edges, key=lambda e: e.exchange_rate, reverse=True)
                best = edges_sorted[0]
                worst = edges_sorted[-1]
                
                if worst.exchange_rate == 0:
                    continue
                
                spread_pct = (best.exchange_rate - worst.exchange_rate) / worst.exchange_rate * 100
                
                if spread_pct < self.MIN_SPREAD_PCT:
                    continue
                
                amount_usd = NATIVE_PRICE_USD
                gross_usd = amount_usd * (spread_pct / 100.0)
                gas_usd = self._estimate_gas_usd()
                net_usd = gross_usd - gas_usd
                
                if not self._is_above_min_profit(net_usd):
                    continue
                
                competition = self._estimate_competition(
                    [best.pool_address, worst.pool_address],
                    [best.dex_name, worst.dex_name]
                )
                
                opp = Opportunity(
                    opp_type=OppType.CROSS_PROTOCOL,
                    chain_id=self.chain_id,
                    block_number=block_number,
                    timestamp=time.time(),
                    tokens=[t0, t1],
                    pools=[best.pool_address, worst.pool_address],
                    dexes=[best.dex_name, worst.dex_name],
                    estimated_profit_ratio=spread_pct / 100.0,
                    estimated_profit_usd=gross_usd,
                    gas_cost_usd=gas_usd,
                    net_profit_usd=net_usd,
                    competition_level=competition,
                    metadata={
                        "spread_pct": round(spread_pct, 4),
                        "best_rate": best.exchange_rate,
                        "worst_rate": worst.exchange_rate,
                        "best_version": best.version,
                        "worst_version": worst.version,
                    },
                )
                opportunities.append(opp)
        
        return opportunities


# ═══════════════════════════════════════════════════════════════════════════════
# LIQUIDATION SCANNER
# ═══════════════════════════════════════════════════════════════════════════════

# In production, populate from Aave events
WATCH_ADDRESSES: list[str] = []


class LiquidationScanner(BaseDetector):
    """
    Monitors Aave positions for liquidation opportunities.
    Health factor < 1.05 = imminent, < 1.15 = at risk.
    """
    name = "liquidation"
    
    IMMINENT_THRESHOLD = 1.05
    AT_RISK_THRESHOLD = 1.15
    
    def __init__(self, w3, graph, logger, cache, chain_id):
        super().__init__(w3, graph, logger, cache, chain_id)
        chain = get_chain()
        self._aave = None
        
        if chain.aave_pool:
            try:
                self._aave = w3.eth.contract(
                    address=w3.to_checksum_address(chain.aave_pool),
                    abi=AAVE_POOL_ABI,
                )
            except Exception as e:
                log.warning(f"Could not load Aave pool: {e}")
        
        self._watch: list[str] = WATCH_ADDRESSES[:]
    
    def add_watch(self, address: str) -> None:
        if address not in self._watch:
            self._watch.append(address)
    
    def on_block(self, block_number: int) -> list[Opportunity]:
        if not self._aave or not self._watch:
            return []
        
        opportunities = []
        
        for addr in self._watch:
            try:
                data = self._aave.functions.getUserAccountData(
                    self.w3.to_checksum_address(addr)
                ).call()
                
                total_collateral, total_debt, available_borrows, \
                    liquidation_threshold, ltv, health_factor_raw = data
                
                hf = health_factor_raw / 1e18
                
                if hf >= self.AT_RISK_THRESHOLD:
                    continue
                
                severity = "IMMINENT" if hf < self.IMMINENT_THRESHOLD else "AT_RISK"
                
                # Liquidation bonus is typically 5-10%
                debt_usd = total_debt / 1e8
                bonus_pct = 0.05
                gross_usd = debt_usd * bonus_pct
                gas_usd = self._estimate_gas_usd(gas_units=500_000)
                net_usd = gross_usd - gas_usd
                
                opp = Opportunity(
                    opp_type=OppType.LIQUIDATION,
                    chain_id=self.chain_id,
                    block_number=block_number,
                    timestamp=time.time(),
                    tokens=[],
                    pools=[],
                    dexes=["AAVE_V3"],
                    estimated_profit_usd=gross_usd,
                    gas_cost_usd=gas_usd,
                    net_profit_usd=net_usd,
                    metadata={
                        "borrower": addr,
                        "health_factor": round(hf, 4),
                        "severity": severity,
                        "total_collateral_usd": total_collateral / 1e8,
                        "total_debt_usd": debt_usd,
                        "liquidation_threshold": liquidation_threshold / 1e4,
                    },
                )
                opportunities.append(opp)
            
            except:
                pass
        
        return opportunities

