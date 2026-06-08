"""
Enhanced Crawler Module
==================
Background pool discovery with multi-source support.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional
from random import choice

if TYPE_CHECKING:
    from web3 import Web3
    from graph import TokenGraph

from cache import PoolCache
from tvl import estimate_tvl_v2, estimate_tvl_v3, is_above_min_liquidity, init_prices
from config import (
    get_chain, MIN_LIQUIDITY_USD, NATIVE_PRICE_USD, CHAIN_ID,
    UNISWAP_V2_FACTORY_ABI, UNISWAP_V3_FACTORY_ABI,
    UNISWAP_V2_PAIR_ABI, UNISWAP_V3_POOL_ABI,
    CRAWLER_V3_CHUNK, CRAWLER_V2_BATCH, CRAWLER_TICK_SLEEP, STALE_REFRESH_INTERVAL,
    get_subgraph_providers,
)
from graph import PoolEdge
from utils.graph_client import GraphClient
from registry import register_new_pool

from log import get_logger
log = get_logger("crawler")


class PoolCrawler:
    """
    Background pool crawler with V2/V3 workers.
    """
    
    def __init__(self, w3: "Web3", graph: "TokenGraph", cache: PoolCache, price_oracle: Optional["PriceOracle"] = None):
        self._w3 = w3
        self._graph = graph
        self._cache = cache
        self._price_oracle = price_oracle
        self._chain = get_chain()
        
        init_prices(NATIVE_PRICE_USD, self._chain.wrapped_native, self._chain.stablecoins)
        
        # Stats
        self.v2_discovered = 0
        self.v2_filtered = 0
        self.v3_discovered = 0
        self.v3_filtered = 0
        self.stale_refreshed = 0
        self._v3_target: dict = {}
        self.running = False
        self._graph_client = GraphClient()
        self._subgraph_providers = get_subgraph_providers(CHAIN_ID)
    
    async def start(self) -> list[asyncio.Task]:
        """Launch all crawler workers."""
        self.running = True
        tasks = []
        
        # Primary: Bulk bootstrap from subgraph if available
        # await self._bootstrap_from_subgraph()
        
        # Secondary: Incremental discovery workers (only for new pools post-bootstrap)
        # V2 Workers: Index + Logs
        for dex in self._chain.dexes:
            if dex.name.lower() in self._subgraph_providers:
                tasks.append(asyncio.create_task(
                    self._bootstrap_from_subgraph(dex),
                    name=f"crawler_bootstrap_{dex.name}",
                ))
            else:
                if dex.version == 2:
                    # has_subgraph = dex.name.lower() in self._subgraph_providers
                    tasks.append(asyncio.create_task(
                        self._v2_worker(dex.factory, dex.name, dex.fee_bps),
                        name=f"crawler_v2_index_{dex.name}",
                    ))
                    # Skip log worker if subgraph available (we'll catch new pools via current block scanning)
                    # if not has_subgraph:
                    tasks.append(asyncio.create_task(
                        self._v2_log_worker(dex.factory, dex.name, dex.fee_bps, dex.deploy_block),
                        name=f"crawler_v2_logs_{dex.name}",
                    ))
        
        # V3 Worker
        # for dex in self._chain.dexes:
                elif dex.version == 3:
                    # has_subgraph = dex.name.lower() in self._subgraph_providers
                    tasks.append(asyncio.create_task(
                        self._v3_worker(dex.factory, dex.name, dex.deploy_block),
                        name=f"crawler_v3_{dex.name}",
                    ))
        
        tasks.append(asyncio.create_task(self._stale_refresher(), name="crawler_stale"))
        tasks.append(asyncio.create_task(self._garbage_collector(), name="crawler_gc"))
        
        # GOLD UPGRADE: Selective Discovery & Metadata Backfill
        # Instead of redundant top-50, we backfill metadata for tokens we already know
        # tasks.append(asyncio.create_task(self._metadata_backfill_loop(), name="crawler_metadata_backfill"))
        
        log.info(f"{len(tasks)} workers started")
        return tasks

    async def _bootstrap_from_subgraph(self, dex) -> None:
        """
        Primary pool discovery: Bulk fetch all pools from subgraph.
        Falls back to nothing - workers will handle RPC discovery for chains without subgraph.
        """
        log.info("Bootstrapping pools from subgraphs...")
        
        # for dex in self._chain.dexes:
        dex_key = dex.name.lower()
        if dex_key not in self._subgraph_providers:
            log.info(f"No subgraph for {dex.name}, skipping bootstrap (will use RPC workers)")
            return False
        
        try:
            log.info(f"Fetching all pools from subgraph for {dex.name}...")
            # Fetch all pools with empty list and intersect=False
            existing_pools = [addr for addr, p in self._cache._pools.items() if p["dex"] == dex.name]
            pools = await self._graph_client.get_pools_data(
                dex_name=dex_key,
                pool_addresses=existing_pools,  # empty = fetch all
                intersect=False     # fetch all, not just intersection
            )
            
            if not pools:
                log.warning(f"No pools returned from subgraph for {dex.name}")
                return False
            
            log.info(f"Got {len(pools)} pools from subgraph for {dex.name}, registering...")
            registered = 0
            skipped = 0
            
            for pool_data in pools:
                if not self.running:
                    return
                
                pool_addr = pool_data.get("id", "").lower()
                if not pool_addr:
                    skipped += 1
                    continue
                
                # Skip if already cached
                if self._cache.has_pool(pool_addr):
                    skipped += 1
                    continue
                
                try:
                    await register_new_pool(
                        self._w3, self._graph, self._cache, self._graph_client,
                        pool_addr, dex.name, dex.version, dex.fee_bps
                    )
                    registered += 1
                    if dex.version == 2:
                        self.v2_discovered += 1
                    else:
                        self.v3_discovered += 1
                except Exception as e:
                    log.debug(f"Failed to register {pool_addr} from subgraph: {e}")
                    skipped += 1
                
                # Brief pause to not overwhelm the system
                await asyncio.sleep(0.001)
            
            log.info(f"Bootstrap for {dex.name}: {registered} registered, {skipped} skipped")
            
        except Exception as e:
            log.error(f"Failed to bootstrap from subgraph for {dex.name}: {e}")
        
        log.info("Subgraph bootstrap complete")
        return True

    async def _metadata_backfill_loop(self) -> None:
        """Periodically backfill missing token metadata from subgraphs."""
        log.info("Starting metadata backfill loop...")
        while self.running:
            try:
                # 1. Identify tokens in graph missing metadata (decimals or symbol)
                unknown_tokens = [
                    t for t in self._graph.tokens 
                    if t not in self._graph.token_metadata or not self._graph.token_metadata[t].get("symbol")
                ]
                
                if unknown_tokens:
                    # Batch fetch in chunks of 50 to avoid huge queries
                    chunk_size = 50
                    for i in range(0, len(unknown_tokens), chunk_size):
                        chunk = unknown_tokens[i:i + chunk_size]
                        # Use uniswap_v3 as primary source for token info
                        # subgraph = ["uniswap_v3", "uniswap_v2", "uniswap_v3_ext", "uniswap_v2_alt", "uniswap_v3_alt"]
                        results = await self._graph_client.get_tokens_data("uniswap_v3_ext", chunk)
                        for t_data in results:
                            usd_price = float(t_data.get("lastPriceUSD") or 0)
                            derived_eth = float(t_data.get("derivedETH") or 0)
                            
                            self._graph.update_token_metadata(t_data["id"], {
                                "symbol": t_data.get("symbol"),
                                "decimals": int(t_data.get("decimals") or 18),
                                "derived_eth": derived_eth,
                                "usd_price": usd_price if usd_price > 0 else None
                            })
                            # Feed into Oracle
                            if self._price_oracle and usd_price > 0:
                                self._price_oracle.set_dex_price(t_data["id"], usd_price)
                        await asyncio.sleep(5) # rate limit prevention

                # 2. Wait 5 minutes before next check
                await asyncio.sleep(300)
            except Exception as e:
                log.debug(f"Metadata backfill error: {e}")
                await asyncio.sleep(60)
    
    async def stop(self) -> None:
        self.running = False
        await self._graph_client.close()
    
    @property
    def status(self) -> dict:
        """Get crawler status."""
        v3_pcts = []
        for dex in self._chain.dexes:
            if dex.version == 3 and dex.deploy_block > 0:
                target = self._v3_target.get(dex.factory.lower(), 0)
                if target > dex.deploy_block:
                    cursor = self._cache.get_v3_cursor(dex.factory, dex.deploy_block)
                    total = max(1, target - dex.deploy_block)
                    done = max(0, cursor - dex.deploy_block)
                    v3_pcts.append(min(100.0, done / total * 100))
        
        avg_v3_pct = sum(v3_pcts) / len(v3_pcts) if v3_pcts else 0.0
        
        return {
            "v2_discovered": self.v2_discovered,
            "v3_discovered": self.v3_discovered,
            "v2_filtered": self.v2_filtered,
            "v3_filtered": self.v3_filtered,
            "stale_refreshed": self.stale_refreshed,
            "v3_scan_pct": round(avg_v3_pct, 1),
            "total_cached": self._cache.pool_count(),
            "running": self.running,
        }
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # V2 WORKER (Ongoing - loads new pairs as they're created)
    # ═══════════════════════════════════════════════════════════════════════════════
    
    async def _v2_worker(self, factory_addr: str, dex_name: str, fee_bps: int) -> None:
        log.info(f"V2 worker started: {dex_name}")
        
        factory = self._w3.eth.contract(
            address=self._w3.to_checksum_address(factory_addr),
            abi=UNISWAP_V2_FACTORY_ABI,
        )
        
        while self.running:
            try:
                total = await asyncio.get_event_loop().run_in_executor(
                    None, factory.functions.allPairsLength().call
                )
                cursor = self._cache.get_v2_cursor(factory_addr)
                start = cursor + 1
                
                if start >= total:
                    await asyncio.sleep(30)
                    continue
                
                end = min(start + CRAWLER_V2_BATCH, total)
                
                for i in range(start, end):
                    if not self.running:
                        return
                    
                    try:
                        pair_addr = await asyncio.get_event_loop().run_in_executor(
                            None, factory.functions.allPairs(i).call
                        )
                        pair_addr = pair_addr.lower()
                        
                        if not self._cache.has_pool(pair_addr):
                            # Delegating to centralized register_new_pool for accurate TVL
                            await register_new_pool(
                                self._w3, self._graph, self._cache, self._graph_client,
                                pair_addr, dex_name, 2, fee_bps
                            )
                            self.v2_discovered += 1
                        else:
                            self.v2_filtered += 1
                        
                        self._cache.set_v2_cursor(factory_addr, i)
                    
                    except Exception as e:
                        if "429" in str(e):
                            log.warning(f"V2 index {i} rate limit hit (429). Backing off 10s.")
                            await asyncio.sleep(10)
                        else:
                            log.debug(f"V2 index {i} error: {e}")
                    
                    await asyncio.sleep(CRAWLER_TICK_SLEEP)
                
                self._cache.save_if_dirty()
            
            except asyncio.CancelledError:
                self._cache.save()
                return
            except Exception as e:
                log.warning(f"V2 worker error: {e}")
                if "429" in str(e):
                    await asyncio.sleep(15)
                else:
                    await asyncio.sleep(5)
    
    async def _v2_log_worker(self, factory_addr: str, dex_name: str, fee_bps: int, deploy_block: int) -> None:
        """V2 Discovery via PairCreated events."""
        log.info(f"V2 log worker started: {dex_name}")
        factory = self._w3.eth.contract(
            address=self._w3.to_checksum_address(factory_addr),
            abi=UNISWAP_V2_FACTORY_ABI,
        )
        
        cursor = self._cache.get_v3_cursor(f"{factory_addr}_v2l", deploy_block)
        
        while self.running:
            try:
                current = await asyncio.get_event_loop().run_in_executor(None, lambda: self._w3.eth.block_number)
                if cursor >= current:
                    await asyncio.sleep(30)
                    continue
                
                from_block = cursor
                to_block = min(cursor + CRAWLER_V3_CHUNK - 1, current)
                
                try:
                    logs = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: factory.events.PairCreated.get_logs(fromBlock=from_block, toBlock=to_block)
                    )
                    for l in logs:
                        pair_addr = l["args"]["pair"].lower()
                        if not self._cache.has_pool(pair_addr):
                            await register_new_pool(
                                self._w3, self._graph, self._cache, self._graph_client,
                                pair_addr, dex_name, 2, fee_bps
                            )
                            self.v2_discovered += 1
                except Exception as e:
                    log.debug(f"V2 log worker {dex_name} error: {e}")
                
                cursor = to_block + 1
                self._cache.set_v3_cursor(f"{factory_addr}_v2l", cursor)
                await asyncio.sleep(CRAWLER_TICK_SLEEP)
            except Exception as e:
                log.warning(f"V2 log worker major error {dex_name}: {e}")
                await asyncio.sleep(10)

    async def _v3_worker(self, factory_addr: str, dex_name: str, deploy_block: int) -> None:
        log.info(f"V3 worker started: {dex_name} from block {deploy_block}")
        factory = self._w3.eth.contract(
            address=self._w3.to_checksum_address(factory_addr),
            abi=UNISWAP_V3_FACTORY_ABI,
        )
        
        cursor = self._cache.get_v3_cursor(factory_addr, deploy_block)
        
        while self.running:
            try:
                current = await asyncio.get_event_loop().run_in_executor(None, lambda: self._w3.eth.block_number)
                if cursor >= current:
                    await asyncio.sleep(30)
                    continue
                
                from_block = cursor
                to_block = min(cursor + CRAWLER_V3_CHUNK - 1, current)
                
                try:
                    logs = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: factory.events.PoolCreated.get_logs(fromBlock=from_block, toBlock=to_block)
                    )
                    for l in logs:
                        pool_addr = l["args"]["pool"].lower()
                        if not self._cache.has_pool(pool_addr):
                            await register_new_pool(
                                self._w3, self._graph, self._cache, self._graph_client,
                                pool_addr, dex_name, 3, l["args"]["fee"] // 100,
                                token0=l["args"]["token0"].lower(),
                                token1=l["args"]["token1"].lower()
                            )
                            self.v3_discovered += 1
                except Exception as e:
                    log.debug(f"V3 worker {dex_name} logs error: {e}")
                
                cursor = to_block + 1
                self._cache.set_v3_cursor(factory_addr, cursor)
                await asyncio.sleep(CRAWLER_TICK_SLEEP)
            except Exception as e:
                log.warning(f"V3 worker major error {dex_name}: {e}")
                await asyncio.sleep(10)

    async def _garbage_collector(self) -> None:
        """
        Periodically garbage-collects abandoned pools from memory and disk cache
        to prevent RAM ballooning over continuous 24/7 uptime.
        """
        log.info("Garbage collector task started")
        while self.running:
            try:
                # Wait 1 hour between GC sweeps
                for _ in range(60): # Sleep 60 * 60s in chunks to allow fast shutdown
                    if not self.running: return
                    await asyncio.sleep(60)
                
                if not self.running: return

                # Prune pools inactive for 14 days (1,209,600 seconds)
                pruned_addrs = self._cache.prune_stale_pools(max_age_seconds=1209600)
                
                # Prune from memory-graph
                for addr in pruned_addrs:
                    self._graph.remove_pool(addr)
                    
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning(f"Garbage collector runtime error: {e}")
                await asyncio.sleep(60)

    async def _stale_refresher(self) -> None:
        """
        Refresh stale pool reserves with Tiered Priority:
        1. High TVL pools (>100k) refresh often
        2. Pending pools (Waiting Room) refresh to see if price is now available
        3. Low TVL pools refresh less often
        """
        log.info("Tiered stale refresher started")
        
        while self.running:
            try:
                # 1. Prioritize Pending Pools (Waiting Room)
                pending = self._cache.get_pending_pools()[:15]
                if pending:
                    log.debug(f"Waiting Room: checking {len(pending)} pending pools using batched Subgraph rescue...")
                    await self._batch_subgraph_rescue(pending)
                    for addr in pending:
                        await self._refresh_and_update(addr)
                    await asyncio.sleep(CRAWLER_TICK_SLEEP)

                # 2. High TVL Pools (> $10k)
                high_tvl = self._cache.stale_pools(min_tvl=10000)[:20]
                if high_tvl:
                    for addr in high_tvl:
                        await self._refresh_and_update(addr)
                        await asyncio.sleep(CRAWLER_TICK_SLEEP)
                
                # 3. Rest of the stale pools
                others = self._cache.stale_pools()[:20]
                if not others and not pending and not high_tvl:
                    await asyncio.sleep(STALE_REFRESH_INTERVAL)
                    continue

                for addr in others:
                    if not self.running: return
                    await self._refresh_and_update(addr)
                    await asyncio.sleep(CRAWLER_TICK_SLEEP)
                
                self._cache.save_if_dirty()
            
            except Exception as e:
                log.warning(f"Stale refresher error: {e}")
                await asyncio.sleep(10)

    async def _refresh_and_update(self, addr: str):
        """Helper to refresh a single pool's reserves and update graph."""
        entry = self._cache.get_pool(addr)
        if not entry: return
        
        ver = entry.get("version", 2)
        try:
            if ver == 2:
                r0, r1, sqrt, liq = await asyncio.get_event_loop().run_in_executor(
                    None, self._refresh_v2_reserves, addr
                )
            else:
                r0, r1, sqrt, liq = await asyncio.get_event_loop().run_in_executor(
                    None, self._refresh_v3_reserves, addr
                )
            
            if r0 is not None or sqrt is not None:
                # If it was pending, check if we can now determine TVL
                was_pending = entry.get("is_pending", False)
                self._cache.update_reserves(addr, r0 or 0, r1 or 0, sqrt or 0, liq or 0)
                
                # Force TVL re-estimate
                if ver == 2:
                    tvl = estimate_tvl_v2(entry["token0"], entry["token1"], r0 or 0, r1 or 0)
                else:
                    tvl = estimate_tvl_v3(entry["token0"], entry["token1"], sqrt or 0, liq or 0)
                
                if tvl > 0:
                    entry["tvl_usd"] = tvl
                    entry["is_pending"] = False # No longer pending
                    self._cache.set_pool(addr, entry)
                    if was_pending:
                        log.info(f"Pool {addr[:10]} graduated from Waiting Room (TVL: ${tvl:,.0f})")

                self._update_graph_reserves(addr, entry, r0 or 0, r1 or 0, sqrt or 0, liq or 0)
                self.stale_refreshed += 1
        except Exception as e:
            log.debug(f"Refresh error {addr[:10]}: {e}")

    async def _batch_subgraph_rescue(self, addresses: list[str]) -> None:
        """Query richer subgraphs to see if pending pools are actually high-TVL in batches."""
        try:
            grouped = {}
            for addr in addresses:
                entry = self._cache.get_pool(addr)
                if not entry: continue
                ver = entry.get("version", 2)
                dex = entry.get("dex", "")
                
                if "sushi" in dex.lower():
                    subgraph = "sushiswap_v2"
                else:
                    subgraph = f"uniswap_v{ver}"
                grouped.setdefault(subgraph, []).append(addr.lower())

            for subgraph, addrs in grouped.items():
                results = await self._graph_client.get_pools_data(subgraph, addrs)
                
                # FALLBACK to EXT only for V3 TVL rescue if standard is empty
                if not results and "v3" in subgraph:
                     results = await self._graph_client.get_pools_data("uniswap_v3_ext", addrs)

                if not results:
                    continue

                for data in results:
                    addr = data["id"].lower()
                    entry = self._cache.get_pool(addr)
                    if not entry: continue
                    
                    tvl_usd = float(data.get("totalValueLockedUSD") or data.get("reserveUSD") or 0)
                    t0_price = float(data.get("token0Price") or 0)
                    t1_price = float(data.get("token1Price") or 0)

                    if tvl_usd >= MIN_LIQUIDITY_USD:
                        log.research(f"BATCH RESCUE | Pool {addr[:10]} confirmed via Subgraph TVL: ${tvl_usd:,.0f} | Prices: {t0_price:.4f}/{t1_price:.4f}")
                        entry["tvl_usd"] = tvl_usd
                        entry["token0_price"] = t0_price
                        entry["token1_price"] = t1_price
                        entry["is_pending"] = False 
                        self._cache.set_pool(addr, entry)
                        
                        # Metadata backfill
                        tokens = data.get("inputTokens", [])
                        if not tokens and "token0" in data:
                            tokens = [data["token0"], data["token1"]]
                            
                        for t in tokens:
                            u_price = float(t.get("lastPriceUSD") or 0)
                            derived_eth = float(t.get("derivedETH") or 0)
                            if not u_price and derived_eth and NATIVE_PRICE_USD:
                                u_price = derived_eth * NATIVE_PRICE_USD
                                
                            self._graph.update_token_metadata(t["id"], {
                                "symbol": t.get("symbol"),
                                "decimals": int(t.get("decimals") or 18),
                                "usd_price": u_price if u_price > 0 else None,
                                "derived_eth": derived_eth or (u_price / NATIVE_PRICE_USD if NATIVE_PRICE_USD > 0 else 0)
                            })
                            if self._price_oracle and u_price > 0:
                                self._price_oracle.set_dex_price(t["id"], u_price)
        except Exception as e:
            log.debug(f"Batch subgraph rescue failed: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # FETCH HELPERS
    # ═══════════════════════════════════════════════════════════════════════════════
    
    
    def _refresh_v2_reserves(self, addr: str):
        try:
            pair = self._w3.eth.contract(
                address=self._w3.to_checksum_address(addr),
                abi=UNISWAP_V2_PAIR_ABI,
            )
            r0, r1, _ = pair.functions.getReserves().call()
            return r0, r1, 0, 0
        except:
            return None, None, None, None
    
    def _refresh_v3_reserves(self, addr: str):
        try:
            pool = self._w3.eth.contract(
                address=self._w3.to_checksum_address(addr),
                abi=UNISWAP_V3_POOL_ABI,
            )
            slot0 = pool.functions.slot0().call()
            liq = pool.functions.liquidity().call()
            return 0, 0, slot0[0], liq
        except:
            return None, None, None, None
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # POOL REGISTRATION
    # ═══════════════════════════════════════════════════════════════════════════════
    
    
    def _update_graph_reserves(self, addr: str, entry: dict, r0: int, r1: int, sqrt: int, liq: int) -> None:
        """Update reserves in graph after stale refresh."""
        t0 = entry["token0"]
        t1 = entry["token1"]
        dex = entry["dex"]
        ver = entry["version"]
        fee = entry["fee_bps"]
        
        if ver == 2:
            tvl = estimate_tvl_v2(t0, t1, r0, r1)
        else:
            tvl = estimate_tvl_v3(t0, t1, sqrt, liq)
        
        if not is_above_min_liquidity(tvl, MIN_LIQUIDITY_USD):
            self._graph.remove_pool(addr)
            self._cache.mark_below_threshold(addr)
            return
        
        if ver == 2:
            ea = PoolEdge(addr, dex, t0, t1, r0, r1, fee, 2, tvl_usd=tvl)
            eb = PoolEdge(addr, dex, t1, t0, r1, r0, fee, 2, tvl_usd=tvl)
        else:
            inv = int(2**192 / sqrt**2 * 2**96) if sqrt > 0 else 0
            ea = PoolEdge(addr, dex, t0, t1, 0, 0, fee, 3, sqrt_price_x96=sqrt, liquidity=liq, tvl_usd=tvl)
            eb = PoolEdge(addr, dex, t1, t0, 0, 0, fee, 3, sqrt_price_x96=inv, liquidity=liq, tvl_usd=tvl)
        
        self._graph.add_or_update_pool(ea, eb)


