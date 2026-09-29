"""
Enhanced Main Module
================
Main orchestrator for the DeFi Research Tool.
"""
from __future__ import annotations

import asyncio
import signal
import threading
import time
from contextlib import suppress

# ── Logging must be set up before any other import that uses get_logger() ──
from log import setup_logging, get_logger
setup_logging()
log = get_logger("main")

from web3 import Web3
from web3 import exceptions as w3_exceptions

from config import (
    CHAIN_ID, NATIVE_PRICE_USD, EXECUTE_ONCHAIN, MIN_PROFIT_USD, get_chain, get_rpc_configs
)
from utils.rpc_manager import MultiRPCManager
from graph import TokenGraph
from executor import ArbExecutor
from logger import OppLogger
from tracker import OpportunityTracker
from registry import bootstrap_graph
from cache import PoolCache
from crawler import PoolCrawler
from prices import PriceOracle
from tvl import set_oracle, init_prices, set_graph_ref
from detectors.cycles import CycleDetector, SharpeDetector
from detectors.specialized import NewPoolDetector, CrossProtocolDetector, StableImbalanceDetector, LiquidationScanner
from detectors.advanced import SplitRouteDetector
from mempool.pipeline import MempoolPipeline
from execution_router import ExecutionRouter, OppSource, create_router
from opportunity_engine import OpportunityEngine
from mempool.decoder import SwapDecoder, build_router_map, get_router_addresses
from block_information import BlockInformationCollector, ProtocolStateSynchronizer

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
#
# NOTE ON TRANSPORT: this process talks to the chain exclusively over HTTP
# (via MultiRPCManager's failover provider, built in main()). There is no
# sync-Web3 WebSocket subscription mode here — web3.py v7's `subscribe()` /
# persistent-connection API requires `AsyncWeb3`, which this codebase does
# not use, and a sync `Web3(WebSocketProvider(...))` cannot receive push
# notifications on either v6 or v7. Real-time observation (new pending txs)
# is handled separately by a raw `websockets` connection in
# mempool/watcher.py, which is unaffected by this. New-block detection uses
# HTTP polling (run_polling, below) with a short poll interval.


def build_detectors(w3, graph, logger, cache):
    """Build all detector instances."""
    return [
        CycleDetector(w3, graph, logger, cache, CHAIN_ID),
        # SharpeDetector(w3, graph, logger, cache, CHAIN_ID),
        NewPoolDetector(w3, graph, logger, cache, CHAIN_ID),
        CrossProtocolDetector(w3, graph, logger, cache, CHAIN_ID),
        StableImbalanceDetector(w3, graph, logger, cache, CHAIN_ID),
        SplitRouteDetector(w3, graph, logger, cache, CHAIN_ID),
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
    opportunity_engine: OpportunityEngine | None = None,
    crawler: PoolCrawler | None = None,
) -> bytes:
    """Process a single block and return its hash."""
    block_started = time.perf_counter()
    log.info(f"--- Block {block_number} ---")
    if router:
        # The router's per-block execution cap (MAX_EXECUTIONS) is only
        # meaningful if it resets every block; process_block() is the
        # canonical "new block observed" hook for the whole live pipeline.
        router.reset_block_counter()
    updated_pools: set[str] = set()
    filled_pools:  dict[str, str] = {}
    current_hash = None
    ctx = None

    try:
        # Phase 19: collect all protocol state-change events through one
        # canonical information layer. The collector gates generic event
        # signatures against the graph's known pools and protocol labels.
        collector = BlockInformationCollector(w3, CHAIN_ID, graph)
        ctx = collector.collect(block_number)
        current_hash = bytes.fromhex(ctx.block_hash[2:]) if ctx.block_hash.startswith("0x") else ctx.block_hash

        # Preserve the existing deep-reorg check, but use the canonical parent
        # hash rather than a second ad-hoc header read.
        if last_block_hash and ctx.parent_hash:
            parent_bytes = bytes.fromhex(ctx.parent_hash[2:]) if ctx.parent_hash.startswith("0x") else ctx.parent_hash
            if parent_bytes != last_block_hash:
                log.warning("⚠️ DEEP REORG DETECTED at block %s; state will be rebuilt from canonical logs.", block_number)

        updated_pools = ctx.touched_pools
        for change in ctx.state_changes:
            if change.is_trade and change.transaction_hash:
                filled_pools[change.pool_address] = change.transaction_hash

        refreshed: set[str] = set()
        if updated_pools:
            synchronizer = ProtocolStateSynchronizer(w3, graph, cache)
            refreshed = synchronizer.refresh(ctx)
        trade_count = sum(1 for c in ctx.state_changes if c.is_trade and not c.removed)
        log.info("[BLOCK] block=%s hash=%s raw_logs=%d tracked_events=%d ignored=%d discovery_hints=%d protocols=%s touched=%d refreshed=%d trades=%d",
                 block_number, ctx.block_hash[:10], ctx.raw_log_count, len(ctx.state_changes),
                 ctx.ignored_log_count, len(ctx.discovery_events),
                 sorted({c.protocol for c in ctx.state_changes}),
                 len(updated_pools), len(refreshed), trade_count)
        if ctx.discovery_events:
            added = crawler.enqueue_event_discovery(ctx.discovery_events) if crawler is not None else 0
            log.debug("[BLOCK] queued %d/%d unknown-pool discovery hints", added, len(ctx.discovery_events))
    except Exception as e:
        log.warning(f"Block information/state sync error block {block_number}: {e}")
        # Do not run block-based detectors against an unknown/stale state view.
        # Mempool outcome handling still advances because it is independently
        # keyed by the canonical block number.
        mempool.on_block(block_number)
        if sync_only:
            return current_hash
        if router:
            router.process_results()
        tracker.process_block(block_number, [], filled_pools=filled_pools)
        return current_hash

    # The OpportunityEngine schedules detectors according to their thesis:
    # state-dependent detectors require touched pools, while independent
    # detectors run on their own bounded cadence. This prevents a cold block
    # from triggering full-graph scans on every block.

    # Resolve pending tx outcomes
    mempool.on_block(block_number)

    if sync_only:
        return current_hash

    # ═══════════════════════════════════════════════════════════════════════════
    # Unified detector -> validation -> economics -> ranking pipeline
    # ═══════════════════════════════════════════════════════════════════════════
    all_opps = []
    if opportunity_engine is not None:
        result = opportunity_engine.process(block_number, touched_pools=updated_pools, block_context=ctx)
        all_opps = list(result.ready)
        if result.rejected:
            log.debug(f"[PIPELINE] block={block_number} rejected {len(result.rejected)} candidates")

        if router and not sync_only:
            source_map = {
                "cycles": OppSource.DETECTOR_CYCLES,
                "new_pool": OppSource.DETECTOR_NEW_POOL,
                "cross_protocol": OppSource.DETECTOR_CROSS_PROTO,
                "cross_proto": OppSource.DETECTOR_CROSS_PROTO,
                "stable_imbalance": OppSource.DETECTOR_CROSS_PROTO,
                "split_route": OppSource.DETECTOR_CROSS_PROTO,
                "liquidation": OppSource.DETECTOR_LIQUIDATION,
                "mempool_backrun": OppSource.BACKRUN,
            }
            for opp in all_opps:
                source = source_map.get(str(opp.metadata.get("detector", "")), OppSource.DETECTOR_CYCLES)
                router.submit_opportunity(opp, source)

    # Process any completed async executions from router
    if router:
        router.process_results()
    
    tracker.process_block(block_number, all_opps, filled_pools=filled_pools)
    log.info("[BLOCK] complete block=%s elapsed_ms=%.1f opportunities_ready=%d",
             block_number, (time.perf_counter() - block_started) * 1000.0, len(all_opps))

    # Non-streaming opportunities remain tracked only. The legacy ArbExec
    # multiCall path is non-atomic and must never be used as a production
    # batch executor. Atomic batching belongs to the dedicated atomic executor.
    
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
            if not t.done():
                t.cancel()
        await asyncio.gather(*all_bg, return_exceptions=True)

        # Close async clients while this loop is still alive. Previously the
        # loop was closed first and httpx/aio clients attempted their final
        # connection close against a dead loop, producing
        # RuntimeError('Event loop is closed').
        with suppress(Exception):
            await crawler.stop()
        with suppress(Exception):
            await mempool.stop()
        log.info("Background async loop: tasks and async clients closed.")

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
    """Canonical catch-up using the same protocol event/state semantics as live blocks."""
    cached_block = cache.get_last_synced_block()
    if cached_block <= 0 or current_tip <= cached_block:
        cache.set_last_synced_block(current_tip)
        cache.save()
        log.info("Starting at chain tip %s", current_tip)
        return current_tip

    start = cached_block + 1
    if current_tip - cached_block > 1000:
        log.info("Canonical catch-up: %s blocks (%s -> %s) — this may take a while…", current_tip - cached_block, start, current_tip)
        return current_tip  # Skip catch-up for large gaps; rely on mempool backrun detection instead
    log.info("Canonical catch-up: %s blocks (%s -> %s)", current_tip - cached_block, start, current_tip)
    collector = BlockInformationCollector(w3, CHAIN_ID, graph)
    contexts = []
    chunk_size = 100
    while start <= current_tip:
        end = min(current_tip, start + chunk_size - 1)
        try:
            contexts.extend(collector.collect_range(start, end, chunk_size=chunk_size))
            start = end + 1
            if chunk_size < 500:
                chunk_size = min(500, chunk_size * 2)
        except Exception as exc:
            if chunk_size > 1:
                chunk_size = max(1, chunk_size // 2)
                log.warning("Catch-up log range rejected; shrinking chunk to %s: %s", chunk_size, exc)
            else:
                raise

    touched = set()
    # refreshed = 0

    for ctx in contexts:
        touched.update(ctx.touched_pools)
        
    if touched:
        sync = ProtocolStateSynchronizer(w3, graph, cache)
        refreshed = sync.refresh_by_addresses(touched, contexts[-1] if contexts else None)
    #     tip_ctx = collector.collect(current_tip)
    #     # Refresh the full catch-up touched set against the canonical tip.
        
    #     for address in touched:
    #         try:
    #             sync._refresh_one(address, tip_ctx)
    #             refreshed += 1
    #         except Exception as exc:
    #             graph.mark_pool_state(address, status="UNCERTAIN", source=f"catchup_error:{type(exc).__name__}")
    #             log.warning("Catch-up state refresh failed pool=%s: %s", address, exc)
        log.info("Canonical catch-up: %d blocks, %d events, %d touched pools, %d refreshed",
                    current_tip - cached_block, sum(len(c.state_changes) for c in contexts), len(touched), len(refreshed))
    else:
        log.info("Canonical catch-up: %d blocks, no tracked protocol state changes", current_tip - cached_block)

    cache.set_last_synced_block(current_tip)
    cache.save()
    return current_tip

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
    router: ExecutionRouter | None = None,
    opportunity_engine: OpportunityEngine | None = None
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
                        last_block_hash = process_block(w3, bn, graph, detectors, tracker, mempool, cache, router=router, sync_only=not is_tip, last_block_hash=last_block_hash, opportunity_engine=opportunity_engine, crawler=crawler)
                        
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
    opportunity_engine = OpportunityEngine(w3, CHAIN_ID, detectors=detectors, graph=graph)
    log.info("Canonical OpportunityEngine ready")

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

    mempool = MempoolPipeline(w3, graph, tracker, cache=cache, router=router, opportunity_engine=opportunity_engine)
    log.info("Mempool pipeline ready")

    # crawler_w3 = rpc_manager.get_failover_web3(ws=False)
    # Pass oracle to crawler for DEX price propagation
    crawler    = PoolCrawler(w3, graph, cache, price_oracle=oracle)
    log.info("Pool crawler ready (using HTTP via rpc_manager with failover)")

    # ── Run — ALL exit paths flow into the finally block ─────────────────────
    # HTTP polling is the sole live-block-detection transport (see the note by
    # build_detectors() above for why there is no sync-Web3 WS streaming mode).
    # `opportunity_engine` MUST be passed here: process_block() only runs the
    # detector pipeline when it isn't None. (Previously this call omitted it,
    # which silently disabled all block-driven detection — cycles, new-pool,
    # cross-protocol, stable-imbalance, split-route, and liquidation — while
    # only the independently-wired mempool backrun path kept working.)
    try:
        run_polling(w3, graph, detectors, tracker,
                    mempool, cache, crawler, oracle,
                    router=router, opportunity_engine=opportunity_engine)

    except KeyboardInterrupt:
        _request_shutdown()

    finally:
        # ── Guaranteed cleanup — runs even on unhandled exceptions ─────────
        _shutdown(cache, logger_obj, crawler, mempool, router=router)


if __name__ == "__main__":
    main()
