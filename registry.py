"""
Enhanced Registry Module
==================
Fast startup bootstrap from cache + seed pools.
"""
from __future__ import annotations

import time
import asyncio
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from web3 import Web3

from config import (
    get_chain,
    UNISWAP_V2_FACTORY_ABI,
    UNISWAP_V2_PAIR_ABI,
    UNISWAP_V3_FACTORY_ABI,
    UNISWAP_V3_POOL_ABI,
    MIN_LIQUIDITY_USD,
    NATIVE_PRICE_USD,
    get_factory_by_name,
    CHAIN_ID,
)
from graph import PoolEdge, TokenGraph
from cache import PoolCache
from tvl import estimate_tvl_v2, estimate_tvl_v3, is_above_min_liquidity, init_prices

from log import get_logger
log = get_logger("registry")

# Subgraph usage in registry
from utils.graph_client import GraphClient

# Rate limiting for registration to avoid RPC/Subgraph overload
_REGISTRATION_SEMAPHORE: Optional[asyncio.Semaphore] = None
_SEMPAHORE_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _get_registration_semaphore() -> asyncio.Semaphore:
    """
    Get or create the registration semaphore, ensuring it's bound to the current event loop.
    This prevents 'Semaphore bound to different event loop' errors when background thread restarts.
    """
    global _REGISTRATION_SEMAPHORE, _SEMPAHORE_LOOP
    
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop running - create new semaphore (will be bound when first used in async context)
        if _REGISTRATION_SEMAPHORE is None:
            _REGISTRATION_SEMAPHORE = asyncio.Semaphore(5)
            _SEMPAHORE_LOOP = None
        return _REGISTRATION_SEMAPHORE
    
    # Check if we need to recreate semaphore for new loop
    if _REGISTRATION_SEMAPHORE is None or (
        _SEMPAHORE_LOOP is not None and _SEMPAHORE_LOOP != current_loop
    ):
        _REGISTRATION_SEMAPHORE = asyncio.Semaphore(5)
        _SEMPAHORE_LOOP = current_loop
    
    return _REGISTRATION_SEMAPHORE


def bootstrap_graph(
    w3: "Web3",
    graph: TokenGraph,
    cache: PoolCache,
    max_v2_pools: int = 50000, # Increased for production scale
) -> None:
    """Bootstrap the token graph from cache and seed pools."""
    chain = get_chain()
    
    init_prices(NATIVE_PRICE_USD, chain.wrapped_native, chain.stablecoins)
    
    # 1. Warm from cache
    n = cache.warm_graph(graph)
    log.info(f"Cache warmup: {n:,} pools -> graph")
    
    # 2. Seed pools
    seeds_loaded = 0
    for seed in chain.seed_pools:
        addr = seed["addr"].lower()
        try:
            if seed["version"] == 2:
                ea, eb = _load_v2_pair(w3, addr, seed["dex"], seed["fee_bps"])
            else:
                ea, eb = _load_v3_pool(
                    w3, addr, seed["token0"], seed["token1"],
                    seed["fee_bps"], seed["dex"]
                )
            
            if ea and eb:
                graph.add_or_update_pool(ea, eb)
                cache.set_pool(addr, {
                    "token0": ea.token_in, "token1": ea.token_out,
                    "dex": ea.dex_name, "version": seed["version"],
                    "fee_bps": ea.fee_bps, "r0": ea.reserve_in,
                    "r1": ea.reserve_out, "sqrt_price_x96": ea.sqrt_price_x96,
                    "liquidity": ea.liquidity, "last_updated": time.time(),
                    "tvl_usd": ea.tvl_usd,
                })
                seeds_loaded += 1
        except Exception as e:
            log.warning(f"Seed {addr[:12]} failed: {e}")
    
    log.info(f"Seed pools: {seeds_loaded} loaded")
    
    # cache.save()
    graph.find_cycles(max_hops=3)  # Fast precompute for summary stats
    log.info(f"Bootstrap complete — {graph.summary()}")


def _load_v2_pair(w3, pair_addr, dex_name, fee_bps):
    """Load a V2 pair's data."""
    try:
        pair = w3.eth.contract(
            address=w3.to_checksum_address(pair_addr),
            abi=UNISWAP_V2_PAIR_ABI,
        )
        token0 = pair.functions.token0().call().lower()
        token1 = pair.functions.token1().call().lower()
        r0, r1, _ = pair.functions.getReserves().call()
        
        if r0 == 0 or r1 == 0:
            return None, None
        
        tvl = estimate_tvl_v2(token0, token1, r0, r1)
        if not is_above_min_liquidity(tvl, MIN_LIQUIDITY_USD):
            return None, None
        
        return (
            PoolEdge(pair_addr, dex_name, token0, token1, r0, r1, fee_bps, 2, tvl_usd=tvl),
            PoolEdge(pair_addr, dex_name, token1, token0, r1, r0, fee_bps, 2, tvl_usd=tvl),
        )
    except Exception as e:
        log.debug(f"Error loading pair {pair_addr[:10]}: {e}")
        return None, None


def _load_v3_pool(w3, pool_addr, token0, token1, fee_bps, dex_name):
    """Load a V3 pool's data."""
    try:
        pool = w3.eth.contract(
            address=w3.to_checksum_address(pool_addr),
            abi=UNISWAP_V3_POOL_ABI,
        )
        slot0 = pool.functions.slot0().call()
        liq = pool.functions.liquidity().call()
        sqrt = slot0[0]
        
        if sqrt == 0:
            return None, None
        
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
    except Exception as e:
        log.debug(f"_load_v3_pool {pool_addr[:10]}: {e}")
        return None, None


def update_pool_reserves(
    w3: "Web3",
    graph: TokenGraph,
    pool_address: str,
    cache: PoolCache = None,
) -> None:
    """Update pool reserves from on-chain data."""
    pool_address = pool_address.lower()
    edges = graph._pool_index.get(pool_address)
    if not edges:
        return
    
    version = edges[0].version
    dex_name = edges[0].dex_name
    fee_bps = edges[0].fee_bps
    token0 = min(edges[0].token_in, edges[0].token_out)
    token1 = max(edges[0].token_in, edges[0].token_out)
    
    r0, r1 = 0, 0
    sqrt_price, liquidity = 0, 0
    
    try:
        if version == 2:
            pair = w3.eth.contract(
                address=w3.to_checksum_address(pool_address),
                abi=UNISWAP_V2_PAIR_ABI,
            )
            r0, r1, _ = pair.functions.getReserves().call()
            tvl_usd = estimate_tvl_v2(token0, token1, r0, r1)
        else:
            pool = w3.eth.contract(
                address=w3.to_checksum_address(pool_address),
                abi=UNISWAP_V3_POOL_ABI,
            )
            slot0 = pool.functions.slot0().call()
            liquidity = pool.functions.liquidity().call()
            sqrt_price = slot0[0]
            
            # If sqrt_price is zero, remove the pool
            if sqrt_price == 0:
                graph.remove_pool(pool_address)
                if cache:
                    cache.mark_below_threshold(pool_address)
                return
            
            tvl_usd = estimate_tvl_v3(token0, token1, sqrt_price, liquidity)
        # TVL Filtering (Spec 3.4)

        is_below = not is_above_min_liquidity(tvl_usd, MIN_LIQUIDITY_USD)
        
        # If below threshold, remove from graph and mark in cache
        if is_below:
            graph.remove_pool(pool_address)
            if cache:
                cache.mark_below_threshold(pool_address)
            log.debug(f"Pool {pool_address[:8]} marked below threshold (${tvl_usd:.1f})")
            return

        # 7. Update Graph Edges (Both Directions)
        # from graph import PoolEdge # Already imported
        edge_a = PoolEdge(
            pool_address=pool_address, dex_name=dex_name,
            token_in=token0, token_out=token1,
            reserve_in=r0, reserve_out=r1,
            sqrt_price_x96=sqrt_price, liquidity=liquidity,
            fee_bps=fee_bps, version=version,
            tvl_usd=tvl_usd, is_pending=is_below
        )
        edge_b = PoolEdge(
            pool_address=pool_address, dex_name=dex_name,
            token_in=token1, token_out=token0,
            reserve_in=r1, reserve_out=r0,
            sqrt_price_x96=int(2**192 / sqrt_price**2 * 2**96) if version == 3 and sqrt_price > 0 else 0, # Calculate inv here
            liquidity=liquidity if version == 3 else 0,
            fee_bps=fee_bps, version=version,
            tvl_usd=tvl_usd, is_pending=is_below
        )

        graph.add_or_update_pool(edge_a, edge_b)
        
        # 8. Update Cache
        if cache:
            cache.update_reserves(pool_address, r0=r0, r1=r1, sqrt_price_x96=sqrt_price, liquidity=liquidity)
    except Exception as e:
        log.debug(f"reserve update failed {pool_address}: {e}")


# ── In-process pair cache to avoid repeat RPC calls ──────────────────────────
# Key: (t0_lower, t1_lower, dex_name)  Value: pool_addr | None | "zero"
# "zero" means the factory returned the null address (pair doesn't exist)
_PAIR_CACHE: dict[tuple, str | None] = {}


async def discover_from_swap(
    w3: "Web3", 
    graph: TokenGraph, 
    cache: PoolCache, 
    swap: "DecodedSwap", 
    graph_client: GraphClient | None = None,
    logger_obj: Any = None
) -> None:
    """Attempt to discover and add pools from a decoded swap path."""
    if not swap.token_path or len(swap.token_path) < 2:
        return

    tokens = swap.token_path
    chain  = get_chain()
    NULL   = "0x0000000000000000000000000000000000000000"

    for i in range(len(tokens) - 1):
        t0 = tokens[i].lower()
        t1 = tokens[i + 1].lower()

        for dex in chain.dexes:
            factory_addr = dex.factory
            v            = dex.version
            dex_name     = dex.name

            # Skip if already tracked in graph for this DEX
            if any(
                e.dex_name == dex_name
                for e in graph.get_edges_from(t0)
                if e.token_out == t1
            ):
                continue

            try:
                if v == 2:
                    cache_key = (min(t0, t1), max(t0, t1), dex_name)
                    cached = _PAIR_CACHE.get(cache_key)
                    if cached == "zero":          # known non-existent — skip RPC
                        continue
                    if cached is None:            # not queried yet
                        factory  = w3.eth.contract(
                            address=w3.to_checksum_address(factory_addr),
                            abi=UNISWAP_V2_FACTORY_ABI,
                        )
                        # RPC call wrapped in executor
                        pool_addr = await asyncio.get_event_loop().run_in_executor(
                            None, factory.functions.getPair(
                                w3.to_checksum_address(t0),
                                w3.to_checksum_address(t1),
                            ).call
                        )
                        _PAIR_CACHE[cache_key] = "zero" if pool_addr == NULL else pool_addr
                        cached = _PAIR_CACHE[cache_key]

                    if cached and cached != "zero":
                        await register_new_pool(
                            w3, graph, cache, graph_client,
                            cached, dex_name, 2, dex.fee_bps, 
                            token0=t0, token1=t1, logger_obj=logger_obj
                        )

                elif v == 3:
                    factory = w3.eth.contract(
                        address=w3.to_checksum_address(factory_addr),
                        abi=UNISWAP_V3_FACTORY_ABI,
                    )
                    for fee in [500, 3000, 10000]:
                        cache_key = (min(t0, t1), max(t0, t1), dex_name, fee)
                        cached = _PAIR_CACHE.get(cache_key)
                        if cached == "zero":
                            continue
                        if cached is None:
                            # RPC call wrapped in executor
                            pool_addr = await asyncio.get_event_loop().run_in_executor(
                                None, factory.functions.getPool(
                                    w3.to_checksum_address(t0),
                                    w3.to_checksum_address(t1),
                                    fee,
                                ).call
                            )
                            _PAIR_CACHE[cache_key] = "zero" if pool_addr == NULL else pool_addr
                            cached = _PAIR_CACHE[cache_key]

                        if cached and cached != "zero":
                            await register_new_pool(
                                w3, graph, cache, graph_client,
                                cached, dex_name, 3, fee // 100,
                                token0=t0, token1=t1, logger_obj=logger_obj
                            )

            except Exception as e:
                log.debug(f"discover_from_swap {dex_name} {t0[:6]}/{t1[:6]}: {e}")
                continue

async def register_new_pool(
    w3: "Web3",
    graph: TokenGraph,
    cache: PoolCache,
    graph_client: GraphClient | None,
    pool_addr: str,
    dex_name: str,
    version: int,
    fee_bps: int,
    token0: str | None = None,
    token1: str | None = None,
    logger_obj: Any = None,
) -> None:
    """
    Standardized pool registration. 
    Fetches actual state from The Graph for accurate TVL, falls back to RPC.
    """
    pool_addr = pool_addr.lower()
    if graph.has_pool(pool_addr):
        return

    tvl_usd = 0.0
    t0_price = 0.0
    t1_price = 0.0
    
    # Get semaphore that is properly bound to current event loop
    semaphore = _get_registration_semaphore()

    async with semaphore:
        # Small jitter to prevent thundering herd when many tasks call this at once
        import random
        await asyncio.sleep(random.uniform(0.01, 0.1))
        
        # NOTE: Single-pool subgraph fetching was deliberately removed here to 
        # prevent 429 rate limit errors during heavy pool creation events.
        # Instead, it drops instantly to RPC and registers as 'Pending'.
        # The Crawler's `_batch_subgraph_rescue` will efficiently batched-fetch metadata later.

    # 2. Fallback to RPC if TVL unknown or Subgraph unavailable
    # We always need the latest reserves/liquidity from RPC for simulation anyway
    if version == 2:
        ea, eb = _load_v2_pair(w3, pool_addr, dex_name, fee_bps)
    else:
        # V3 discovery through crawler might not have token0/token1 yet if passed as None
        if not token0 or not token1:
            try:
                pool_contract = w3.eth.contract(address=w3.to_checksum_address(pool_addr), abi=UNISWAP_V3_POOL_ABI)
                token0 = pool_contract.functions.token0().call().lower()
                token1 = pool_contract.functions.token1().call().lower()
            except:
                return # Can't load without tokens

        ea, eb = _load_v3_pool(w3, pool_addr, token0, token1, fee_bps, dex_name)

    if not ea or not eb:
        return

    # Use subgraph TVL if it was better
    if tvl_usd > ea.tvl_usd:
        ea.tvl_usd = tvl_usd
        eb.tvl_usd = tvl_usd
    
    # 3. Determine if this pool is "Pending" (Waiting Room)
    # If TVL is 0 but we have reserves, it's pending (we don't have token prices yet)
    is_pending = False
    if ea.tvl_usd <= 0:
        if version == 2 and (ea.reserve_in > 0 or ea.reserve_out > 0):
            is_pending = True
        elif version == 3 and ea.liquidity > 0:
            is_pending = True
    
    ea.is_pending = is_pending
    eb.is_pending = is_pending

    # Final verification: If not pending, it MUST be above min liquidity
    if not is_pending and not is_above_min_liquidity(ea.tvl_usd, MIN_LIQUIDITY_USD):
        return

    _add_new_discovery(graph, cache, pool_addr, ea, eb, logger_obj=logger_obj)


def _add_new_discovery(graph, cache, pool_addr, ea, eb, logger_obj=None):
    """Add new discovered pool to graph and persistent cache."""
    is_pending = getattr(ea, "is_pending", False)
    
    if is_pending:
        log.info(f"Pool {pool_addr[:10]}... added to Waiting Room (TVL unknown)")
    else:
        graph.add_or_update_pool(ea, eb)
        log.research(f"NEW POOL DISCOVERED: {pool_addr[:10]}... | {ea.token_in[:6]}/{ea.token_out[:6]} | TVL: ${ea.tvl_usd:,.0f}")
    
    if logger_obj and not is_pending:
        from logger import Opportunity, OppType
        from config import CHAIN_ID
        opp = Opportunity(
            opp_type=OppType.NEW_POOL,
            chain_id=CHAIN_ID,
            block_number=0,
            timestamp=time.time(),
            tokens=[ea.token_in, ea.token_out],
            pools=[pool_addr],
            dexes=[ea.dex_name],
            estimated_profit_usd=0,
            net_profit_usd=0,
            metadata={
                "version": ea.version,
                "fee_bps": ea.fee_bps,
                "tvl_usd": round(ea.tvl_usd, 2),
                "note": "Discovered from live swap",
            },
        )
        try:
            logger_obj.log(opp)
        except Exception as e:
            log.warning(f"Failed to log NEW_POOL to OppLogger: {e}")

    if cache:
        cache.set_pool(pool_addr.lower(), {
            "token0": ea.token_in, "token1": ea.token_out,
            "dex": ea.dex_name, "version": ea.version,
            "fee_bps": ea.fee_bps, "r0": ea.reserve_in,
            "r1": ea.reserve_out, "sqrt_price_x96": ea.sqrt_price_x96,
            "liquidity": ea.liquidity, "last_updated": time.time(),
            "tvl_usd": ea.tvl_usd,
            "is_pending": is_pending,
        })
        cache.save()


