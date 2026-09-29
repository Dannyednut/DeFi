"""
mempool/pipeline.py — Async mempool processing pipeline.

Consumes decoded txs from MempoolWatcher.queue and:
  1. Decodes calldata via SwapDecoder
  2. Simulates price impact and detects pre-mine opportunities via MempoolSimulator
  3. Registers PendingOpportunity objects with OutcomeResolver
  4. Exposes stats and pending opps for dashboard/reporter

This runs as an asyncio task alongside the block watcher in main.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
import json
from typing import TYPE_CHECKING, Optional
import os

if TYPE_CHECKING:
    from web3 import Web3
    from graph import TokenGraph
    from tracker import OpportunityTracker
    from cache import PoolCache
    from execution_router import ExecutionRouter

from mempool.watcher import MempoolWatcher
from mempool.decoder import SwapDecoder, build_router_map, get_router_addresses
from mempool.simulator import MempoolSimulator, PendingOpportunity
from mempool.outcome import OutcomeResolver
from mempool.engine import MempoolOpportunityEngine, PendingObservation

# from utils.graph_client import GraphClient
from registry import discover_from_swap
from execution_router import OppSource
from config import CHAIN_ID, NATIVE_PRICE_USD, RPC_WS, get_ws_rpc

from log import get_logger
log = get_logger("pipeline")


class MempoolPipeline:
    """
    Top-level mempool pipeline.
    Create one instance, call start() to launch async tasks,
    call on_block() from the main block loop.
    """

    def __init__(self, w3: "Web3", graph: "TokenGraph", tracker: "OpportunityTracker", cache: Optional["PoolCache"] = None, router: Optional["ExecutionRouter"] = None, opportunity_engine=None):
        self._w3      = w3
        self._graph   = graph
        self._tracker = tracker
        self._cache   = cache
        self._router  = router  # ExecutionRouter for routing mempool opps
        self._opportunity_engine = opportunity_engine

        active_chain_id = int(w3.eth.chain_id)
        router_addrs  = get_router_addresses(active_chain_id)
        # Watch every configured protocol router, including protocols whose
        # calldata decoder is not yet implemented (Curve/Balancer/etc.).
        from config import get_chain
        configured_targets = {d.router.lower() for d in get_chain(active_chain_id).dexes if d.router}
        router_addrs |= configured_targets
        router_map    = build_router_map(active_chain_id)
        self._watcher   = MempoolWatcher(
            rpc_ws_url=RPC_WS,
            w3=w3,
            router_addresses=router_addrs,
            max_fetches_per_second=float(os.getenv("MEMPOOL_FETCHES_PER_SECOND", "20")),
            on_hash_callback=self.mark_seen,
            chain_id=active_chain_id,
        )
        # self._graph_client = GraphClient() # Dead dependency (Spec 5.x)
        self._decoder   = SwapDecoder(router_map)
        self._simulator = MempoolSimulator(graph, native_price_usd=NATIVE_PRICE_USD)
        self._resolver  = OutcomeResolver(w3, graph, None)
        self._mev_engine = MempoolOpportunityEngine(int(w3.eth.chain_id))
        # Initialize with the current block to avoid dropping startup transactions
        try:
            self._latest_block = w3.eth.block_number
        except Exception:
            self._latest_block = 0

        # In-memory list of recent pending opportunities for dashboard
        self._recent_pending: list[PendingOpportunity] = []
        self._max_recent = 50

        # Promotion/validation for a single candidate involves real,
        # network-bound RPC calls (discover_from_swap's multicall batches,
        # then OpportunityValidator's eth_call/eth_estimateGas sequence
        # inside process_candidates). _consumer_loop used to await this
        # inline for every candidate before pulling the next pending
        # transaction off the queue -- meaning ANY transaction that even
        # looked promising enough to validate would serialize the ENTIRE
        # mempool pipeline behind that validation's network latency, however
        # long it took, delaying every other pending transaction sitting in
        # the queue (including ones representing their own time-sensitive
        # backrun windows). Tracked here so this work can run as background
        # tasks instead -- see _process_tx/_promote_and_route below -- while
        # still being awaitable for graceful shutdown.
        self._validation_tasks: set[asyncio.Task] = set()

        # Stats
        self._decoded_count   = 0
        self._simulated_count = 0
        self._opps_found      = 0
        self._touched_pools: dict[str, float] = {}  # pool_addr -> timestamp
        
        # GOLD UPGRADE: Public Memory (tx_hash -> timestamp)
        self._seen_hashes: dict[str, float] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> list[asyncio.Task]:
        """Launch watcher + consumer tasks. Must be called from async context."""
        loop = asyncio.get_running_loop()
        self._resolver.set_loop(loop)

        tasks = [
            asyncio.create_task(self._watcher.run(), name="mempool_watcher"),
            asyncio.create_task(self._consumer_loop(),  name="mempool_consumer"),
            asyncio.create_task(self._stats_ticker(), name="mempool_stats"),
        ]
        log.info("Mempool pipeline started")
        return tasks

    async def stop(self) -> None:
        """Signal all sub-components to stop."""
        self._watcher.stop()
        # GraphClient dependency removed - was dead code causing resource leak warnings
        # Let in-flight promotion/validation tasks (see _promote_and_route)
        # finish rather than dropping them silently on shutdown -- they may
        # be mid-way through submitting a real execution.
        if self._validation_tasks:
            await asyncio.gather(*list(self._validation_tasks), return_exceptions=True)

    def on_block(self, block_number: int) -> None:
        """Called from main block loop to resolve pending tx outcomes."""
        self._latest_block = block_number
        self._resolver.on_block(block_number, self._tracker, is_public_fn=self.is_public)

    # ── Stats / read API ──────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        ws = self._watcher.stats
        rs = self._resolver.stats
        return {
            "watcher":  ws,
            "resolver": rs,
            "pipeline": {
                "decoded":   self._decoded_count,
                "simulated": self._simulated_count,
                "opps_found": self._opps_found,
            },
        }

    @property
    def recent_pending(self) -> list[PendingOpportunity]:
        return list(self._recent_pending)

    @property
    def in_flight_count(self) -> int:
        return self._resolver.stats["in_flight"]

    def mark_seen(self, tx_hash: str) -> None:
        """Record a hash seen in the public mempool."""
        self._seen_hashes[tx_hash.lower()] = time.time()

    def is_public(self, tx_hash: str) -> bool:
        """Check if a hash was seen in the public mempool."""
        return tx_hash.lower() in self._seen_hashes

    # ── Consumer loop ─────────────────────────────────────────────────────────

    async def _consumer_loop(self) -> None:
        """
        Continuously consumes from watcher queue.
        Runs decode + simulate synchronously (they are CPU-bound, fast).
        """
        log.info("Consumer loop started")
        
        while self._watcher.queue is None:
            await asyncio.sleep(0.1)
        
        queue = self._watcher.queue

        while True:
            try:
                # Block until a tx arrives (with timeout for clean shutdown)
                tx = await asyncio.wait_for(queue.get(), timeout=5.0)
                await self._process_tx(tx)
                queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Consumer loop error: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    async def _process_tx(self, tx: dict) -> None:
        """Decode → simulate → register outcome (optimized path)."""
        
        # IMPORTANT: distinct tx hashes are never dropped just because calldata looks similar.
        # GOLD UPGRADE: Stalked / Alpha Bot Hit
        if tx.get("_stalked_hit"):
            log.research(f"STALKED | Alpha Bot Activity! hash={tx['hash'][:10]}... | "
                  f"from={tx['from'][:10]}... | to={tx['to'][:10]}...")
            # If it's a direct swap we decode it as usual, if not, we just log and return
            if tx.get("to", "").lower() not in self._watcher._routers:
                return

        # MEV OPTIMIZATION: Direct call instead of run_in_executor
        # Decode calldata - fast CPU operation, no thread switch needed
        swap = self._decoder.decode(tx)
        if not swap:
            return

        self._decoded_count += 1
        
        # GOLD UPGRADE: Dynamic discovery of unknown pools
        # discovery logic (orchestrates RPC/Graph, fully async)
        await discover_from_swap(
            self._w3, self._graph, self._cache, swap, None, self._tracker._logger
        )

        # Track 'heat' for tokens in the path
        now = time.time()
        for i in range(len(swap.token_path) - 1):
             # Finding edges for heat tracking
             edges = self._graph.get_edges_from(swap.token_path[i])
             for e in edges:
                 if e.token_out == swap.token_path[i+1]:
                     pool_addr = e.pool_address.lower()
                     self._touched_pools[pool_addr] = now
                     self._watcher.add_target_addresses({pool_addr})
        # Optional: Print activity for researcher visibility
        if self._decoded_count % 10 == 0:
            log.debug(f"Decoded {self._decoded_count} transactions total")

        # MEV OPTIMIZATION: Direct call instead of run_in_executor
        # Simulate impact and find pre-mine opportunities - fast CPU operation
        pending_opps = self._simulator.simulate(swap)

        if not pending_opps:
            return

        # Filter out tiny noise for research-only items
        final_opps = []
        for opp in pending_opps:
            if opp.opp_type == "RESEARCH" and opp.price_impact_pct <= 1.0:
                continue
            final_opps.append(opp)
            
        if not final_opps:
            log.debug(f"Analyzing {swap.router_name} swap: "
                  f"{swap.token_in[:6]}... -> {swap.token_out[:6]}... | "
                  f"No significant impact.")
            return

        self._simulated_count += 1

        # Estimate current block for registration
        current_block = self._latest_block

        for opp in final_opps:
            self._opps_found += 1

            # Register for outcome tracking
            self._resolver.register(opp, current_block)

            # GOLD UPGRADE: Feed to tracker for lifecycle logging
            # We track VERIFIED arbs AND significant RESEARCH impacts (>2%)
            if opp.opp_type.startswith("VERIFIED_") or (opp.opp_type == "RESEARCH" and opp.price_impact_pct > 2.0):
                # Promotion/validation is network-bound (see the note on
                # self._validation_tasks in __init__) -- run it as a
                # background task rather than awaiting it here, so this
                # consumer loop can immediately go back to consuming the
                # NEXT pending transaction instead of blocking on this one's
                # RPC round-trips. Tracked for graceful shutdown in stop().
                task = asyncio.create_task(self._promote_and_route(opp, current_block))
                self._validation_tasks.add(task)
                task.add_done_callback(self._validation_tasks.discard)

    async def _promote_and_route(self, opp: "PendingOpportunity", current_block: int) -> None:
        """Convert a mempool-detected candidate to a canonical Opportunity,
        validate it against live chain state, and route it for execution.

        Split out of _process_tx so it can run as a background task (see the
        note on self._validation_tasks in __init__) instead of serializing
        the mempool consumer loop behind each candidate's RPC round-trips.
        """
        try:
            tracker_opp = opp.to_opportunity(int(self._w3.eth.chain_id), current_block, graph=self._graph)
            # Mempool reserve math is discovery/shadow evidence only. The
            # same canonical engine used by block detectors must promote it.
            if self._opportunity_engine is not None:
                result = self._opportunity_engine.process_candidates([tracker_opp], current_block)
                promoted = list(result.ready)
            else:
                promoted = []
            self._tracker.process_block(current_block, promoted, filled_pools={})
            if self._router and promoted:
                for ready in promoted:
                    self._router.submit_opportunity(ready, OppSource.BACKRUN, priority=2.0)
        except Exception as e:
            log.error(f"Promotion/validation failed for {opp.opp_type} candidate: {e}", exc_info=True)

            # Add to recent list
            self._recent_pending.append(opp)
            if len(self._recent_pending) > self._max_recent:
                self._recent_pending.pop(0)

            if self._simulated_count % 50 == 0:
                log.info(f"SIMULATOR HEARTBEAT | {self._simulated_count} txs simulated | {self._opps_found} opps found")


            if opp.estimated_profit_usd > 0:
                profit_str = f"${opp.estimated_profit_usd:.4f}"
            elif opp.profit_token_symbol:
                profit_str = f"{opp.profit_in_token:.4g} {opp.profit_token_symbol}"
            else:
                profit_str = "N/A"

            log.research(
                f"PENDING OPP | {opp.opp_type} | "
                f"trigger={opp.trigger_tx_hash[:10]} | "
                f"impact={opp.price_impact_pct:.3f}% | "
                f"est_profit={profit_str}"
            )

    async def _stats_ticker(self) -> None:
        """Periodic heartbeat and Alpha Bot list refresh."""
        refresh_counter = 0
        while True:
            await asyncio.sleep(10)
            ws = self._watcher.stats
            
            # Periodically refresh stalked addresses from logs
            refresh_counter += 1
            if refresh_counter % 6 == 0: # Every 60 seconds
                self._load_alpha_bots()
                # Also cleanup public memory (keep last 5 mins)
                self._cleanup_public_memory()
            
            # Count 'hot' pools (touched in last 60s)
            now = time.time()
            self._touched_pools = {addr: ts for addr, ts in self._touched_pools.items() if now - ts < 60}
            hot_count = len(self._touched_pools)
            
            # Snapshot active cycles count to avoid race condition with tracker
            active_count = len(list(self._tracker._active))
            
            log.debug(
                f"[mempool] Activity: {ws['hashes_received']} received | "
                f"{self._decoded_count} decoded | {hot_count} pools 'hot' | "
                f"{active_count} active cycles | Stalking {ws['stalked_count']} bots"
            )

    def _load_alpha_bots(self) -> None:
        """Load Alpha Bot addresses from competitors.jsonl."""
        from pathlib import Path
        import json
        comp_path = Path("logs/competitors.jsonl")
        if not comp_path.exists():
            return
            
        bots = set()
        try:
            with open(comp_path, "r") as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        if data.get("winner_eoa"): bots.add(data["winner_eoa"].lower())
                        if data.get("winner_contract"): bots.add(data["winner_contract"].lower())
                    except: continue
                    
            if bots:
                self._watcher.set_stalked_addresses(bots)
                # print(f"[mempool] Refreshed Alpha Bot list: {len(bots)} addresses stalked.")
        except Exception as e:
            log.debug(f"Failed to load alpha bots: {e}")

    def _cleanup_public_memory(self) -> None:
        """Remove hashes older than 5 minutes."""
        now = time.time()
        expired = [h for h, ts in self._seen_hashes.items() if now - ts > 300]
        for h in expired:
            del self._seen_hashes[h]
        if expired:
            log.debug(f"Cleaned up {len(expired)} hashes from public memory.")

