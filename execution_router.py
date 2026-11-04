"""
Execution Router Module
=======================
Centralized execution system for routing opportunities from detectors 
and mempool through a unified pipeline with async workers, filtering, 
and result processing.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, Callable
from dataclasses import dataclass
from enum import Enum

from log import get_logger

if TYPE_CHECKING:
    from graph import Opportunity
    from tracker import OpportunityTracker
    from executor import ArbExecutor, ExecutionResult

log = get_logger("execution_router")


class OppSource(Enum):
    """Source of the opportunity."""
    DETECTOR_CYCLES = "cycles"
    DETECTOR_NEW_POOL = "new_pool"
    DETECTOR_CROSS_PROTO = "cross_proto"
    DETECTOR_LIQUIDATION = "liquidation"
    MEMPOOL = "mempool"
    BACKRUN = "backrun"


@dataclass
class RoutedOpportunity:
    """Wrapper for an opportunity with routing metadata."""
    opp: "Opportunity"
    source: OppSource
    priority: float  # Higher = more urgent
    received_at: float
    
    @property
    def fingerprint(self) -> str:
        from tracker import _fingerprint
        return _fingerprint(self.opp)


class ExecutionRouter:
    """
    Centralized execution router that manages:
    - Async execution worker threads
    - Opportunity queuing and deduplication
    - Priority-based filtering
    - Result processing and lifecycle updates
    - Batch execution coordination
    
    Usage:
        router = ExecutionRouter(tracker, executor, config)
        router.start()
        
        # From detectors
        router.submit_opportunity(opp, OppSource.DETECTOR_CYCLES)
        
        # From mempool
        router.submit_opportunity(opp, OppSource.MEMPOOL, priority=2.0)
        
        router.shutdown()
    """
    
    def __init__(
        self,
        tracker: "OpportunityTracker",
        executor: "ArbExecutor | None",
        *,
        execute_onchain: bool = False,
        min_profit_usd: float = 0.01,
        max_executions_per_block: int = 3,
        streaming_multiplier: float = 2.0,
        queue_size: int = 10,
        async_workers: int = 1,
    ):
        self.tracker = tracker
        self.executor = executor
        # Check if executor is properly configured with arb_exec contract
        executor_ready = executor is not None and getattr(executor, '_arb_exec', None) is not None
        self.execute_onchain = execute_onchain and executor_ready
        self.min_profit_usd = min_profit_usd
        self.streaming_threshold = min_profit_usd * streaming_multiplier
        self.max_executions = max_executions_per_block
        self.queue_size = queue_size
        
        # Execution state
        self._executed_count = 0
        self._executed_lock = threading.Lock()
        self._queued_opps: set[str] = set()  # Fingerprints of queued ops
        self._queued_lock = threading.Lock()
        
        # Threading primitives
        self._shutdown = threading.Event()
        self._execution_queue: queue.Queue[RoutedOpportunity] = queue.Queue(maxsize=queue_size)
        self._execution_results: queue.Queue[tuple[RoutedOpportunity, Any]] = queue.Queue()
        
        # Worker threads
        self._async_workers = async_workers
        self._worker_threads: list[threading.Thread] = []
        self._result_thread: threading.Thread | None = None
        
    # ═══════════════════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════════════════
    
    def start(self) -> None:
        """Start execution worker threads."""
        if not self.execute_onchain:
            if self.executor is None:
                log.warning("[ROUTER] Execution disabled (no executor)")
            elif getattr(self.executor, '_arb_exec', None) is None:
                log.warning("[ROUTER] Execution disabled (no arb_exec contract configured)")
            else:
                log.warning("[ROUTER] Execution disabled (EXECUTE_ONCHAIN=False)")
            return
            
        log.info(f"[ROUTER] Starting execution router | workers={self._async_workers}")
        self._shutdown.clear()
        
        # Start async execution workers
        for i in range(self._async_workers):
            t = threading.Thread(
                target=self._execution_worker,
                name=f"exec_worker_{i}",
                daemon=True
            )
            t.start()
            self._worker_threads.append(t)
        
        # Start result processor
        self._result_thread = threading.Thread(
            target=self._result_processor,
            name="result_processor",
            daemon=True
        )
        self._result_thread.start()
        
    def shutdown(self, timeout: float = 15.0) -> None:
        """Shutdown execution router gracefully."""
        log.info("[ROUTER] Shutting down execution router...")
        self._shutdown.set()
        
        # Wait for workers to finish
        for t in self._worker_threads:
            t.join(timeout=timeout / len(self._worker_threads) if self._worker_threads else timeout)
        
        if self._result_thread:
            self._result_thread.join(timeout=2.0)
            
        log.info("[ROUTER] Shutdown complete")
    
    def submit_opportunity(
        self, 
        opp: "Opportunity", 
        source: OppSource,
        *,
        priority: float = 1.0,
        bypass_queue: bool = False
    ) -> bool:
        """
        Submit an opportunity for execution consideration.
        
        Args:
            opp: The opportunity to execute
            source: Where this opp came from (detector, mempool, etc)
            priority: Execution priority multiplier (higher = more urgent)
            bypass_queue: If True and executor available, execute immediately
            
        Returns:
            True if opportunity was accepted for execution
        """
        if not self.execute_onchain:
            return False
            
        # Check profit threshold
        if opp.estimated_profit_usd < self.min_profit_usd:
            return False
            
        # Check execution limit
        with self._executed_lock:
            if self._executed_count >= self.max_executions:
                return False
        
        routed = RoutedOpportunity(
            opp=opp,
            source=source,
            priority=priority,
            received_at=time.time()
        )
        
        fp = routed.fingerprint
        
        # Deduplication check
        with self._queued_lock:
            if fp in self._queued_opps:
                log.debug(f"[ROUTER] Opp already queued | fp={fp[:8]}")
                return False
        
        # High priority = immediate async streaming execution
        if opp.estimated_profit_usd >= self.streaming_threshold:
            try:
                self._execution_queue.put_nowait(routed)
                with self._queued_lock:
                    self._queued_opps.add(fp)
                log.info(
                    f"[ROUTER] STREAMING | {source.value} | "
                    f"profit=${opp.estimated_profit_usd:.4f} | fp={fp[:8]}"
                )
                return True
            except queue.Full:
                log.warning("[ROUTER] Execution queue full, skipping streaming")
                return False
        
        # Lower priority = will be processed in batch at end of block
        # Just mark as eligible for batch execution
        return False  # Not queued, but may be picked up by batch processor
    
    def submit_batch_opportunities(
        self, 
        opps: list["Opportunity"], 
        source: OppSource,
        block_number: int
    ) -> list["Opportunity"]:
        """
        Submit multiple opportunities for batch execution.
        Returns list of opportunities that were NOT executed (for tracking).
        """
        if not self.execute_onchain or not opps:
            return opps
            
        # Filter out already queued and low profit
        eligible = []
        for opp in opps:
            fp = self._fingerprint(opp)
            with self._queued_lock:
                if fp in self._queued_opps:
                    continue
            if opp.estimated_profit_usd >= self.min_profit_usd:
                eligible.append(opp)
        
        if not eligible:
            return []
            
        # Execute batch
        executed = self._execute_batch(eligible, block_number)
        
        # Return non-executed for tracking
        # executed_fps = {self._fingerprint(o) for o in executed}
        # return [o for o in opps if self._fingerprint(o) not in executed_fps]
        return executed
    
    def process_results(self) -> int:
        """
        Process any pending async execution results.
        Called periodically (e.g., between blocks) to update lifecycle.
        
        Returns:
            Number of results processed
        """
        processed = 0
        while not self._execution_results.empty():
            try:
                routed, result = self._execution_results.get_nowait()
                self._handle_result(routed, result)
                processed += 1
            except queue.Empty:
                break
        return processed
    
    def get_stats(self) -> dict:
        """Get current execution statistics."""
        with self._executed_lock:
            executed = self._executed_count
        with self._queued_lock:
            queued = len(self._queued_opps)
        return {
            "executed_count": executed,
            "queued_count": queued,
            "queue_size": self._execution_queue.qsize(),
            "results_pending": self._execution_results.qsize(),
            "execute_onchain": self.execute_onchain,
        }
    
    def reset_block_counter(self) -> None:
        """Reset per-block execution counter. Called at start of new block."""
        with self._executed_lock:
            old_count = self._executed_count
            self._executed_count = 0
        if old_count > 0:
            log.debug(f"[ROUTER] Reset block counter | previous={old_count}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Internal Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _execution_worker(self) -> None:
        """Background worker that processes queued opportunities."""
        while not self._shutdown.is_set():
            try:
                routed = self._execution_queue.get(timeout=0.1)
                if routed is None:
                    continue
                
                # Check if we've hit the limit
                with self._executed_lock:
                    if self._executed_count >= self.max_executions:
                        continue
                
                # Execute
                if self.executor:
                    result = self.executor.submit(routed.opp)
                    self._execution_results.put((routed, result))
                    
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"[ROUTER] Execution worker error: {e}")
    
    def _result_processor(self) -> None:
        """Background thread that processes execution results continuously."""
        log.info("[ROUTER] Result processor started")
        while not self._shutdown.is_set():
            try:
                routed, result = self._execution_results.get(timeout=0.1)
                self._handle_result(routed, result)
            except queue.Empty:
                time.sleep(0.05)
                continue
            except Exception as e:
                log.error(f"[ROUTER] Result processor error: {e}")
                log.debug(traceback.format_exc())
    
    def _handle_result(
        self, 
        routed: RoutedOpportunity, 
        result: "ExecutionResult | None"
    ) -> None:
        """Process a single execution result."""
        opp = routed.opp
        fp = routed.fingerprint
        
        # Remove from queued set
        with self._queued_lock:
            self._queued_opps.discard(fp)
        
        if not result:
            return
            
        # Update execution count
        with self._executed_lock:
            if result.success:
                self._executed_count += 1
        
        if not result.success:
            return
            
        # Calculate actual profit
        actual_profit_usd = 0.0
        if result.profit_wei > 0 and opp.tokens:
            from tvl import token_price
            root_token = opp.tokens[0]
            token_price_usd = token_price(root_token)
            if token_price_usd > 0:
                actual_profit_usd = (result.profit_wei / 1e18) * token_price_usd
        
        # Close in lifecycle
        block_number = getattr(result, 'block_number', 0) or 0
        self.tracker.close_executed(opp, block_number, result, actual_profit_usd)
        
        log.info(
            f"[ROUTER] EXECUTED | {routed.source.value} | "
            f"tx={result.tx_hash[:16] if hasattr(result, 'tx_hash') else 'N/A'} | "
            f"profit=${actual_profit_usd:.4f} | fp={fp[:8]}"
        )
    
    def _execute_batch(
        self, 
        opps: list["Opportunity"], 
        block_number: int
    ) -> ExecutionResult: # list["Opportunity"]:
        """Execute a batch of opportunities in parallel."""
        if not self.executor or not opps:
            return []

        
        result = self.executor.submit_batch(opps, block_number)
        if result.success:
            log.info(
                f"[ROUTER] BATCH_EXEC | tx={result.tx_hash[:16] if hasattr(result, 'tx_hash') else 'N/A'} | "
                f"profit=${result.profit_usd:.4f}"
            )
            if result.summary.successful:
                for opp in opps:
                    self.tracker.close_executed(opp, block_number, result, result.profit_usd)
            return result
        else:
            log.error(f"[ROUTER] Batch exec error: {result.error}")
            return None


            
        # executed: list["Opportunity"] = []
        
        # def _exec_single(opp: "Opportunity") -> tuple["Opportunity", Any]:
        #     try:
        #         result = self.executor.submit(opp)
        #         return opp, result
        #     except Exception as e:
        #         log.error(f"[ROUTER] Batch exec error: {e}")
        #         return opp, None
        
        # # Execute in parallel with limited workers
        # with ThreadPoolExecutor(max_workers=3) as pool:
        #     # Limit to max_executions remaining
        #     with self._executed_lock:
        #         remaining = self.max_executions - self._executed_count
            
        #     to_execute = opps[:max(0, remaining)]
        #     futures = {pool.submit(_exec_single, o): o for o in to_execute}
            
        #     for future in as_completed(futures):
        #         opp, result = future.result()
                
        #         if result and result.success:
        #             # Calculate profit
        #             actual_profit_usd = 0.0
        #             if result.profit_wei > 0 and opp.tokens:
        #                 from tvl import token_price
        #                 root_token = opp.tokens[0]
        #                 token_price_usd = token_price(root_token)
        #                 if token_price_usd > 0:
        #                     actual_profit_usd = (result.profit_wei / 1e18) * token_price_usd
                    
        #             # Close in lifecycle
        #             self.tracker.close_executed(opp, block_number, result, actual_profit_usd)
                    
        #             with self._executed_lock:
        #                 self._executed_count += 1
                    
        #             executed.append(opp)
                    
        #             log.info(
        #                 f"[ROUTER] BATCH_EXEC | tx={result.tx_hash[:16] if hasattr(result, 'tx_hash') else 'N/A'} | "
        #                 f"profit=${actual_profit_usd:.4f}"
        #             )
        
        # return executed
    
    @staticmethod
    def _fingerprint(opp: "Opportunity") -> str:
        from tracker import _fingerprint
        return _fingerprint(opp)


# ═══════════════════════════════════════════════════════════════════════════
# Convenience Functions
# ═══════════════════════════════════════════════════════════════════════════

def create_router(
    tracker: "OpportunityTracker",
    executor: "ArbExecutor | None",
    config: dict | None = None
) -> ExecutionRouter:
    """Factory function to create execution router from config."""
    cfg = config or {}
    return ExecutionRouter(
        tracker=tracker,
        executor=executor,
        execute_onchain=cfg.get("EXECUTE_ONCHAIN", False),
        min_profit_usd=cfg.get("MIN_PROFIT_USD", 0.01),
        max_executions_per_block=cfg.get("MAX_EXECUTIONS", 3),
        streaming_multiplier=cfg.get("STREAMING_MULTIPLIER", 2.0),
        queue_size=cfg.get("QUEUE_SIZE", 10),
        async_workers=cfg.get("ASYNC_WORKERS", 1),
    )
