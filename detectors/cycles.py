"""

Enhanced Cycle Detector

==================

Detects DEX-DEX, Triangular, and Multi-hop arbitrage opportunities.

"""

from __future__ import annotations

import json
import time
import requests
import os
import concurrent.futures
from functools import lru_cache
from itertools import combinations
from typing import Optional, TYPE_CHECKING

from detectors.base import BaseDetector
from logger import Opportunity, OppType
from routing import ArbKind
from graph import ArbitrageCycle
from config import get_chain, get_extended_arb_exec_address, NATIVE_PRICE_USD, MIN_PROFIT_USD, DEFAULT_GAS_UNITS
from tvl import token_price, profit_in_best_unit, token_decimals
from log import get_logger
from utils.multicall3 import multicall3_batch_yieldOut, multicall3_batch_quote_v3
from utils.gecko_terminal import GeckoTerminal
log = get_logger("cycles")



# Amount to simulate with — 1 ETH equivalent in wei (18 decimals)

# For stablecoin-rooted cycles we rebase to 1000 USD worth later.

SIMULATION_AMOUNT_WEI = 10 ** 18


ARB_EXEC_ADDRESS = get_extended_arb_exec_address(get_chain().chain_id) or get_chain().arb_exec_address


def _filter_pool_conflicts(cycles: list) -> list:

    """

    Filter cycles to ensure no two selected cycles share any pool.

    Uses greedy selection based on log_weight_sum (lower is better).

    

    Algorithm:

    1. Sort cycles by log_weight_sum ascending (lower = better arbitrage)

    2. Greedy selection: pick best cycle, exclude all cycles sharing any of its pools

    3. Continue until no cycles remain

    """

    if not cycles:
        return []

    # Sort by log_weight_sum ascending - best (lowest) weight gets priority
    sorted_cycles = sorted(cycles, key=lambda c: c.log_weight_sum)

    selected = []
    used_pools: set[str] = set()
    unique_cycles = {}

    for cycle in sorted_cycles:
        c_key = tuple(sorted(cycle.pools))
        cycle_pools = set(cycle.pools)

        if c_key in unique_cycles:
            continue

        # Check if any pool in this cycle is already used
        if cycle_pools & used_pools:
            continue  # Conflict exists - skip this cycle

        # No conflict - select this cycle
        selected.append(cycle)
        used_pools.update(cycle_pools)
        unique_cycles[c_key] = cycle

    return selected    


class CycleDetector(BaseDetector):
    arb_kind = ArbKind.CYCLE

    """

    Detects arbitrage opportunities via graph cycle detection.

    Covers DEX-DEX, Triangular, and Multi-hop strategies.

    """

    name = "cycles"
    requires_touched_pools = True

    def __init__(self, w3, graph, logger, cache, chain_id):
        super().__init__(w3, graph, logger, cache, chain_id)
        chain = get_chain()
        
        # Use wrapped native + stablecoins as cycle sources
        self._source_tokens = [chain.wrapped_native] + chain.stablecoins
        
        # Cached gas cost per block — fetched once, reused for all cycles in that block
        self._cached_gas_usd: float = 0.0
        self._cache_gas_wei: int = 0
        self._cached_gas_block: int = -1
        
        # PERFORMANCE: Simple cycle cache to avoid recomputation
        # Key: pool_address, Value: (timestamp, cycles) - TTL 30 seconds
        self._cycle_cache: dict[str, tuple[float, list]] = {}
        self._cache_ttl = 30.0  # seconds

    

    def on_block(self, block_number: int, touched_pools: set[str] = None) -> list[Opportunity]:
        if self.graph.pool_count < 2:
            return []

        # Cache gas price once per block — avoid 1 RPC call per cycle
        if self._cached_gas_block != block_number:
            self._cached_gas_usd, self._cache_gas_wei = self._estimate_gas_usd()
            self._cached_gas_block = block_number
        gas_usd = self._cached_gas_usd
        gas_wei = self._cache_gas_wei

        

        # SMART TARGETED SCAN (Spec 3.3):

        # Instead of a cold DFS from arbitrary source tokens, we strictly execute 

        # the pathfinder originating FROM the pools that were actually touched (reserve-updated).

        # We use must_include_pool to guarantee the cycle incorporates the volatile edge,

        # completely eliminating redundant scans over static parts of the multigraph.

        # cycles = []

        if touched_pools: # Only scan if there was actual volatility
            seen_cycles = set()
            all_cycles = []
            for pool_addr in touched_pools:
                pool_cycles = self.graph.find_cycles(must_include_pool=pool_addr, min_profit_ratio=0.0005,)

                # cycles.extend(pool_cycles)

                if pool_cycles:
                    # Filter already-seen cycles
                    new_cycles = [c for c in pool_cycles if tuple(sorted(c.pools)) not in seen_cycles]
                    all_cycles.extend(new_cycles)
                    seen_cycles.update(tuple(sorted(c.pools)) for c in new_cycles)

            # Cleanup old cache entries (simple expiration)
            expired = [k for k, v in self._cycle_cache.items() if time.time() - v[0] > self._cache_ttl * 2]
            for k in expired:
                del self._cycle_cache[k]

            if not all_cycles:
                return []

            log.debug(f"Block {block_number}: {len(all_cycles)} unique cycles from {len(touched_pools)} pools")

            # for c in all_cycles:
            #     log.debug(f"Cycle: pools={c.pools}, dexes={c.dexes}, tokens={c.tokens}, fees={c.fees}, log_weight={c.log_weight_sum}")

            return self.simulate_and_build(all_cycles, block_number, gas_usd, gas_wei)

        return []

    def simulate_and_build(self, cycles: list[ArbitrageCycle], block_number, gas_usd, gas_wei=0, name='Cycle') -> list[Opportunity]:

        # CycleDetector owns genuine graph cycles only. Direct two-pool
        # fragmentation is exclusively CrossProtocolDetector's thesis.
        # cycles = [c for c in cycles if len(getattr(c, "pools", [])) >= 3]
        # Filter pool conflicts BEFORE simulation to avoid wasting resources
        cycles = _filter_pool_conflicts(cycles)
        if name == 'SharpeCycle':
            log.debug(f"[Sharpe] Block {block_number}: {len(cycles)} cycles after pool conflict filtering")
        cycles = [c.set_amount_in_wei() or c.rotate_to_best_start(self.graph) for c in cycles]
        
        # Batch yieldOut via Multicall3
        results = {}
        if ARB_EXEC_ADDRESS:
            results = multicall3_batch_yieldOut(self.w3, ARB_EXEC_ADDRESS, cycles, chunk_size=3)
        else:
            pure_v3_cycles = [c for c in cycles if all("V3" in x for x in c.dexes)]
            results = multicall3_batch_quote_v3(self.w3, pure_v3_cycles)
            

        opportunities = []

        for idx, raw_cycle in enumerate(cycles):

            # 1. Rotate to best starting token (WETH > Stables > Top TVL)

            # cycle = raw_cycle.rotate_to_best_start(self.graph)

            

            # 2. FIRST LAYER VALIDATION (PRODUCTION SPEC 4.2.1)

            # Test a minimal nominal amount (e.g., $10 equivalent) strictly off-chain in Python.

            # If a $10 input yields a > $10 output, it perfectly proves the path is mathematically viable.

            # We defer the heavy calculus (finding the peak TVL utilization) to the Optimality Solver plugin.

            root_token = raw_cycle.tokens[0]
            root_dec = token_decimals(root_token)

            # price = token_price(root_token) or NATIVE_PRICE_USD
            # target_usd = 10.0  # Nominal probe amount
            # sim_amount = int((target_usd / price) * (10 ** root_dec))

            # 3. Off-Chain Python Execution (Zero API Rate Limits)

            # The First Layer Validation relies on onchain simulation, fallback to python mathematical engine.
            if idx in results:
                ad_tuple, token_profit_raw = results[idx]
                via_contract = bool(ad_tuple)
            else:
                log.debug(f"Simulating cycle {raw_cycle.pools[0][:10]}")
                ad_tuple, token_profit_raw, via_contract = self.simulate_cycle(raw_cycle, raw_cycle.amount_in_wei)
                log.debug(f"Cycle {raw_cycle.pools[0][:10]}...: simulation fallback with profit {token_profit_raw}")

            if token_profit_raw is None or token_profit_raw <= 0:
                continue

            # Detector stage is quote/discovery only. Exact execution validation
            # is centralized in OpportunityEngine so every detector gets the
            # identical validation contract and no detector can bypass it.
            sim_amount = int(ad_tuple[0]) if ad_tuple else raw_cycle.amount_in_wei
            
            # Guard against zero simulation amount
            if sim_amount <= 0:
                log.debug(f"Skipping cycle {raw_cycle.pools[0][:10]}...: zero sim_amount (ad_tuple={ad_tuple})")
                continue

            # Path passed First Layer Validation!

            # Update contexts formatting for the Broadcaster using the Nominal numbers!
            token_profit_human = token_profit_raw / (10 ** root_dec)
            profit_value, profit_unit = profit_in_best_unit(root_token, token_profit_human)

            # For the USD filter, we map the nominal $10 profit to USD.
            profit_usd = profit_value if profit_unit == "USD" else 0
            gas_multiplier = 0.5 * len(raw_cycle.pools)
            gas_usd = gas_usd * gas_multiplier
            gas_wei = int(gas_wei * gas_multiplier) if root_token == get_chain().wrapped_native else 0
            net_profit_usd=(profit_usd - gas_usd) if (profit_usd and via_contract) else profit_usd

            if net_profit_usd is not None and net_profit_usd < MIN_PROFIT_USD:
                log.debug(f"Net profit {net_profit_usd} is less than {MIN_PROFIT_USD}")
                continue

            # Since the First Layer is just proving viability, we evaluate the % return
            # A $1 profit on a $10 input tells us the path has exactly a 10% return constraint.
            # We ignore fixed gas costs here because a $10 probe wouldn't cover $2 gas.

            profit_pct = (token_profit_raw / sim_amount) * 100
            if profit_pct < 0.005 and via_contract:
                continue

            # ── 6-tier classification using BOTH hop count AND cross-DEX flag ──
            #
            #  hops │  same-DEX          │  cross-DEX
            # ──────┼────────────────────┼─────────────────
            #   2   │  SAME_DEX_CYCLE    │  DEX_SPREAD      ← main arb signal
            #   3   │  TRIANGULAR        │  CROSS_DEX_TRI   ← strong signal
            #  4-5  │  MULTI_HOP         │  CROSS_DEX_MULTI  ← complex arb
            #

            is_cross = raw_cycle.is_cross_dex
            hops     = raw_cycle.hop_count

            if hops == 2:
                opp_type   = OppType.DEX_SPREAD if is_cross else OppType.SAME_DEX_CYCLE
                confidence = 0.9 if is_cross else 0.2
            elif hops == 3:
                opp_type   = OppType.CROSS_DEX_TRI if is_cross else OppType.TRIANGULAR
                confidence = 0.85 if is_cross else 0.7
            else:
                opp_type   = OppType.CROSS_DEX_MULTI if is_cross else OppType.MULTI_HOP
                confidence = 0.75 if is_cross else 0.55

            # Skip same-DEX 2-hop noise unless profit is extraordinary
            if opp_type == OppType.SAME_DEX_CYCLE and profit_pct < 0.1:
                continue

            competition = self._estimate_competition(raw_cycle.pools, raw_cycle.dexes)

            # Amount in USD based on the pure nominal input size
            root_price = token_price(root_token)
            amount_in_usd = (sim_amount / (10 ** root_dec)) * (root_price or NATIVE_PRICE_USD) if via_contract else 10
            fees_pip = [int(f * 100) for f in raw_cycle.fees]


            opp = Opportunity(
                opp_type=opp_type,
                chain_id=self.chain_id,
                block_number=block_number,
                timestamp=time.time(),
                tokens=raw_cycle.tokens,
                pools=raw_cycle.pools,
                dexes=raw_cycle.dexes,
                estimated_profit_ratio=raw_cycle.estimated_profit_ratio,
                estimated_profit_usd=profit_usd,
                amount_in_usd=amount_in_usd,
                gas_cost_usd=gas_usd,
                net_profit_usd=net_profit_usd,
                simulated_via_contract=False,
                simulation_profit_wei=0,
                competition_level=competition,
                confidence=confidence,
                metadata={
                    "hop_count":          raw_cycle.hop_count,
                    "is_cross_dex":       raw_cycle.is_cross_dex,
                    "dex_count":          len(set(raw_cycle.dexes)),
                    "fees_bps":           raw_cycle.fees,
                    "fees_pip":           fees_pip,            # ← NEW: pip format for executor
                    "versions":           raw_cycle.versions,
                    "cycle_type":         raw_cycle.cycle_type,
                    "log_weight":         raw_cycle.log_weight_sum,
                    "profit_pct":         profit_pct,
                    "profit_native":      round(token_profit_human, 8) if token_profit_raw > 0 else 0,
                    "profit_wei":         token_profit_raw,
                    "profit_unit":        profit_unit,
                    "gas_units":          DEFAULT_GAS_UNITS * gas_multiplier,
                    "min_profit":         gas_wei,
                    "root_token":         root_token,
                    "arb_data":           ad_tuple,            # ← NEW: sorted ArbData for executor
                    "optimal_amount_in":  int(ad_tuple[0]) if ad_tuple else None,  # ← NEW
                    "is_mempool":         False,               # ← set True by mempool pipeline
                    "detector": self.name,
                    "arb_kind": self.arb_kind.value,
                    "validation_status": "quote_validated",
                    "execution_simulated": False,
                    "execution_ready": False,
                    "requires_exact_validation": True,
                    "quote_profit_wei": token_profit_raw,
                    "execution_supported": __import__("routing").execution_capability([
                        getattr(self.graph.get_edge_by_pool_and_tokens(raw_cycle.pools[i], raw_cycle.tokens[i], raw_cycle.tokens[i + 1]), "protocol", "")
                        for i in range(len(raw_cycle.pools))
                    ])[0],
                },

            )

            opportunities.append(opp)

        

        log.info(f"Block {block_number}: {len(opportunities)} opportunities built from {len(cycles)} cycles")
        return sorted(opportunities, key=lambda x: (x.net_profit_usd, x.metadata.get("hop_count", 0)))



class SharpeDetector(CycleDetector):

    name = "cycles"

    def __init__(self, w3, graph, logger, cache, chain_id):
        super().__init__(w3, graph, logger, cache, chain_id)
        self.gecko = GeckoTerminal()

    def query_cycles(self, pool):
        pools = self.gecko.related_pools(self.chain_id, pool)
        all_pools = list(set([p['address'] for p in pools] + [pool]))
        cycles = []

        if all_pools and len(all_pools) > 1:
            try:
                # log.debug(f"Related Pools: {all_pools}")
                for combo in combinations(all_pools, 2):
                    resp = requests.get(
                        "https://www.sharpe.ai/api/v1/arbitrage/dex-scanner/preview",
                        headers={"Authorization": f"Bearer {os.getenv('SHARPE_API_KEY', '')}"},
                        params={
                            "poolUrl": f"https://www.geckoterminal.com/eth/pools/{combo[0]}",
                            "secondPoolUrl": f"https://www.geckoterminal.com/eth/pools/{combo[1]}",
                            "minProfitPct": 1,
                            "mode": "dex_dex",
                        },
                    )
                    data = resp.json()["data"]
                    pool = data['pool']
                    secondPool = data['secondPool']
                    token0 = pool['baseToken']['address'].lower()
                    token1 = pool['quoteToken']['address'].lower()
                    token_in = pool['targetToken']['address'].lower()
                    dex1 = "UniswapV2" if pool['dexName'].replace(" ", "") == "Uniswap" else pool['dexName'].replace(" ", "")
                    dex2 = "UniswapV2" if secondPool['dexName'].replace(" ", "") == "Uniswap" else secondPool['dexName'].replace(" ", "")
                    v1 = 2 if "V2" in dex1 else 3
                    v2 = 2 if "V2" in dex2 else 3
                    f1 = float(pool['raw'].get('attributes', {}).get('pool_fee_percentage', 0.3) or 0.3)
                    f2 = float(secondPool['raw'].get('attributes', {}).get('pool_fee_percentage', 0.3) or 0.3)
                    opp = [i for i in data["opportunities"] if i['legType'] == "dex_dex"][0]
                    # log.debug(json.dumps(pool, indent=2))
                    # log.debug(json.dumps(secondPool, indent=2))
                    cycles.append(ArbitrageCycle(
                        tokens=[token0, token1, token0],
                        pools=[pool['poolAddress'].lower(), secondPool['poolAddress'].lower()],
                        dexes=[dex1, dex2],
                        fees=[int(f1 * 10000), int(f2 * 10000)],
                        versions=[v1, v2],
                        log_weight_sum=opp['spreadPct'],
                        hop_count=2,
                        estimated_profit_ratio=float(opp['spreadPct'])
                    ))
                for c in cycles:   
                    log.debug(f"[Sharpe] {c}")
            except Exception as e:
                log.error(f"Error querying Sharpe API for cycles: {e}")
                return []
        return cycles
    
    def on_block(self, block_number: int, touched_pools: set[str] = None) -> list[Opportunity]:
        if self.graph.pool_count < 2:
            return []

        # Cache gas price once per block — avoid 1 RPC call per cycle
        if self._cached_gas_block != block_number:
            self._cached_gas_usd, self._cache_gas_wei = self._estimate_gas_usd()
            self._cached_gas_block = block_number
        gas_usd = self._cached_gas_usd
        gas_wei = self._cache_gas_wei

        if touched_pools: # Only scan if there was actual volatility
            seen_cycles = set()
            all_cycles = []
            for pool_addr in touched_pools:
                pool_cycles = self.query_cycles(pool_addr)

                if pool_cycles:
                    # Filter already-seen cycles
                    new_cycles = [c for c in pool_cycles if tuple(sorted(c.pools)) not in seen_cycles]
                    all_cycles.extend(new_cycles)
                    seen_cycles.update(tuple(sorted(c.pools)) for c in new_cycles)

            # Cleanup old cache entries (simple expiration)
            expired = [k for k, v in self._cycle_cache.items() if time.time() - v[0] > self._cache_ttl * 2]
            for k in expired:
                del self._cycle_cache[k]

            if not all_cycles:
                return []

            log.debug(f"[Sharpe] Block {block_number}: {len(all_cycles)} unique cycles from {len(touched_pools)} pools")

            # for c in all_cycles:
            #     log.debug(f"Cycle: pools={c.pools}, dexes={c.dexes}, tokens={c.tokens}, fees={c.fees}, log_weight={c.log_weight_sum}")

            return self.simulate_and_build(all_cycles, block_number, gas_usd, gas_wei, name='SharpeCycle')

        return []