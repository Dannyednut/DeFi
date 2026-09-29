"""
Enhanced Registry Module
==================
Fast startup bootstrap from cache + seed pools.
"""
from __future__ import annotations

import time
import asyncio
from dataclasses import replace
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
from utils.gecko_terminal import GeckoTerminal

from log import get_logger
log = get_logger("registry")

# Subgraph usage in registry
from utils.graph_client import GraphClient
from protocols import CurveAdapter, PoolRef, SyncSwapAdapter

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
                ea, eb = _load_v2_pair(
                    w3, addr, seed["dex"], seed["fee_bps"],
                    token0=seed.get("token0"), token1=seed.get("token1")
                )
            else:
                ea, eb = _load_v3_pool(
                    w3, addr, seed["fee_bps"], seed["dex"],
                    token0=seed.get("token0"), token1=seed.get("token1")
                )
            
            if ea and eb:
                graph.add_or_update_pool(ea, eb)
                cache.set_pool(addr, {
                    "token0": ea.pool_token0 or seed.get("token0") or ea.token_in, "token1": ea.pool_token1 or seed.get("token1") or ea.token_out,
                    "dex": ea.dex_name, "protocol": ea.protocol, "version": seed["version"],
                    "fee_bps": ea.fee_bps, "r0": ea.reserve_in,
                    "r1": ea.reserve_out, "sqrt_price_x96": ea.sqrt_price_x96,
                    "liquidity": ea.liquidity, "last_updated": time.time(),
            "state_block": getattr(ea, "state_block", 0),
            "state_block_hash": getattr(ea, "state_block_hash", ""),
            "state_source": getattr(ea, "state_source", "discovery"),
            "state_status": getattr(ea, "state_status", "UNKNOWN"),
                    "tvl_usd": ea.tvl_usd,
                })
                seeds_loaded += 1
        except Exception as e:
            log.warning(f"Seed {addr[:12]} failed: {e}")
    
    log.info(f"Seed pools: {seeds_loaded} loaded")
    
    # cache.save()
    graph.find_cycles(max_hops=3)  # Fast precompute for summary stats
    log.info(f"Bootstrap complete — {graph.summary()}")


def _load_v2_pair(w3, pair_addr, dex_name, fee_bps, token0=None, token1=None):
    """Load a V2 pair's data.

    Delegates to UniswapV2Adapter.load_pool() instead of hand-rolling the
    token0()/token1()/getReserves() RPC sequence here directly — this used
    to duplicate (and could silently drift from) protocols/uniswap_v2.py's
    own implementation of the exact same calls. Signature/return shape kept
    identical so callers (register_new_pool, the crawler's stale-refresher,
    tests) don't need to change.
    """
    try:
        from protocols import UniswapV2Adapter, PoolRef
        dex_cfg = next((d for d in get_chain().dexes if d.name == dex_name), None)
        if dex_cfg is None:
            return None, None
        adapter = UniswapV2Adapter(w3, dex_cfg)
        ref = PoolRef(pair_addr, "uniswap_v2", dex_name, token0, token1, fee_bps, {})
        state = adapter.load_pool(ref)
        if not state:
            return None, None
        token0, token1, r0, r1 = state["token0"], state["token1"], state["r0"], state["r1"]

        tvl = estimate_tvl_v2(token0, token1, r0, r1, pool_address=pair_addr)
        if not is_above_min_liquidity(tvl, MIN_LIQUIDITY_USD):
            return None, None

        return (
            PoolEdge(pair_addr, dex_name, token0, token1, r0, r1, fee_bps, 2, tvl_usd=tvl, protocol="uniswap_v2", pool_token0=token0, pool_token1=token1),
            PoolEdge(pair_addr, dex_name, token1, token0, r1, r0, fee_bps, 2, tvl_usd=tvl, protocol="uniswap_v2", pool_token0=token0, pool_token1=token1),
        )
    except Exception as e:
        log.debug(f"Error loading pair {pair_addr[:10]}: {e}")
        return None, None


def _load_v3_pool(w3, pool_addr, fee_bps, dex_name, token0 = None, token1 = None):
    """Load a V3 pool's data.

    Delegates to UniswapV3Adapter.load_pool() — see _load_v2_pair's
    docstring above for why (was hand-rolled RPC here, duplicating what the
    adapter now does as the single source of truth).
    """
    try:
        from protocols import UniswapV3Adapter, PoolRef
        dex_cfg = next((d for d in get_chain().dexes if d.name == dex_name), None)
        if dex_cfg is None:
            return None, None
        adapter = UniswapV3Adapter(w3, dex_cfg)
        ref = PoolRef(pool_addr, "uniswap_v3", dex_name, token0, token1, fee_bps, {})
        state = adapter.load_pool(ref)
        if not state:
            return None, None
        token0, token1 = state["token0"], state["token1"]
        sqrt, liq = state["sqrt_price_x96"], state["liquidity"]

        tvl = estimate_tvl_v3(token0, token1, sqrt, liq, pool_address=pool_addr)
        if not is_above_min_liquidity(tvl, MIN_LIQUIDITY_USD):
            return None, None

        return (
            PoolEdge(pool_addr, dex_name, token0, token1, 0, 0, fee_bps, 3,
                    sqrt_price_x96=sqrt, liquidity=liq, tvl_usd=tvl, protocol="uniswap_v3", pool_token0=token0, pool_token1=token1),
            PoolEdge(pool_addr, dex_name, token1, token0, 0, 0, fee_bps, 3,
                    sqrt_price_x96=sqrt, liquidity=liq, tvl_usd=tvl, protocol="uniswap_v3", pool_token0=token0, pool_token1=token1),
        )
    except Exception as e:
        log.debug(f"_load_v3_pool {pool_addr[:10]}: {e}")
        return None, None

def _load_curve_pool(w3, pool_addr: str, dex_name: str, metadata: dict) -> tuple[PoolEdge, PoolEdge] | tuple[None, None]:
    """Load a Curve pool using API-discovered coin indices and on-chain balances.

    Curve is intentionally not forced into the V2 reserve model for quoting; the
    balances are retained for TVL/context while exact quotes use get_dy().
    """
    try:
        from config import get_chain
        dex = next(d for d in get_chain().dexes if d.name == dex_name)
        adapter = CurveAdapter(w3, dex)
        ref = PoolRef(pool_addr, "curve", dex_name, metadata.get("token0"), metadata.get("token1"), 0, metadata)
        state = adapter.load_pool(ref)
        if not state:
            return None, None
        t0, t1 = state["token0"].lower(), state["token1"].lower()
        r0, r1 = state["r0"], state["r1"]
        tvl = float(metadata.get("tvl_usd") or 0)
        # Use a tiny on-chain probe to establish the directed graph spot rate.
        decimals = 18
        try:
            from tvl import token_decimals
            decimals = token_decimals(t0)
        except Exception:
            pass
        probe = 10 ** max(1, decimals - 4)
        q = adapter.quote_exact_in(ref, t0, probe)
        spot = (q.amount_out / probe) if q and probe else 0.0
        meta0 = {**metadata, "token0": t0, "token1": t1, "spot_rate": spot}
        meta1 = {**metadata, "token0": t0, "token1": t1, "spot_rate": (1.0 / spot if spot > 0 else 0.0)}
        return (
            PoolEdge(pool_addr, dex_name, t0, t1, r0, r1, 0, 2, tvl_usd=tvl, protocol="curve", pool_token0=t0, pool_token1=t1, metadata=meta0),
            PoolEdge(pool_addr, dex_name, t1, t0, r1, r0, 0, 2, tvl_usd=tvl, protocol="curve", pool_token0=t0, pool_token1=t1, metadata=meta1),
        )
    except Exception as e:
        log.debug(f"_load_curve_pool {pool_addr[:10]}: {e}")
        return None, None

def prune_below_threshold(pool_address: str, graph: TokenGraph, cache: PoolCache | None) -> bool:
    """Remove a pool that has fallen below the minimum-liquidity threshold.

    NOTE: this function was called from update_pool_reserves() (both the
    V2/V3 branch and the simple-reserve-pair branch used by
    refresh_pools_batched()) but was never actually defined anywhere in the
    codebase — the call raised NameError, which the caller's surrounding
    try/except silently swallowed (logged at debug level only). Net effect:
    illiquid pools were never actually pruned via this path. Adding the
    implementation here, matching the same remove+mark pattern already used
    inline elsewhere (utils/multicall3.py's V2/V3 refresh, and the V3
    zero-sqrt-price branch just above in this file).
    """
    graph.remove_pool(pool_address)
    if cache:
        cache.mark_below_threshold(pool_address)
    return True


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
    token0 = (edges[0].pool_token0 or edges[0].token_in).lower()
    token1 = (edges[0].pool_token1 or edges[0].token_out).lower()
    
    r0, r1 = 0, 0
    sqrt_price, liquidity = 0, 0
    
    try:
        protocol = edges[0].protocol or ("uniswap_v3" if version == 3 else "uniswap_v2")
        if protocol == "curve":
            for existing_edge in edges:
                metadata = dict(existing_edge.metadata or {})
                metadata.setdefault("token0", existing_edge.pool_token0 or existing_edge.token_in)
                metadata.setdefault("token1", existing_edge.pool_token1 or existing_edge.token_out)
                ea, eb = _load_curve_pool(w3, pool_address, dex_name, metadata)
                if ea and eb:
                    graph.add_or_update_pool(ea, eb)
            if cache:
                cache.set_pool(pool_address, {"protocol": "curve", "metadata": {"pairs": [e.metadata for e in graph.get_edges_for_pool(pool_address)]}})
            return
        if protocol == "balancer_v2":
            from protocols import BalancerV2Adapter, PoolRef
            dex_cfg = next((d for d in get_chain().dexes if d.name == dex_name), None)
            if dex_cfg is None:
                return
            base_meta = dict(edges[0].metadata or {})
            ref = PoolRef(pool_address, protocol, dex_name, token0, token1, fee_bps, base_meta)
            state = BalancerV2Adapter(w3, dex_cfg).load_pool(ref)
            if not state:
                return
            balances = {t.lower(): int(b) for t, b in zip(state["tokens"], state["balances"])}
            weights = base_meta.get("weights") or []
            tokens = state["tokens"]
            refreshed = []
            for e in edges:
                # Use the edge's OWN token_in/token_out here, not
                # pool_token0/pool_token1 -- for Balancer's N-token pools,
                # pool_token0/pool_token1 are fixed to whichever pair a given
                # register_new_pool() call was made with, and BOTH directions
                # of that pair (A->B and B->A) share the same pool_token0/
                # pool_token1 values. Using them instead of token_in/
                # token_out here meant the "reverse" edge of every Balancer
                # pair computed the FORWARD direction's spot rate and stored
                # it as its own -- i.e. every reverse-direction Balancer edge
                # has always priced trades using the reciprocal of its real
                # rate (confirmed present in the original codebase, not
                # introduced by later changes here). token_in/token_out are
                # unambiguous per-edge and need no such fallback.
                t_in, t_out = e.token_in.lower(), e.token_out.lower()
                if t_in not in balances or t_out not in balances:
                    continue
                i, j = tokens.index(t_in), tokens.index(t_out)
                r_in, r_out = balances[t_in], balances[t_out]
                meta = dict(e.metadata or {})
                spot = ((r_out / r_in) * (float(weights[i]) / float(weights[j]))
                        if i < len(weights) and j < len(weights) and weights[i] > 0 and weights[j] > 0 and r_in > 0 else 0.0)
                spot *= max(0.0, 1.0 - float(base_meta.get("swap_fee", 0) or 0))
                meta.update({"spot_rate": spot, "last_change_block": int(state["last_change_block"])})
                # reserve_in/reserve_out on the edge must match its own
                # direction too (this used to store balances[t0]/balances[t1]
                # from the mis-derived t0/t1 above, which is the same bug).
                refreshed.append(replace(e, reserve_in=r_in, reserve_out=r_out, metadata=meta))
            if refreshed:
                graph.upsert_pool_edges(refreshed)
            if cache:
                cache.update_reserves(pool_address, r0=balances.get(token0, 0), r1=balances.get(token1, 0))
            return
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
            return prune_below_threshold(pool_address, graph, cache)

        # 7. Update Graph Edges (Both Directions)
        # from graph import PoolEdge # Already imported
        edge_a = PoolEdge(
            pool_address=pool_address, dex_name=dex_name,
            token_in=token0, token_out=token1,
            reserve_in=r0, reserve_out=r1,
            sqrt_price_x96=sqrt_price, liquidity=liquidity,
            fee_bps=fee_bps, version=version,
            tvl_usd=tvl_usd, is_pending=is_below,
            protocol=(edges[0].protocol or ("uniswap_v3" if version == 3 else "uniswap_v2")),
            pool_token0=token0, pool_token1=token1
        )
        edge_b = PoolEdge(
            pool_address=pool_address, dex_name=dex_name,
            token_in=token1, token_out=token0,
            reserve_in=r1, reserve_out=r0,
            sqrt_price_x96=sqrt_price if version == 3 else 0,
            liquidity=liquidity if version == 3 else 0,
            fee_bps=fee_bps, version=version,
            tvl_usd=tvl_usd, is_pending=is_below,
            protocol=(edges[0].protocol or ("uniswap_v3" if version == 3 else "uniswap_v2")),
            pool_token0=token0, pool_token1=token1
        )

        graph.add_or_update_pool(edge_a, edge_b)
        
        # 8. Update Cache
        if cache:
            cache.update_reserves(pool_address, r0=r0, r1=r1, sqrt_price_x96=sqrt_price, liquidity=liquidity)
    except Exception as e:
        log.debug(f"reserve update failed {pool_address}: {e}")


def apply_adapter_state(
    graph: TokenGraph,
    cache: PoolCache | None,
    pool_address: str,
    dex_name: str,
    protocol: str,
    edges: list[PoolEdge],
    state: dict[str, Any],
) -> bool:
    """Apply an already-decoded adapter state dict (the shape returned by
    ProtocolAdapter.load_pool()/decode_refresh_result()) to the graph.

    Extracted from update_pool_reserves()'s per-protocol branches so the same
    "turn decoded state into graph edges" logic can be shared between the
    sequential one-pool-at-a-time path (update_pool_reserves) and the batched
    Multicall3 path (refresh_pools_batched, below) — previously this logic
    only existed inline inside the sequential path.

    Returns True if any edge was updated.
    """
    if not state:
        return False
    try:
        if protocol == "balancer_v2":
            balances = {t.lower(): int(b) for t, b in zip(state["tokens"], state["balances"])}
            tokens = state["tokens"]
            base_meta = dict(edges[0].metadata or {}) if edges else {}
            weights = base_meta.get("weights") or []
            refreshed = []
            for e in edges:
                # See the matching comment in update_pool_reserves()'s
                # balancer branch above: must use the edge's own
                # token_in/token_out, not pool_token0/pool_token1 (which are
                # shared, direction-independent values for a Balancer pair
                # and would give every reverse-direction edge the forward
                # direction's rate instead of its own).
                t_in, t_out = e.token_in.lower(), e.token_out.lower()
                if t_in not in balances or t_out not in balances:
                    continue
                i, j = tokens.index(t_in), tokens.index(t_out)
                r_in, r_out = balances[t_in], balances[t_out]
                meta = dict(e.metadata or {})
                spot = ((r_out / r_in) * (float(weights[i]) / float(weights[j]))
                        if i < len(weights) and j < len(weights) and weights[i] > 0 and weights[j] > 0 and r_in > 0 else 0.0)
                spot *= max(0.0, 1.0 - float(base_meta.get("swap_fee", 0) or 0))
                meta.update({"spot_rate": spot, "last_change_block": int(state.get("last_change_block", 0))})
                refreshed.append(replace(e, reserve_in=r_in, reserve_out=r_out, metadata=meta))
            if not refreshed:
                return False
            graph.upsert_pool_edges(refreshed)
            if cache:
                cache.update_reserves(pool_address, r0=refreshed[0].reserve_in, r1=refreshed[0].reserve_out)
            return True

        if protocol == "curve":
            # Each curve (i, j) direction is its own graph edge-pair with its
            # own metadata; state here already carries the specific i/j this
            # call refreshed (see CurveAdapter.decode_refresh_result).
            r0, r1 = int(state["r0"]), int(state["r1"])
            spot_rate = state.get("spot_rate")
            refreshed = []
            for e in edges:
                em = dict(e.metadata or {})
                if int(em.get("i", -1)) != int(state.get("i", -2)) or int(em.get("j", -1)) != int(state.get("j", -2)):
                    continue
                # spot_rate MUST be recomputed here, not just carried over
                # from whatever it was at initial pool registration.
                # PoolEdge.exchange_rate for protocol=="curve" reads
                # metadata["spot_rate"] directly (Curve isn't priced as a
                # simple balance ratio the way V2/SyncSwap/Aerodrome are) --
                # a previous version of this branch updated reserve_in/
                # reserve_out but left metadata (and therefore spot_rate)
                # completely untouched, which meant opportunity detection
                # for every Curve pool used an increasingly stale price the
                # longer the process ran, even though the underlying
                # balances genuinely were being refreshed. Only overwrite
                # spot_rate when the probe quote succeeded (see
                # decode_refresh_result) -- keep the previous value rather
                # than zeroing out a pool's price on a transient revert.
                if spot_rate is not None:
                    em["spot_rate"] = spot_rate
                refreshed.append(replace(e, reserve_in=r0, reserve_out=r1, metadata=em))
            if not refreshed:
                return False
            graph.upsert_pool_edges(refreshed)
            if cache:
                cache.update_reserves(pool_address, r0=r0, r1=r1)
            return True

        # syncswap / aerodrome_v2 / any other simple-reserve-pair protocol
        r0, r1 = int(state.get("r0", 0)), int(state.get("r1", 0))
        if not r0 or not r1:
            return False
        token0 = state.get("token0") or (edges[0].pool_token0 or edges[0].token_in)
        token1 = state.get("token1") or (edges[0].pool_token1 or edges[0].token_out)
        tvl_usd = estimate_tvl_v2(token0, token1, r0, r1, dex=dex_name)
        if not is_above_min_liquidity(tvl_usd, MIN_LIQUIDITY_USD):
            return prune_below_threshold(pool_address, graph, cache) or False
        # spot_rate is only present for Aerodrome "stable" pools (see
        # AerodromeV2Adapter.build_refresh_calls/decode_refresh_result) --
        # exchange_rate reads it instead of the reserve ratio for those.
        # Only overwrite when the probe succeeded; keep whatever was there on
        # a transient revert rather than zeroing a stable pool's price out.
        spot_rate = state.get("spot_rate")
        refreshed = []
        for e in edges:
            fwd = e.token_in.lower() == token0.lower()
            meta = dict(e.metadata or {})
            if spot_rate is not None:
                # Direction matters here exactly like Balancer above: the
                # probe above was quoted token0->token1, so the reverse edge
                # needs the reciprocal, not the same value.
                meta["spot_rate"] = spot_rate if fwd else (1.0 / spot_rate if spot_rate > 0 else 0.0)
            refreshed.append(replace(
                e,
                reserve_in=(r0 if fwd else r1),
                reserve_out=(r1 if fwd else r0),
                tvl_usd=tvl_usd,
                metadata=meta,
            ))
        if not refreshed:
            return False
        graph.upsert_pool_edges(refreshed)
        if cache:
            cache.update_reserves(pool_address, r0=r0, r1=r1)
        return True
    except Exception as e:
        log.debug(f"apply_adapter_state failed protocol={protocol} pool={pool_address}: {e}")
        return False


def refresh_pools_batched(
    w3: "Web3",
    graph: TokenGraph,
    cache: PoolCache | None,
    addresses: set[str],
) -> set[str]:
    """Refresh non-V2/V3 pools (Curve, Balancer, SyncSwap, Aerodrome, ...) in
    as few Multicall3 requests as possible, instead of one eth_call sequence
    per pool. Falls back to the sequential update_pool_reserves() path for
    any pool whose adapter doesn't support batched refresh.

    This is the "curve/balancer/other adapters should build the calls
    instead" fast path — see ProtocolAdapter.build_refresh_calls() /
    decode_refresh_result() in protocols/base.py.
    """
    from protocols import ProtocolRegistry
    from utils.multicall3 import multicall3_bulk_refresh_adapters

    if not addresses:
        return set()

    registry = ProtocolRegistry(w3)
    chain = get_chain()
    batchable: list[tuple[PoolRef, Any, str, str, list[PoolEdge]]] = []  # (ref, adapter, dex, protocol, edges)
    fallback: set[str] = set()

    for addr in addresses:
        addr = addr.lower()
        edges = graph._pool_index.get(addr)
        if not edges:
            continue
        protocol = edges[0].protocol
        dex_name = edges[0].dex_name
        dex_cfg = next((d for d in chain.dexes if d.name == dex_name), None)
        if dex_cfg is None:
            fallback.add(addr)
            continue
        adapter = registry.get(dex_cfg)
        if adapter is None or not adapter.supports_batched_refresh():
            fallback.add(addr)
            continue
        # Curve needs one PoolRef per distinct (i, j) direction, since each
        # is a separate refresh call; other protocols need just one PoolRef
        # per physical pool address.
        if protocol == "curve":
            seen_ij = set()
            for e in edges:
                em = dict(e.metadata or {})
                ij = (em.get("i"), em.get("j"))
                if ij in seen_ij or ij == (None, None):
                    continue
                seen_ij.add(ij)
                ref = PoolRef(addr, protocol, dex_name,
                               e.pool_token0 or e.token_in, e.pool_token1 or e.token_out,
                               e.fee_bps, em)
                batchable.append((ref, adapter, dex_name, protocol, edges))
        else:
            e0 = edges[0]
            ref = PoolRef(addr, protocol, dex_name,
                           e0.pool_token0 or e0.token_in, e0.pool_token1 or e0.token_out,
                           e0.fee_bps, dict(e0.metadata or {}))
            batchable.append((ref, adapter, dex_name, protocol, edges))

    updated: set[str] = set()
    if batchable:
        pairs = [(ref, adapter) for ref, adapter, _, _, _ in batchable]
        decoded = multicall3_bulk_refresh_adapters(w3, pairs)
        by_addr = {}
        for ref, adapter, dex_name, protocol, edges in batchable:
            by_addr.setdefault(ref.address, []).append((ref, protocol, dex_name, edges))
        for addr, state in decoded.items():
            for ref, protocol, dex_name, edges in by_addr.get(addr, []):
                if apply_adapter_state(graph, cache, addr, dex_name, protocol, edges, state):
                    updated.add(addr)

    for addr in fallback:
        try:
            update_pool_reserves(w3, graph, addr, cache)
            updated.add(addr)
        except Exception as e:
            log.debug(f"fallback refresh failed {addr}: {e}")

    return updated


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
    """Discover pools referenced by a decoded swap path.

    Generic V2/V3 factory checks are batched through Multicall3. Specialized
    protocols keep their adapter-specific discovery because their factory/state
    semantics differ from Uniswap's pair/pool model.
    """
    if not swap.token_path or len(swap.token_path) < 2:
        return
    chain = get_chain()
    token_pairs = [(swap.token_path[i].lower(), swap.token_path[i + 1].lower()) for i in range(len(swap.token_path) - 1)]

    try:
        from utils.multicall3 import multicall3_discover_token_pairs
        candidates = await asyncio.get_running_loop().run_in_executor(None, multicall3_discover_token_pairs, w3, token_pairs)
    except Exception as exc:
        log.debug("batched swap-path discovery failed: %s", exc)
        candidates = []

    for c in candidates:
        dex = c["dex"]
        pool_addr = c["pool_address"]
        if graph.has_pool(pool_addr):
            continue
        try:
            fee = c["fee"] // 100 if dex.version == 3 and c["fee"] is not None else dex.fee_bps
            await register_new_pool(
                w3, graph, cache, graph_client, pool_addr, dex.name, dex.version, fee,
                token0=c["token0"], token1=c["token1"], logger_obj=logger_obj,
                protocol=dex.protocol, metadata={"discovered_from_swap": True}, expand_related=False,
            )
        except Exception as exc:
            log.debug("swap-path registration failed %s %s: %s", dex.name, pool_addr[:10], exc)

    # Specialized factories are not included in the generic Multicall3 factory
    # batch above (that one only knows Uniswap's pair/pool factory ABI shape).
    # SyncSwap/Aerodrome DO have a compatible "factory.getPool(...)" shape
    # though, just with different signatures per protocol — batch those too,
    # across every (pair, dex) combination in one or two Multicall3 requests,
    # instead of a sequential adapter.discover_pair() call per combination.
    lookup_entries = []
    for t0, t1 in token_pairs:
        for dex in chain.dexes:
            if not dex.enabled or dex.protocol not in {"syncswap", "aerodrome_v2"}:
                continue
            try:
                from protocols import ProtocolRegistry
                adapter = ProtocolRegistry(w3).get(dex)
            except Exception:
                adapter = None
            if adapter is None or not adapter.supports_batched_pair_lookup():
                continue
            lookup_entries.append((adapter, t0, t1, dex))

    if lookup_entries:
        try:
            from utils.multicall3 import multicall3_batch_lookup_pairs
            triples = [(a, t0, t1) for a, t0, t1, _ in lookup_entries]
            refs = await asyncio.get_running_loop().run_in_executor(None, multicall3_batch_lookup_pairs, w3, triples)
            dex_by_name = {d.name: d for d in chain.dexes}
            for ref in refs:
                if graph.has_pool(ref.address):
                    continue
                dex = dex_by_name.get(ref.dex)
                if dex is None:
                    continue
                await register_new_pool(
                    w3, graph, cache, graph_client, ref.address, dex.name, dex.version, dex.fee_bps,
                    token0=ref.token0, token1=ref.token1, logger_obj=logger_obj,
                    protocol=dex.protocol, metadata=ref.metadata, expand_related=False,
                )
        except Exception as exc:
            log.debug("batched specialized swap-path discovery failed: %s", exc)
async def discover_from_event(
    w3: "Web3",
    graph: TokenGraph,
    cache: PoolCache,
    graph_client: GraphClient | None,
    hint: dict,
) -> bool:
    """Discover a pool directly from a recognized protocol event.

    This is deliberately background-only: the block processor records the
    hint and the crawler performs the RPC work later.  The event address is a
    stronger signal than an arbitrary contract log, but registration still
    validates the pool through the normal protocol-specific loaders.
    """
    address = str(hint.get("pool_address", "")).lower()
    protocol = str(hint.get("protocol", "")).lower()
    if not address.startswith("0x") or graph.has_pool(address):
        return False
    chain = get_chain()
    dexes = [d for d in chain.dexes if d.enabled and d.protocol == protocol]
    if not dexes:
        return False

    for dex in dexes:
        try:
            if protocol == "curve":
                topics = hint.get("topics") or []
                if len(topics) < 4:
                    continue
                i = int(topics[2], 16)
                j = int(topics[3], 16)
                curve = CurveAdapter(w3, dex)
                c = curve._pool_contract(address)
                token0 = c.functions.coins(i).call().lower()
                token1 = c.functions.coins(j).call().lower()
                await register_new_pool(w3, graph, cache, graph_client, address, dex.name, 2, 0,
                                        token0=token0, token1=token1, logger_obj=None,
                                        protocol=protocol, metadata={"i": i, "j": j, "event_discovered": True}, expand_related=False)
                return graph.has_pool(address)

            if protocol == "balancer_v2":
                topics = hint.get("topics") or []
                if len(topics) < 2:
                    continue
                pool_id = topics[1]
                adapter = __import__("protocols", fromlist=["BalancerV2Adapter"]).BalancerV2Adapter(w3, dex)
                state = adapter.load_pool(PoolRef(address, protocol, dex.name, None, None, 0, {"pool_id": pool_id}))
                if not state or len(state.get("tokens", [])) < 2:
                    continue
                # Registering every directional pair preserves the multi-token
                # graph semantics; each registration is still gated by the
                # canonical pool identity in register_new_pool.
                tokens = state["tokens"]
                for a in tokens:
                    for b in tokens:
                        if a != b:
                            await register_new_pool(
                                w3, graph, cache, graph_client, address, dex.name, 2, 0,
                                token0=a, token1=b, logger_obj=None, protocol=protocol,
                                metadata={"pool_id": pool_id, "coins": tokens, "event_discovered": True}, expand_related=False,
                            )
                return graph.has_pool(address)

            if protocol in {"syncswap", "aerodrome_v2"}:
                adapter_cls = SyncSwapAdapter if protocol == "syncswap" else __import__("protocols", fromlist=["AerodromeV2Adapter"]).AerodromeV2Adapter
                adapter = adapter_cls(w3, dex)
                # Both adapters can validate an address and recover canonical
                # token0/token1 state without knowing the originating swap path.
                abi = PoolRef(address, protocol, dex.name, None, None, dex.fee_bps, {})
                state = adapter.load_pool(abi)
                if not state:
                    continue
                if protocol == "syncswap":
                    expected = adapter._factory().functions.getPool(
                        w3.to_checksum_address(state["token0"]),
                        w3.to_checksum_address(state["token1"]),
                    ).call().lower()
                    if expected != address:
                        continue
                else:
                    # Aerodrome has separate stable/volatile pools.  Verify
                    # that this contract is actually returned by its factory.
                    matches_factory = False
                    for stable in (False, True):
                        expected = adapter._factory().functions.getPool(
                            w3.to_checksum_address(state["token0"]),
                            w3.to_checksum_address(state["token1"]), stable,
                        ).call().lower()
                        if expected == address:
                            matches_factory = True
                            break
                    if not matches_factory:
                        continue
                await register_new_pool(w3, graph, cache, graph_client, address, dex.name, 2, dex.fee_bps,
                                        token0=state["token0"], token1=state["token1"],
                                        logger_obj=None, protocol=protocol, metadata={"event_discovered": True, **({"stable": state.get("stable")} if protocol == "aerodrome_v2" else {})}, expand_related=False)
                return graph.has_pool(address)

            if dex.version == 2:
                pair = w3.eth.contract(address=w3.to_checksum_address(address), abi=UNISWAP_V2_PAIR_ABI)
                token0 = pair.functions.token0().call().lower()
                token1 = pair.functions.token1().call().lower()
                if not dex.factory:
                    continue
                factory = w3.eth.contract(address=w3.to_checksum_address(dex.factory), abi=UNISWAP_V2_FACTORY_ABI)
                expected = factory.functions.getPair(w3.to_checksum_address(token0), w3.to_checksum_address(token1)).call().lower()
                if expected != address:
                    continue
                await register_new_pool(w3, graph, cache, graph_client, address, dex.name, 2, dex.fee_bps,
                                        token0=token0, token1=token1, logger_obj=None, protocol=protocol, expand_related=False)
                return graph.has_pool(address)

            if dex.version == 3:
                pool = w3.eth.contract(address=w3.to_checksum_address(address), abi=UNISWAP_V3_POOL_ABI)
                token0 = pool.functions.token0().call().lower()
                token1 = pool.functions.token1().call().lower()
                if not dex.factory:
                    continue
                factory = w3.eth.contract(address=w3.to_checksum_address(dex.factory), abi=UNISWAP_V3_FACTORY_ABI)
                for fee in (dex.fee_tiers or (100, 500, 2500, 3000, 10000)):
                    expected = factory.functions.getPool(w3.to_checksum_address(token0), w3.to_checksum_address(token1), fee).call().lower()
                    if expected == address:
                        await register_new_pool(w3, graph, cache, graph_client, address, dex.name, 3, fee // 100,
                                                token0=token0, token1=token1, logger_obj=None, protocol=protocol, expand_related=False)
                        return graph.has_pool(address)
        except Exception as exc:
            log.debug("event discovery failed pool=%s protocol=%s: %s", address[:10], protocol, exc)
    return False


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
    protocol: str | None = None,
    metadata: dict | None = None,
    expand_related: bool = True,
) -> None:
    """
    Standardized pool registration. 
    Fetches actual state from The Graph for accurate TVL, falls back to RPC.
    """
    gecko = GeckoTerminal()
    
    pool_addr = pool_addr.lower()
    existing_before = graph.has_pool(pool_addr)
    if existing_before:
        existing = graph.get_edges_for_pool(pool_addr)
        # A pool contract is discovered once. Curve may expose additional
        # token directions from the same contract, so only that protocol is
        # allowed to enrich an existing identity with a genuinely new edge.
        if protocol != "curve" or any(
            e.token_in.lower() == (token0 or "").lower() and
            e.token_out.lower() == (token1 or "").lower() for e in existing
        ):
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
        #
        # IMPORTANT: everything below used to run OUTSIDE this semaphore block
        # (the block used to end right after the jitter sleep above). That
        # meant this Semaphore(5) limited nothing but how many callers could
        # simultaneously sleep for up to 100ms — every actual RPC pool load,
        # the GeckoTerminal TVL lookup, and the recursive related_pools
        # cascade below ran with fully unbounded concurrency across all
        # discovery sources (crawler workers, event discovery, mempool swap
        # discovery). Widened the block to cover the real work so the
        # semaphore actually bounds concurrent load on RPC/GeckoTerminal.

        # 2. Fallback to RPC if TVL unknown or Subgraph unavailable
        # We always need the latest reserves/liquidity from RPC for simulation anyway
        protocol = protocol or next((d.protocol for d in get_chain().dexes if d.name == dex_name), None)
        if protocol == "curve":
            curve_meta = dict(metadata or {})
            curve_meta.setdefault("token0", token0)
            curve_meta.setdefault("token1", token1)
            ea, eb = _load_curve_pool(w3, pool_addr, dex_name, curve_meta)
        elif protocol == "aerodrome_v2":
            from protocols import AerodromeV2Adapter, PoolRef
            dex_cfg = next(d for d in get_chain().dexes if d.name == dex_name)
            ref = PoolRef(pool_addr, protocol, dex_name, token0, token1, fee_bps, metadata or {})
            adapter = AerodromeV2Adapter(w3, dex_cfg)
            state = adapter.load_pool(ref)
            if state:
                tvl = estimate_tvl_v2(state["token0"], state["token1"], state["r0"], state["r1"], dex=dex_name)
                ea = PoolEdge(pool_addr, dex_name, state["token0"], state["token1"], state["r0"], state["r1"], fee_bps, 2, tvl_usd=tvl, protocol=protocol, pool_token0=state["token0"], pool_token1=state["token1"], metadata={"stable": state.get("stable", False)})
                eb = PoolEdge(pool_addr, dex_name, state["token1"], state["token0"], state["r1"], state["r0"], fee_bps, 2, tvl_usd=tvl, protocol=protocol, pool_token0=state["token0"], pool_token1=state["token1"], metadata={"stable": state.get("stable", False)})
            else:
                ea = eb = None
        elif protocol == "balancer_v2":
            from protocols import BalancerV2Adapter, PoolRef
            dex_cfg = next(d for d in get_chain().dexes if d.name == dex_name)
            ref = PoolRef(pool_addr, protocol, dex_name, token0, token1, fee_bps, metadata or {})
            adapter = BalancerV2Adapter(w3, dex_cfg)
            state = adapter.load_pool(ref)
            if state and token0 and token1 and token0.lower() in state["tokens"] and token1.lower() in state["tokens"]:
                i, j = state["tokens"].index(token0.lower()), state["tokens"].index(token1.lower())
                r0, r1 = state["balances"][i], state["balances"][j]
                tvl = float((metadata or {}).get("tvl_usd", 0))
                weights = (metadata or {}).get("weights") or []
                swap_fee = float((metadata or {}).get("swap_fee", 0))
                fee_mult = max(0.0, 1.0 - swap_fee)

                # ea (token0->token1) and eb (token1->token0) each need their
                # OWN spot rate computed in their own direction -- they used
                # to share a single `meta` dict/spot value computed only for
                # the token0->token1 direction, so eb's exchange_rate was
                # actually token0->token1's rate, not its own reverse rate
                # (confirmed present in the original codebase). Balancer's
                # weighted spot formula is exactly reciprocal between
                # directions at a given balance snapshot (unlike Curve's
                # StableSwap invariant), so this is safe to compute directly
                # per direction rather than needing a probe call.
                spot_fwd = 0.0
                if i < len(weights) and j < len(weights) and weights[i] > 0 and weights[j] > 0 and r0 > 0:
                    spot_fwd = (r1 / r0) * (weights[i] / weights[j]) * fee_mult
                spot_rev = 0.0
                if i < len(weights) and j < len(weights) and weights[i] > 0 and weights[j] > 0 and r1 > 0:
                    spot_rev = (r0 / r1) * (weights[j] / weights[i]) * fee_mult

                meta_fwd = dict(metadata or {})
                meta_fwd.update({"weights": weights, "spot_rate": spot_fwd, "pool_id": meta_fwd.get("pool_id")})
                meta_rev = dict(metadata or {})
                meta_rev.update({"weights": weights, "spot_rate": spot_rev, "pool_id": meta_rev.get("pool_id")})
                ea = PoolEdge(pool_addr, dex_name, token0.lower(), token1.lower(), r0, r1, 0, 2, tvl_usd=tvl, protocol=protocol, pool_token0=token0.lower(), pool_token1=token1.lower(), metadata=meta_fwd)
                eb = PoolEdge(pool_addr, dex_name, token1.lower(), token0.lower(), r1, r0, 0, 2, tvl_usd=tvl, protocol=protocol, pool_token0=token0.lower(), pool_token1=token1.lower(), metadata=meta_rev)
            else:
                ea = eb = None
        elif protocol == "syncswap":
            from protocols import SyncSwapAdapter, PoolRef
            dex_cfg = next(d for d in get_chain().dexes if d.name == dex_name)
            adapter = SyncSwapAdapter(w3, dex_cfg)
            ref = PoolRef(pool_addr, protocol, dex_name, token0, token1, fee_bps, metadata or {})
            state = adapter.load_pool(ref)
            if state:
                tvl = estimate_tvl_v2(state["token0"], state["token1"], state["r0"], state["r1"], dex=dex_name)
                ea = PoolEdge(pool_addr, dex_name, state["token0"], state["token1"], state["r0"], state["r1"], fee_bps, 2, tvl_usd=tvl, protocol=protocol, pool_token0=state["token0"], pool_token1=state["token1"])
                eb = PoolEdge(pool_addr, dex_name, state["token1"], state["token0"], state["r1"], state["r0"], fee_bps, 2, tvl_usd=tvl, protocol=protocol, pool_token0=state["token0"], pool_token1=state["token1"])
            else:
                ea = eb = None
        elif version == 2:
            ea, eb = _load_v2_pair(w3, pool_addr, dex_name, fee_bps, token0=token0, token1=token1)
        else:
            ea, eb = _load_v3_pool(w3, pool_addr, fee_bps, dex_name, token0=token0, token1=token1)

        if not ea or not eb:
            return

        if protocol:
            ea.protocol = eb.protocol = protocol
        if metadata:
            ea.metadata.update(metadata); eb.metadata.update(metadata)

        # GeckoTerminal is the authoritative external TVL source for dead-pool
        # filtering. On-chain reserve-derived TVL remains a fallback only.
        try:
            authoritative_tvl = await asyncio.get_running_loop().run_in_executor(
                None, gecko.pool_tvl_usd, CHAIN_ID, pool_addr, None
            )
        except Exception:
            authoritative_tvl = None
        if authoritative_tvl is not None:
            ea.tvl_usd = eb.tvl_usd = float(authoritative_tvl)
            now = time.time()
            ea.metadata["tvl_source"] = eb.metadata["tvl_source"] = "geckoterminal"
            ea.metadata["tvl_updated_at"] = eb.metadata["tvl_updated_at"] = now
        else:
            ea.metadata.setdefault("tvl_source", "estimated_onchain")
            eb.metadata.setdefault("tvl_source", "estimated_onchain")

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

        _add_new_discovery(graph, cache, pool_addr, ea, eb, logger_obj=logger_obj, is_new_pool=not existing_before)

        related_pools = gecko.related_pools(CHAIN_ID, pool_addr) if expand_related else []

        # Register all pools in the related set, but only if they aren't already in the graph
        for p in related_pools:
            await register_new_pool(
                w3, graph, cache, graph_client,
                p['address'], p['dex'], p['version'], p['fee'],
                logger_obj=logger_obj
            )


def _add_new_discovery(graph, cache, pool_addr, ea, eb, logger_obj=None, is_new_pool: bool = True):
    """Add new discovered pool to graph and persistent cache."""
    # if cache and cache._pools.get(pool_addr.lower()):
    #     # Update existing pool in cache
    #     cache.update_reserves(pool_addr.lower(), r0=ea.reserve_in, r1=ea.reserve_out, sqrt_price_x96=ea.sqrt_price_x96, liquidity=ea.liquidity)
    #     return
    
    is_pending = getattr(ea, "is_pending", False)
    
    if is_pending:
        log.info(f"Pool {pool_addr[:10]}... added to Waiting Room (TVL unknown)")
    else:
        graph.add_or_update_pool(ea, eb)
        if is_new_pool:
            log.research(f"NEW POOL DISCOVERED: {pool_addr[:10]}... | {ea.token_in[:6]}/{ea.token_out[:6]} | TVL: ${ea.tvl_usd:,.0f}")
    
    if logger_obj and not is_pending and is_new_pool:
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
                "detector": "registry_discovery",
                "arb_kind": "new_pool",
                "validation_status": "discovered",
                "execution_simulated": False,
                "execution_ready": False,
            },
        )
        try:
            logger_obj.log(opp)
        except Exception as e:
            log.warning(f"Failed to log NEW_POOL to OppLogger: {e}")

    if cache:
        existing = cache.get_pool(pool_addr.lower()) or {}
        pair_meta = list((existing.get("metadata") or {}).get("pairs", [])) if ea.protocol == "curve" else []
        if ea.protocol == "curve":
            pair_meta.append({**(ea.metadata or {}), "token0": ea.pool_token0 or ea.token_in, "token1": ea.pool_token1 or ea.token_out, "r0": ea.reserve_in, "r1": ea.reserve_out, "tvl_usd": ea.tvl_usd})
        cache.set_pool(pool_addr.lower(), {
            "token0": ea.pool_token0 or ea.token_in, "token1": ea.pool_token1 or ea.token_out,
            "dex": ea.dex_name, "protocol": ea.protocol, "version": ea.version,
            "fee_bps": ea.fee_bps, "r0": ea.reserve_in,
            "r1": ea.reserve_out, "sqrt_price_x96": ea.sqrt_price_x96,
            "liquidity": ea.liquidity, "last_updated": time.time(),
            "tvl_usd": ea.tvl_usd,
            "is_pending": is_pending,
            "metadata": ({**(ea.metadata or {}), "pairs": pair_meta} if ea.protocol == "curve" else ea.metadata),
        })
        cache.save()


