"""
Enhanced Main Module
================
Main orchestrator for the DeFi Research Tool.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import threading
import time
from contextlib import suppress

# ── Logging must be set up before any other import that uses get_logger() ──
from log import setup_logging, get_logger
setup_logging()
log = get_logger("main")

from web3 import Web3
from web3 import exceptions as w3_exceptions
from web3.middleware import geth_poa_middleware

from config import (
    RPC_WS, RPC_HTTP, CHAIN_ID, NATIVE_PRICE_USD, EXECUTE_ONCHAIN, MIN_PROFIT_USD,
      get_log_rpc_config, get_chain, get_ws_rpc, get_primary_rpc, get_rpc_configs
)
from utils.rpc_manager import MultiRPCManager, BlockPoller
from graph import TokenGraph
from executor import ArbExecutor
from logger import OppLogger
from tracker import OpportunityTracker
from registry import bootstrap_graph, update_pool_reserves
from cache import PoolCache
from crawler import PoolCrawler
from prices import PriceOracle
from tvl import set_oracle, init_prices, set_graph_ref
from detectors.cycles import CycleDetector
from detectors.specialized import NewPoolDetector, CrossProtocolDetector, LiquidationScanner
from mempool.pipeline import MempoolPipeline
from execution_router import ExecutionRouter, OppSource, create_router
from mempool.decoder import SwapDecoder, build_router_map, get_router_addresses
from utils.multicall3 import multicall3_bulk_refresh, MULTICALL_CHUNK

# Event topics
SYNC_TOPIC    = Web3.keccak(text="Sync(uint112,uint112)").hex()
SWAP_V3_TOPIC = Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
MINT_V3_TOPIC = Web3.keccak(text="Mint(address,address,int24,int24,uint128,uint256,uint256)").hex()
BURN_V3_TOPIC = Web3.keccak(text="Burn(address,int24,int24,uint128,uint256,uint256)").hex()

# ── Global shutdown signal ────────────────────────────────────────────────────
_SHUTDOWN = threading.Event()
_BG_DONE = threading.Event()  # Signals background thread cleanup completion

def _request_shutdown(sig=None, frame=None):
    """Signal-safe shutdown trigger. Works for SIGINT, SIGTERM, and Windows."""
    if not _SHUTDOWN.is_set():
        log.info(f"Shutdown requested (signal={sig}) — draining and stopping…")
        _SHUTDOWN.set()


# Register OS-level signals
signal.signal(signal.SIGTERM, _request_shutdown)
signal.signal(signal.SIGINT, _request_shutdown)
with suppress(AttributeError):          # SIGBREAK is Windows-only
    signal.signal(signal.SIGBREAK, _request_shutdown)


# ── Connection helpers ────────────────────────────────────────────────────────

def build_w3() -> Web3:
    """Connect via WebSocket or HTTP."""
    if RPC_WS:
        try:
            w3 = Web3(Web3.WebsocketProvider(RPC_WS, websocket_timeout=60))
            if w3.is_connected():
                log.info(f"Connected via WebSocket: {RPC_WS[:50]}...")
                return _apply_middleware(w3)
        except Exception as e:
            log.warning(f"WebSocket failed ({e}), trying HTTP...")

    if RPC_HTTP:
        w3 = Web3(Web3.HTTPProvider(RPC_HTTP))
        if w3.is_connected():
            log.info(f"Connected via HTTP: {RPC_HTTP[:50]}...")
            return _apply_middleware(w3)

    raise ConnectionError("Could not connect to RPC. Check RPC_WS / RPC_HTTP in .env")


def _apply_middleware(w3: Web3) -> Web3:
    """Apply PoA middleware for supported chains."""
    chain = get_chain()
    if chain.chain_id in (56, 137, 8453, 42161, 324):
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    return w3


def build_detectors(w3, graph, logger, cache):
    """Build all detector instances."""
    return [
        CycleDetector(w3, graph, logger, cache, CHAIN_ID),
        NewPoolDetector(w3, graph, logger, cache, CHAIN_ID),
        CrossProtocolDetector(w3, graph, logger, cache, CHAIN_ID),
        LiquidationScanner(w3, graph, logger, cache, CHAIN_ID),
    ]


# ── Guaranteed cleanup ────────────────────────────────────────────────────────

async def _shutdown_async(cache: PoolCache, logger_obj: OppLogger,
                        crawler: PoolCrawler, mempool: MempoolPipeline) -> None:
    """
    Async part of shutdown.
    """
    _SHUTDOWN.set()
    log.info("Shutdown: stopping crawler and mempool pipeline…")
    
    # Run stops concurrently
    stop_tasks = []
    with suppress(Exception):
        stop_tasks.append(crawler.stop())
    with suppress(Exception):
        stop_tasks.append(mempool.stop())
        
    if stop_tasks:
        await asyncio.gather(*stop_tasks, return_exceptions=True)

def _shutdown(cache: PoolCache, logger_obj: OppLogger,
              crawler: PoolCrawler, mempool: MempoolPipeline,
              router: ExecutionRouter | None = None) -> None:
    """
    Called from the finally block in main().
    Handles both sync (polling) and async (WebSocket) shutdown gracefully.
    """
    # First, try async cleanup with proper loop handling
    try:
        # Check if we're in an async context with a running loop
        try:
            loop = asyncio.get_running_loop()
            # We're being called from within an async context
            # Create a new loop for shutdown if current one is closing
            if loop.is_closing():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    asyncio.wait_for(
                        _shutdown_async(cache, logger_obj, crawler, mempool),
                        timeout=10.0
                    )
                )
            else:
                # Current loop is running - use it with timeout
                loop.run_until_complete(
                    asyncio.wait_for(
                        _shutdown_async(cache, logger_obj, crawler, mempool),
                        timeout=10.0
                    )
                )
        except RuntimeError:
            # No running loop - create new one for shutdown
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    asyncio.wait_for(
                        _shutdown_async(cache, logger_obj, crawler, mempool),
                        timeout=10.0
                    )
                )
            finally:
                loop.close()
    except Exception as e:
        log.warning(f"Async shutdown failed ({e}) — falling back to sync cleanup")
        # Fallback: sync cleanup
        with suppress(Exception): crawler.stop()
        with suppress(Exception): mempool.stop()

    log.info("Shutdown: stopping execution router…")
    with suppress(Exception):
        if router:
            router.shutdown(timeout=10.0)

    log.info("Shutdown: flushing opportunity log file handles…")
    with suppress(Exception):
        logger_obj.close()

    log.info("Shutdown: saving pool cache…")
    with suppress(Exception):
        cache.save()

    log.info("Shutdown complete ✓")


# ── Block processing ──────────────────────────────────────────────────────────

def process_block(
    w3: Web3,
    block_number: int,
    graph: TokenGraph,
    detectors: list,
    tracker: OpportunityTracker,
    mempool: MempoolPipeline,
    cache: PoolCache,
    router: ExecutionRouter | None = None,
    sync_only: bool = False,
    last_block_hash: bytes = None,
) -> bytes:
    """Process a single block and return its hash."""
    log.info(f"--- Block {block_number} ---")
    updated_pools: set[str] = set()
    filled_pools:  dict[str, str] = {}
    current_hash = None

    try:
        # Deep Reorg Check (Spec 2.3)
        header = w3.eth.get_block(block_number)
        current_hash = getattr(header, "hash", header.get("hash"))
        parent_hash = getattr(header, "parentHash", header.get("parentHash"))
        
        if last_block_hash and parent_hash and parent_hash != last_block_hash:
            log.warning(f"⚠️ DEEP REORG DETECTED at block {block_number}.")
            expected_str = last_block_hash.hex()[:10] if isinstance(last_block_hash, bytes) else str(last_block_hash)[:10]
            actual_str   = parent_hash.hex()[:10] if isinstance(parent_hash, bytes) else str(parent_hash)[:10]
            log.warning(f"Expected parent: {expected_str} | Actual parent: {actual_str}")
            log.warning("Initiating Reorg Rescue via Multicall3 Quick-Sync...")
            
            # Fetch touched pools from the new canonical chain's recent history
            reorg_logs = w3.eth.get_logs({
                "fromBlock": max(0, block_number - 16),
                "toBlock":   block_number - 1,
                "topics":    [[SYNC_TOPIC, SWAP_V3_TOPIC, MINT_V3_TOPIC, BURN_V3_TOPIC]],
            })
            reorg_pools = {entry["address"].lower() for entry in reorg_logs}
            if reorg_pools:
                multicall3_bulk_refresh(w3, graph, cache, reorg_pools, chunk_size=MULTICALL_CHUNK)
                log.info(f"Reorg Rescue Complete: {len(reorg_pools)} pools mathematically synchronized to canonical chain.")

        # Normal processing
        logs = w3.eth.get_logs({
            "fromBlock": block_number,
            "toBlock":   block_number,
            "topics":    [[SYNC_TOPIC, SWAP_V3_TOPIC, MINT_V3_TOPIC, BURN_V3_TOPIC]],
        })
        for entry in logs:
            pool_addr = entry["address"].lower()
            tx_hash   = (entry["transactionHash"].hex()
                         if hasattr(entry["transactionHash"], "hex")
                         else str(entry["transactionHash"]))
            filled_pools[pool_addr] = tx_hash
            updated_pools.add(pool_addr)

        # Bulk refresh all touched pools (Spec 2.5)
        if updated_pools:
            # Only refresh pools we currently track in the graph
            known_pools = set(graph._pool_index.keys())
            tracked_touched = updated_pools.intersection(known_pools)
            
            if tracked_touched:
                multicall3_bulk_refresh(w3, graph, cache, tracked_touched, chunk_size=MULTICALL_CHUNK)
            
            # Any touched pools that are NOT in graph? 
            # They might be new discoveries handled by Crawler later, 
            # or filtered pools. We ignore them for real-time reserve updates.
    except Exception as e:
        log.warning(f"Log fetch error block {block_number}: {e}")

    # MEV OPTIMIZATION: Early exit if no pools changed (nothing to detect)
    if not updated_pools and not filled_pools:
        return current_hash

    # Resolve pending tx outcomes
    mempool.on_block(block_number)

    if sync_only:
        return current_hash

    # ═══════════════════════════════════════════════════════════════════════════
    # Run detectors - opportunities routed through ExecutionRouter
    # ═══════════════════════════════════════════════════════════════════════════
    all_opps = []
    if updated_pools:  # Only run detection if there was actual volatility
        for detector in detectors:
            if _SHUTDOWN.is_set():
                break
            try:
                if hasattr(detector, "on_block") and detector.name == "cycles":
                    opps = detector.on_block(block_number, touched_pools=updated_pools)  
                else: 
                    opps = detector.on_block(block_number)
                # Route high-value opportunities through execution router
                if router and opps and len(opps) == 1:
                    source_map = {
                        "cycles": OppSource.DETECTOR_CYCLES,
                        "new_pool": OppSource.DETECTOR_NEW_POOL,
                        "cross_proto": OppSource.DETECTOR_CROSS_PROTO,
                        "liquidation": OppSource.DETECTOR_LIQUIDATION,
                    }
                    source = source_map.get(detector.name, OppSource.DETECTOR_CYCLES)
                    
                    for opp in opps:
                        # if opp.estimated_profit_usd >= MIN_PROFIT_USD * 2:
                        router.submit_opportunity(opp, source)
                
                all_opps.extend(opps)
            except Exception as e:
                log.error(f"Detector '{detector.name}' error on block {block_number}: {e}",
                          exc_info=True)
    
    # Process any completed async executions from router
    if router:
        router.process_results()
    
    tracker.process_block(block_number, all_opps, filled_pools=filled_pools)

    # Batch execution for remaining opportunities (if streaming didn't get them)
    if router and all_opps and not sync_only:
        router.submit_batch_opportunities(all_opps, OppSource.DETECTOR_CYCLES, block_number)
    
    return current_hash


# ── Background async tasks ────────────────────────────────────────────────────

def _run_background_async(
    crawler: PoolCrawler,
    mempool: MempoolPipeline,
    oracle: PriceOracle,
    graph: TokenGraph,
) -> threading.Thread:
    """
    Spin up crawler, mempool-watcher, and oracle refresh in a dedicated asyncio
    event loop running on a NON-daemon thread.  Returns the thread so the caller
    can .join() it during shutdown.
    """
    loop = asyncio.new_event_loop()

    async def _main_coro():
        c_tasks = await crawler.start()
        m_tasks = await mempool.start()
        o_task  = oracle.start_background_refresh(graph, interval_seconds=300)
        all_bg  = c_tasks + m_tasks + [o_task]

        # Wait for the shutdown signal without blocking the event loop
        await loop.run_in_executor(None, _SHUTDOWN.wait)

        log.info("Background async loop: cancelling tasks…")
        for t in all_bg:
            t.cancel()
        await asyncio.gather(*all_bg, return_exceptions=True)
        log.info("Background async loop: all tasks cancelled.")

    def _thread_target():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_main_coro())
        finally:
            loop.close()
            _BG_DONE.set()  # Signal that background cleanup is complete

    t = threading.Thread(target=_thread_target, name="bg_async_loop", daemon=False)
    t.start()
    return t


# ── Targeted Catchup ──────────────────────────────────────────────────────────
def _perform_fast_catchup(w3: "Web3", graph: "TokenGraph", cache: "PoolCache", current_tip: int) -> int:
    """
    PRODUCTION SPEC 2.5: Targeted Catch-Up & Multicall3 Integration
    Fast-forwards the graph mathematically to the real-time block tip.
    Returns the block from which normal operation should resume.
    """
    cached_block = cache.get_last_synced_block()
    
    if cached_block > 0 and (current_tip - cached_block) < 5000:
        last_block = cached_block
        log.info(f"Resuming from cached block {last_block} (gap: {current_tip - last_block})")
        
        # --- INITIAL FAST CATCHUP ---
        if current_tip > last_block:
            log.info(f"Fast-forwarding state for {current_tip - last_block} missed blocks...")
            try:
                # 1. Fetch all sync logs in batches, dynamically adjusting size for strict RPCs
                chunk_size = 2000
                touched_pools = set()
                chunk_start = last_block + 1
                
                while chunk_start <= current_tip:
                    chunk_end = min(chunk_start + chunk_size - 1, current_tip)
                    try:
                        logs_chunk = w3.eth.get_logs({
                            "fromBlock": chunk_start,
                            "toBlock": chunk_end,
                            "topics": [[SYNC_TOPIC, SWAP_V3_TOPIC, MINT_V3_TOPIC, BURN_V3_TOPIC]],
                        })
                        for entry in logs_chunk:
                            touched_pools.add(entry["address"].lower())
                        chunk_start = chunk_end + 1
                        
                        # Slowly scale back up if a small chunk succeeded
                        if chunk_size < 1000:
                            chunk_size = min(2000, chunk_size * 2)
                    except Exception as e:
                        if chunk_size > 1:
                            # Strict RPCs often hardcap at 10 to 100 blocks; aggressively scale down
                            new_size = 10 if chunk_size > 10 else max(1, chunk_size // 2)
                            log.warning(f"Batch {chunk_start}-{chunk_end} rejected ({e}). Shrinking chunk size: {chunk_size} -> {new_size}")
                            chunk_size = new_size
                        else:
                            raise Exception(f"RPC completely rejected getLogs even for 1 block: {e}")
                
                # 2. Filter mathematically isolated pools to exclusively those in our TokenGraph
                known_pools = set(graph._pool_index.keys())
                tracked_touched = touched_pools.intersection(known_pools)
                
                log.info(f"Filtered {len(touched_pools)} raw events down to {len(tracked_touched)} tracked pools for update.")
                
                # 3. Update their reserves concurrently to eliminate I/O bottleneck
                if tracked_touched:
                    multicall3_bulk_refresh(w3, graph, cache, tracked_touched, chunk_size=MULTICALL_CHUNK)
                        
                last_block = current_tip
                cache.set_last_synced_block(last_block)
                cache.save()
                log.info("Fast-forward complete. Graph is now natively at the chain tip.")
                
            except Exception as e:
                log.warning(f"Fast-forward batch failed ({e}). Reseting to tip...")
                last_block = current_tip
                cache.set_last_synced_block(last_block)
    else:
        last_block = current_tip
        cache.set_last_synced_block(last_block)
        log.info(f"Starting fresh sync from block {last_block}")
        
    return last_block

# ── HTTP polling mode ─────────────────────────────────────────────────────────

def run_polling(
    w3: Web3,
    graph: TokenGraph,
    detectors: list,
    tracker: OpportunityTracker,
    mempool: MempoolPipeline,
    cache: PoolCache,
    crawler: PoolCrawler,
    oracle: PriceOracle,
    router: ExecutionRouter | None = None
) -> None:
    """HTTP polling mode — blocks until _SHUTDOWN is set."""
    # Production-Grade Catchup (Spec 2.5)
    current_tip = w3.eth.block_number
    last_block = _perform_fast_catchup(w3, graph, cache, current_tip)

    # START background tasks AFTER catchup so they operate on an updated graph
    # and don't flood logs with outdated mempool cross-checks.
    bg_thread = _run_background_async(crawler, mempool, oracle, graph)

    last_block_hash = None
    try:
        while not _SHUTDOWN.is_set():
            try:
                current = w3.eth.block_number
                if current > last_block:
                    total_to_process = current - last_block
                    if total_to_process > 1:
                        log.info(f"Catching up: processing {total_to_process} blocks ({last_block+1} -> {current})")
                        # Reset hash tracker on massive skips to prevent false-positive reorg reports
                        last_block_hash = None
                    
                    for bn in range(last_block + 1, current + 1):
                        if _SHUTDOWN.is_set():
                            break
                        # Only the most recent block gets full detection
                        is_tip = (bn == current)
                        last_block_hash = process_block(w3, bn, graph, detectors, tracker, mempool, cache, router=router, sync_only=not is_tip, last_block_hash=last_block_hash)
                        
                        # Update persistent checkpoint
                        cache.set_last_synced_block(bn)
                        if bn % 10 == 0:
                            cache.save() # Atomic save every 10 blocks
                            
                    last_block = current

                # _SHUTDOWN.wait(timeout=2) replaces time.sleep(2) so CTRL-C
                # wakes the loop immediately instead of waiting up to 2 s.
                # MEV OPTIMIZATION: Reduced from 2s to 0.2s for faster block detection
                _SHUTDOWN.wait(timeout=0.2)

            except w3_exceptions.BadResponseFormat as e:
                log.error(f"Bad response format: {e}")
                continue

            except KeyboardInterrupt:
                _request_shutdown()

    except KeyboardInterrupt:
        _request_shutdown()

    log.info("Polling loop exited — waiting for background thread (up to 15 s)…")
    # Wait for background thread to signal completion (with timeout fallback)
    _BG_DONE.wait(timeout=15)
    if not _BG_DONE.is_set():
        log.warning("Background thread did not finish in time — continuing shutdown.")


# ── WebSocket mode ────────────────────────────────────────────────────────────

async def run_ws(
    w3: Web3,
    graph: TokenGraph,
    detectors: list,
    tracker: OpportunityTracker,
    mempool: MempoolPipeline,
    cache: PoolCache,
    crawler: PoolCrawler,
    oracle: PriceOracle,
    rpc_ws_url: str,
    router: ExecutionRouter | None = None
) -> None:
    """WebSocket subscription mode."""
    crawler_tasks = await crawler.start()
    oracle_task   = oracle.start_background_refresh(graph, interval_seconds=300)
    mempool_tasks = await mempool.start()
    all_tasks     = crawler_tasks + mempool_tasks + [oracle_task]

    subscription = None
    if hasattr(w3.eth, "subscribe"):
        subscription = await w3.eth.subscribe("newHeads")
        log.info("Subscribed to newHeads via WebSocket")
    else:
        log.warning("w3.eth.subscribe not available — falling back to polling.")
        raise AttributeError("subscribe() not natively supported on sync Web3 v6.")

    # PRODUCTION SPEC 2.5: Target Catchup Before Streaming
    current_tip = w3.eth.block_number
    # Run the fast-forward asynchronously if in an event loop to prevent blocking the WS initialization
    try:
        await asyncio.to_thread(_perform_fast_catchup, w3, graph, cache, current_tip)
    except Exception as e:
        log.error(f"Targeted Catch-Up Failed before WebSocket Streaming: {e}")

    last_block_hash = None
    try:
        async for header in subscription:
            if _SHUTDOWN.is_set():
                break
            # Websockets often deliver hex strings for block numbers
            block_number = int(header["number"], 16) if isinstance(header["number"], str) else header["number"]
            last_block_hash = process_block(w3, block_number, graph, detectors, tracker, mempool, cache, router=router, last_block_hash=last_block_hash)
            
            # Update persistent checkpoint
            cache.set_last_synced_block(block_number)
            if block_number % 10 == 0:
                cache.save() # Atomic save every 10 blocks
    finally:
        log.info("WebSocket: cancelling background tasks…")
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    # Startup banner
    chain_name = get_chain().name.upper()
    log.info("=" * 60)
    log.info("  ENHANCED ARB MONITOR — DeFi Research Tool")
    log.info(f"  Chain: {chain_name} (id={CHAIN_ID})")
    log.info("=" * 60)

    rpc_configs = get_rpc_configs()
    rpc_manager = MultiRPCManager.from_config(rpc_configs)

    w3 = rpc_manager.get_failover_web3(ws=False)
    log.info(f"Connected via multi-RPC manager | failover enabled with {len(rpc_manager.providers)} providers")
    log.info(f"Latest block: {w3.eth.block_number}")

    cache = PoolCache(chain_id=CHAIN_ID)
    cache.load()

    chain  = get_chain()
    oracle = PriceOracle(
        chain_id=CHAIN_ID,
        native_price_usd=NATIVE_PRICE_USD,
        wrapped_native=chain.wrapped_native,
        stablecoins=chain.stablecoins,
    )
    set_oracle(oracle)
    init_prices(NATIVE_PRICE_USD, chain.wrapped_native, chain.stablecoins)
    log.info("Price oracle ready")

    graph      = TokenGraph(w3=w3, cache=cache)
    set_graph_ref(graph)
    logger_obj = OppLogger()
    tracker    = OpportunityTracker(logger_obj)

    log.info("Bootstrapping graph...")
    bootstrap_graph(w3, graph, cache)

    detectors = build_detectors(w3, graph, logger_obj, cache)
    log.info(f"{len(detectors)} detectors active: {[d.name for d in detectors]}")

    executor = ArbExecutor(w3, chain_id=CHAIN_ID)

    # Create execution router to centralize opp routing from detectors and mempool
    router = create_router(
        tracker=tracker,
        executor=executor,
        config={
            "EXECUTE_ONCHAIN": EXECUTE_ONCHAIN,
            "MIN_PROFIT_USD": MIN_PROFIT_USD,
            "MAX_EXECUTIONS": 3,
            "STREAMING_MULTIPLIER": 2.0,
        }
    )
    router.start()
    log.info("Execution router ready")

    mempool = MempoolPipeline(w3, graph, tracker, cache=cache, router=router)
    log.info("Mempool pipeline ready")

    # crawler_w3 = rpc_manager.get_failover_web3(ws=False)
    # Pass oracle to crawler for DEX price propagation
    crawler    = PoolCrawler(w3, graph, cache, price_oracle=oracle)
    log.info("Pool crawler ready (using HTTP via rpc_manager with failover)")

    # ── Run — ALL exit paths flow into the finally block ─────────────────────
    try:
        if RPC_WS and isinstance(w3.provider, Web3.WebsocketProvider):
            try:
                asyncio.run(
                    run_ws(w3, graph, detectors, tracker,
                           mempool, cache, crawler, oracle, RPC_WS,
                           router=router)
                )
            except KeyboardInterrupt:
                _request_shutdown()
            except Exception as e:
                log.warning(
                    f"WebSocket mode failed ({type(e).__name__}: {e}), "
                    "falling back to polling..."
                )
                run_polling(w3, graph, detectors, tracker,
                            mempool, cache, crawler, oracle,
                            router=router)
        else:
            run_polling(w3, graph, detectors, tracker,
                        mempool, cache, crawler, oracle,
                        router=router)

    except KeyboardInterrupt:
        _request_shutdown()

    finally:
        # ── Guaranteed cleanup — runs even on unhandled exceptions ─────────
        _shutdown(cache, logger_obj, crawler, mempool, router=router)


if __name__ == "__main__":
    main()
