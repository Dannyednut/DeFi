"""
Enhanced Cache Module
==================
Persistent pool/token cache with improved structure and metadata.
"""
from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path
import asyncio
from typing import Optional, TYPE_CHECKING
from utils.graph_client import GraphClient
from decimal import Decimal, getcontext
from config import get_chain

if TYPE_CHECKING:
    from graph import TokenGraph

SCHEMA_VERSION = 3
STALE_SECONDS = 3600

from log import get_logger
log = get_logger("cache")

getcontext().prec = 40


class PoolCache:
    """
    Persistent cache for pool data across sessions.
    Supports atomic writes and incremental updates.
    """
    
    def __init__(self, chain_id: int, cache_dir: str = "./cache"):
        self._chain_id = chain_id
        self._chain = get_chain()
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"pools_{chain_id}.json"
        self._tmp_path = self._dir / f"pools_{chain_id}.json.tmp"
        self._lock = threading.Lock()
        
        # Data structures
        self._pools: dict[str, dict] = {}
        self._v2_cursors: dict[str, int] = {}
        self._v3_cursors: dict[str, int] = {}
        self._last_synced_block: int = 0
        
        # Tracking
        self._dirty_count = 0
        self.SAVE_INTERVAL = 50
        
        # Metadata
        self._discovered_at = time.time()
        self._last_saved_at = 0
    
    # ══════════════════════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ══════════════════════════════════════════════════════════════════════════════
    
    def load(self) -> bool:
        """Load cache from disk."""
        if not self._path.exists():
            log.info(f"No cache at {self._path} — starting fresh")
            return False
        
        try:
            with open(self._path) as f:
                data = json.load(f)
            
            if data.get("chain_id") != self._chain_id:
                log.warning("Chain mismatch — discarding cache")
                self._path.unlink(missing_ok=True)
                return False
            
            if data.get("schema_version", 1) < SCHEMA_VERSION:
                log.warning("Schema outdated — rebuilding cache")
                self._path.unlink(missing_ok=True)
                return False
            
            async def _load_sync():
                gc = GraphClient()
                try:
                    return await self.update_pools(data.get("pools", {}), gc)
                finally:
                    await gc.close()

            # Handle both sync and async contexts safely
            try:
                # Check if we're already in an async context
                loop = asyncio.get_running_loop()
                # We're in async context - use nest_asyncio or run in executor
                import nest_asyncio
                nest_asyncio.apply()
                self._pools = asyncio.run(_load_sync())
            except RuntimeError:
                # No running loop - safe to use asyncio.run()
                self._pools = asyncio.run(_load_sync())
            self._v2_cursors = data.get("v2_cursors", {})
            self._v3_cursors = data.get("v3_cursors", {})
            self._last_synced_block = data.get("last_synced_block", 0)
            
            stale = sum(
                1 for p in self._pools.values()
                if time.time() - p.get("last_updated", 0) > STALE_SECONDS
            )
            
            log.info(
                f"  [cache] Loaded {len(self._pools):,} pools "
                f"({stale:,} stale)"
            )
            return True
        
        except Exception as e:
            log.warning(f"Load error ({e}) — starting fresh")
            return False
    
    def save(self) -> None:
        """Atomically save cache to disk with retry for Windows."""
        import shutil, uuid
        with self._lock:
            try:
                payload = {
                    "chain_id": self._chain_id,
                    "schema_version": SCHEMA_VERSION,
                    "saved_at": time.time(),
                    "v2_cursors": self._v2_cursors.copy(),
                    "v3_cursors": self._v3_cursors.copy(),
                    "last_synced_block": self._last_synced_block,
                    "pools": self._pools.copy(),
                }
                
                # Use a unique temp file per call to prevent concurrent write collisions on Windows
                unique_tmp = self._path.parent / f"pools_{self._chain_id}_{uuid.uuid4().hex[:8]}.tmp"
                
                with open(unique_tmp, "w") as f:
                    json.dump(payload, f, separators=(",", ":"))
                
                # Retry loop for Windows file locking issues
                max_retries = 5
                for i in range(max_retries):
                    try:
                        shutil.move(str(unique_tmp), str(self._path))
                        break
                    except Exception:
                        if i == max_retries - 1:
                            # Give up silently - cache will retry next interval
                            if unique_tmp.exists():
                                unique_tmp.unlink(missing_ok=True)
                        time.sleep(0.15 * (i + 1))

                self._dirty_count = 0
                self._last_saved_at = time.time()
            
            except Exception as e:
                log.warning(f"Save failed: {e}")
    
    def save_if_dirty(self) -> None:
        """Save only if enough changes accumulated."""
        if self._dirty_count >= self.SAVE_INTERVAL:
            self.save()
    
    # ══════════════════════════════════════════════════════════════════════════════
    # POOL ACCESS
    # ══════════════════════════════════════════════════════════════════════════════
    
    def set_pool(self, addr: str, data: dict) -> None:
        """Insert or update a pool."""
        addr = addr.lower()
        
        # Merge with existing data
        existing = self._pools.get(addr, {})
        existing.update(data)
        existing["last_updated"] = time.time()
        
        if "discovered_at" not in existing:
            existing["discovered_at"] = time.time()
        
        self._pools[addr] = existing
        self._dirty_count += 1
        self.save_if_dirty()
    
    def has_pool(self, addr: str) -> bool:
        return addr.lower() in self._pools
    
    def get_pool(self, addr: str) -> Optional[dict]:
        return self._pools.get(addr.lower())
    
    def all_pools(self) -> dict[str, dict]:
        return self._pools
    
    def pool_count(self) -> int:
        return len(self._pools)
    
    def update_reserves(
        self,
        addr: str,
        r0: int = 0,
        r1: int = 0,
        sqrt_price_x96: int = 0,
        liquidity: int = 0,
    ) -> None:
        """Update reserve data for a pool."""
        entry = self._pools.get(addr.lower())
        if not entry:
            return
        
        entry["r0"] = r0
        entry["r1"] = r1
        entry["sqrt_price_x96"] = sqrt_price_x96
        entry["liquidity"] = liquidity
        entry["last_updated"] = time.time()
        
        self._dirty_count += 1
        self.save_if_dirty()
    
    def mark_below_threshold(self, addr: str) -> None:
        """Mark pool as below liquidity threshold."""
        entry = self._pools.get(addr.lower())
        if entry:
            entry["below_threshold"] = True
            entry["r0"] = 0
            entry["r1"] = 0
            entry["sqrt_price_x96"] = 0
            entry["liquidity"] = 0
            entry["last_updated"] = time.time()
            self._dirty_count += 1
            self.save_if_dirty()
    
    def stale_pools(self, min_tvl: float = 0.0) -> list[str]:
        """
        Get list of pools with stale reserves.
        Sorts by TVL descending to prioritize high-value pools.
        """
        cutoff = time.time() - STALE_SECONDS
        
        pools = [
            (a, p.get("tvl_usd", 0)) for a, p in self._pools.items()
            if (p.get("last_updated", 0) < cutoff or p.get("is_pending", False))
            and not p.get("below_threshold", False)
            # If it's pending, we don't care if TVL is 0; it stays in the refresh queue.
            and (p.get("is_pending", False) or p.get("tvl_usd", 0) >= min_tvl)
        ]
        
        # Sort by TVL (highest first)
        pools.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in pools]

    def prune_stale_pools(self, max_age_seconds: int = 1209600) -> list[str]:
        """
        Garbage collect abandoned pools that haven't been updated in 'max_age_seconds'
        to prevent RAM ballooning over weeks of uptime.
        """
        now = time.time()
        to_remove = []
        for addr, p in self._pools.items():
            if now - p.get("last_updated", 0) > max_age_seconds:
                to_remove.append(addr)
                
        for addr in to_remove:
            del self._pools[addr]
            
        if to_remove:
            self._dirty_count += len(to_remove)
            self.save_if_dirty()
            log.info(f"Garbage Collector: Pruned {len(to_remove)} abandoned pools from cache.")
            
        return to_remove

    def get_pending_pools(self) -> list[str]:
        """Get pools that are in the 'Waiting Room' (unknown TVL)."""
        return [
            a for a, p in self._pools.items()
            if p.get("is_pending", False) and not p.get("below_threshold", False)
        ]
    
    # ══════════════════════════════════════════════════════════════════════════════
    # CURSORS
    # ══════════════════════════════════════════════════════════════════════════════
    
    def get_v2_cursor(self, factory: str) -> int:
        return self._v2_cursors.get(factory.lower(), -1)
    
    def set_v2_cursor(self, factory: str, index: int) -> None:
        self._v2_cursors[factory.lower()] = index
        self._dirty_count += 1
    
    def get_v3_cursor(self, factory: str, deploy_block: int) -> int:
        return self._v3_cursors.get(factory.lower(), deploy_block)
    
    def set_v3_cursor(self, factory: str, block: int) -> None:
        self._v3_cursors[factory.lower()] = block
        self._dirty_count += 1

    def get_last_synced_block(self) -> int:
        return self._last_synced_block

    def set_last_synced_block(self, block: int) -> None:
        self._last_synced_block = block
        self._dirty_count += 1
    
    # ══════════════════════════════════════════════════════════════════════════════
    # GRAPH WARMUP
    # ══════════════════════════════════════════════════════════════════════════════
    
    def warm_graph(self, graph: "TokenGraph") -> int:
        """Load all cached pools into the token graph."""
        from graph import PoolEdge
        from tvl import estimate_tvl_v2, estimate_tvl_v3, is_above_min_liquidity
        from config import MIN_LIQUIDITY_USD
        
        loaded = 0
        skipped = 0
        
        
        for addr, p in self._pools.items():
            if p.get("below_threshold", False):
                skipped += 1
                continue
            
            try:
                t0 = p["token0"]
                t1 = p["token1"]
                
                # GOLD UPGRADE: Inject metadata directly during warmup if available!
                if "token0_meta" in p and isinstance(p["token0_meta"], dict):
                    meta = p["token0_meta"]
                    graph.update_token_metadata(t0, {
                        "symbol": meta.get("symbol"),
                        "decimals": int(meta.get("decimals") or 18),
                        "derived_eth": float(meta.get("derivedETH") or 0)
                    })
                if "token1_meta" in p and isinstance(p["token1_meta"], dict):
                    meta = p["token1_meta"]
                    graph.update_token_metadata(t1, {
                        "symbol": meta.get("symbol"),
                        "decimals": int(meta.get("decimals") or 18),
                        "derived_eth": float(meta.get("derivedETH") or 0)
                    })
                    
                dex = p["dex"]
                ver = p["version"]
                fee = p.get("fee_bps", 30)
                r0 = p.get("r0", 0)
                r1 = p.get("r1", 0)
                sqrt = p.get("sqrt_price_x96", 0)
                liq = p.get("liquidity", 0)
                
                if ver == 2:
                    if r0 == 0 or r1 == 0:
                        skipped += 1
                        continue
                    
                    if p.get("is_pending", False):
                        # Protecting the Waiting Room: don't check TVL yet
                        skipped += 1
                        continue

                    tvl = estimate_tvl_v2(t0, t1, r0, r1)
                    if not is_above_min_liquidity(tvl, MIN_LIQUIDITY_USD):
                        self._pools[addr]["below_threshold"] = True
                        skipped += 1
                        continue
                    
                    ea = PoolEdge(addr, dex, t0, t1, r0, r1, fee, 2)
                    eb = PoolEdge(addr, dex, t1, t0, r1, r0, fee, 2)
                
                else:
                    if sqrt == 0:
                        skipped += 1
                        continue
                    
                    if p.get("is_pending", False):
                        skipped += 1
                        continue

                    tvl = estimate_tvl_v3(t0, t1, sqrt, liq)
                    if not is_above_min_liquidity(tvl, MIN_LIQUIDITY_USD):
                        self._pools[addr]["below_threshold"] = True
                        skipped += 1
                        continue
                    
                    inv = int(2**192 / sqrt**2 * 2**96) if sqrt > 0 else 0
                    ea = PoolEdge(addr, dex, t0, t1, 0, 0, fee, 3,
                                 sqrt_price_x96=sqrt, liquidity=liq, tvl_usd=tvl)
                    eb = PoolEdge(addr, dex, t1, t0, 0, 0, fee, 3,
                                 sqrt_price_x96=inv, liquidity=liq, tvl_usd=tvl)
                
                graph.add_or_update_pool(ea, eb)
                loaded += 1
            
            except Exception:
                skipped += 1
                continue
        
        if skipped:
            log.info(f"warm_graph: {loaded} loaded, {skipped} skipped")
        return loaded

    # ══════════════════════════════════════════════════════════════════════════════
    # POOL DATA UPDATE
    # ══════════════════════════════════════════════════════════════════════════════

    async def update_pools(self, pools: dict, graph_client) -> dict:
        """
        Takes a pools dict, fetches latest metadata from subgraph,
        updates each pool in place, and returns the updated dict.
        Does NOT alter reserves (Spec 2.6).
        """

        # --- Group pool addresses by dex version ---
        dexes = {dex.name.lower(): {} for dex in self._chain.dexes}
        # uniswap_v3_pools = {}
        # uniswap_v2_pools = {}
        # sushiswap_v2_pools = {}

        for address, pool in pools.items():
            if pool.get("below_threshold", False) or pool.get('is_pending', False):
                continue
            if (pool['r0']==0 or pool['r1']==0) and pool['version'] == 2:
                continue
            if pool['dex'].lower() in dexes:
                dexes[pool['dex'].lower()][address] = pool
            

        # for address, pool in pools.items():
        #     if pool.get("below_threshold", False) or pool.get('is_pending', False):
        #         continue
        #     if (pool['r0']==0 or pool['r1']==0) and pool['version'] == 2:
        #         continue
        #     if pool["version"] == 3 and pool['dex'] == 'UniswapV3':
        #         uniswap_v3_pools[address] = pool
        #     elif pool['version'] == 2 and pool['dex'] == 'UniswapV2':
        #         uniswap_v2_pools[address] = pool
        #     else:
        #         sushiswap_v2_pools[address] = pool

        # --- Fetch & update V3 pools ---
        for dex in dexes:
        # if uniswap_v3_pools:
            data = await graph_client.get_pools_data(
                dex_name=dex,
                pool_addresses=list(dexes[dex].keys())
            )
            for p in data:
                address = p["id"].lower()
                if address not in pools:
                    continue

                # Spec 2.6: Never update raw reserves/ticks/liquidity from TheGraph
                # raw_tvl = p.get("totalValueLockedUSD")
                # tvl = float(raw_tvl) if raw_tvl is not None else pools[address].get("tvl_usd", 0.0)
                
                # raw_t0p = p.get("token0Price")
                # token0_price = float(raw_t0p) if raw_t0p is not None else pools[address].get("token0Price", 0.0)
                
                # raw_t1p = p.get("token1Price")
                # token1_price = float(raw_t1p) if raw_t1p is not None else pools[address].get("token1Price", 0.0)
                
                # raw_vol = p.get("volumeUSD")
                # volume = float(raw_vol) if raw_vol is not None else pools[address].get("volumeUSD", 0.0)

                pools[address].update({
                    "tvl_usd":      float(p.get("totalValueLockedUSD" if "v3" in dex else "reserveUSD", pools[address].get("tvl_usd", 0))),
                    "token0Price":  float(p.get("token0Price", pools[address].get("token0Price", 0))),
                    "token1Price":  float(p.get("token1Price", pools[address].get("token1Price", 0))),
                    "volumeUSD":    float(p.get("volumeUSD", pools[address].get("volumeUSD", 0))),
                    "last_updated": self._now(),
                })
                
                # Save metadata so we don't have to backfill it later
                if "token0" in p and isinstance(p["token0"], dict):
                    pools[address]["token0_meta"] = p["token0"]
                if "token1" in p and isinstance(p["token1"], dict):
                    pools[address]["token1_meta"] = p["token1"]

        # --- Fetch & update V2 pools ---
        # if uniswap_v2_pools:
        #     v2_data = await graph_client.get_pools_data(
        #         dex_name="uniswap_v2",
        #         pool_addresses=list(uniswap_v2_pools.keys())
        #     )
        #     for p in v2_data:
        #         address = p["id"].lower()
        #         if address not in pools:
        #             continue

        #         pools[address].update({
        #             "tvl_usd":      float(p.get("reserveUSD", pools[address].get("tvl_usd", 0))),
        #             "token0Price":  float(p.get("token0Price", pools[address].get("token0Price", 0))),
        #             "token1Price":  float(p.get("token1Price", pools[address].get("token1Price", 0))),
        #             "volumeUSD":    float(p.get("volumeUSD", pools[address].get("volumeUSD", 0))),
        #             "last_updated": self._now(),
        #         })
                
        #         # Save metadata
        #         if "token0" in p and isinstance(p["token0"], dict):
        #             pools[address]["token0_meta"] = p["token0"]
        #         if "token1" in p and isinstance(p["token1"], dict):
        #             pools[address]["token1_meta"] = p["token1"]

        # if sushiswap_v2_pools:
        #     v2_data = await graph_client.get_pools_data(
        #         dex_name="sushiswap_v2",
        #         pool_addresses=list(sushiswap_v2_pools.keys())
        #     )
        #     for p in v2_data:
        #         address = p["id"].lower()
        #         if address not in pools:
        #             continue

        #         pools[address].update({
        #             "tvl_usd":      float(p.get("reserveUSD", pools[address].get("tvl_usd", 0))),
        #             "token0Price":  float(p.get("token0Price", pools[address].get("token0Price", 0))),
        #             "token1Price":  float(p.get("token1Price", pools[address].get("token1Price", 0))),
        #             "volumeUSD":    float(p.get("volumeUSD", pools[address].get("volumeUSD", 0))),
        #             "last_updated": self._now(),
        #         })
                
        #         # Save metadata
        #         if "token0" in p and isinstance(p["token0"], dict):
        #             pools[address]["token0_meta"] = p["token0"]
        #         if "token1" in p and isinstance(p["token1"], dict):
        #             pools[address]["token1_meta"] = p["token1"]

        return pools

    # --- Helpers ---
    def _now(self) -> float:
        import time
        return time.time()
    
    # ══════════════════════════════════════════════════════════════════════════════
    # STATS
    # ══════════════════════════════════════════════════════════════════════════════
    
    def get_stats(self) -> dict:
        """Get cache statistics."""
        now = time.time()
        
        version_counts = {2: 0, 3: 0}
        dex_counts = {}
        stale_count = 0
        below_threshold = 0
        
        for p in self._pools.values():
            version_counts[p.get("version", 2)] = version_counts.get(p.get("version", 2), 0) + 1
            dex_counts[p.get("dex", "unknown")] = dex_counts.get(p.get("dex", "unknown"), 0) + 1
            
            if p.get("last_updated", 0) < now - STALE_SECONDS:
                stale_count += 1
            if p.get("below_threshold", False):
                below_threshold += 1
        
        return {
            "total_pools": len(self._pools),
            "v2_pools": version_counts.get(2, 0),
            "v3_pools": version_counts.get(3, 0),
            "dex_distribution": dex_counts,
            "stale_pools": stale_count,
            "below_threshold": below_threshold,
            "v2_cursors": len(self._v2_cursors),
            "v3_cursors": len(self._v3_cursors),
            "last_saved": self._last_saved_at,
            "dirty_count": self._dirty_count,
        }

