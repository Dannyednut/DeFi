"""
Enhanced Tracker Module
==================
Opportunity lifetime tracking with fingerprinting.
"""
from __future__ import annotations

import hashlib
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

from log import get_logger
from logger import Opportunity, OppLogger, OppType
from executor.executor import ExecutionResult

log = get_logger("tracker")


# ════════════════════════════════════════════════════════════════════════════════
# LIFECYCLE EVENTS
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class LifecycleEvent:
    """Lifecycle event for an opportunity."""
    fingerprint: str
    status: str  # "OPEN", "CLOSED"
    opp_type: str
    chain_id: int
    
    open_block: int
    open_time: float
    close_block: Optional[int] = None
    close_time: Optional[float] = None
    last_seen_block: int = 0
    duration_blocks: int = 0
    duration_seconds: float = 0.0
    
    peak_profit_usd: float = 0.0
    open_profit_usd: float = 0.0
    close_profit_usd: float = 0.0
    update_count: int = 0
    
    tokens: list = field(default_factory=list)
    pools: list = field(default_factory=list)
    dexes: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "status": self.status,
            "opp_type": self.opp_type,
            "chain_id": self.chain_id,
            "open_block": self.open_block,
            "open_time": self.open_time,
            "close_block": self.close_block,
            "close_time": self.close_time,
            "last_seen_block": self.last_seen_block,
            "duration_blocks": self.duration_blocks,
            "duration_seconds": round(self.duration_seconds, 3),
            "peak_profit_usd": round(self.peak_profit_usd, 6),
            "open_profit_usd": round(self.open_profit_usd, 6),
            "close_profit_usd": round(self.close_profit_usd, 6),
            "update_count": self.update_count,
            "tokens": self.tokens,
            "pools": self.pools,
            "dexes": self.dexes,
            "metadata": self.metadata,
        }


# ════════════════════════════════════════════════════════════════════════════════
# ACTIVE OPPORTUNITY STATE
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class _ActiveOpp:
    """In-memory state for an active opportunity."""
    fingerprint: str
    opp: Opportunity
    open_block: int
    open_time: float
    last_seen_block: int
    last_seen_time: float
    peak_profit_usd: float
    update_count: int = 0


# ════════════════════════════════════════════════════════════════════════════════
# TRACKER
# ════════════════════════════════════════════════════════════════════════════════

class OpportunityTracker:
    """
    Tracks opportunity lifetimes across blocks.
    - Deduplicates opportunities by fingerprint
    - Logs OPENED/CLOSED events
    - Tracks lifetime statistics
    """
    
    def __init__(self, logger: OppLogger):
        self._logger = logger
        self._active: dict[str, _ActiveOpp] = {}
        self._lock = threading.RLock()
        log.info("OpportunityTracker initialised")
        self._lifetime_stats: dict[str, _LifetimeStats] = {}
    
    def process_block(
        self,
        block_number: int,
        detected: list[Opportunity],
        filled_pools: dict[str, str] = None,
    ) -> None:
        """Process detected opportunities for a block."""
        with self._lock:
            now = time.time()
            filled_pools = filled_pools or {}
            
            # Build fingerprint map for this block
            this_block: dict[str, Opportunity] = {}
            for opp in detected:
                fp = _fingerprint(opp)
                if fp not in this_block or opp.net_profit_usd > this_block[fp].net_profit_usd:
                    this_block[fp] = opp
            
            # Handle NEW (OPENED) opportunities
            for fp, opp in this_block.items():
                if fp not in self._active:
                    active = _ActiveOpp(
                        fingerprint=fp,
                        opp=opp,
                        open_block=block_number,
                        open_time=now,
                        last_seen_block=block_number,
                        last_seen_time=now,
                        peak_profit_usd=opp.net_profit_usd,
                    )
                    self._active[fp] = active
    
                    opp_type_str = opp.opp_type.value if hasattr(opp.opp_type, "value") else str(opp.opp_type)
                    log.research(
                        f"OPENED [{opp_type_str}] fp={fp} "
                        f"profit=${opp.net_profit_usd:.4f} "
                        f"hops={len(opp.pools)} dexes={opp.dexes} "
                        f"block={block_number}"
                    )
    
                    # Log the opportunity
                    self._logger.log(opp)
    
                    # Write lifecycle event
                    self._write_lifecycle(LifecycleEvent(
                        fingerprint=fp,
                        status="OPEN",
                        opp_type=opp_type_str,
                        chain_id=opp.chain_id,
                        open_block=block_number,
                        open_time=now,
                        last_seen_block=block_number,
                        open_profit_usd=opp.net_profit_usd,
                        peak_profit_usd=opp.net_profit_usd,
                        tokens=opp.tokens,
                        pools=opp.pools,
                        dexes=opp.dexes,
                        metadata=opp.metadata,
                    ))
            
            # Update EXISTING opportunities
            for fp, active in self._active.items():
                if fp in this_block:
                    opp = this_block[fp]
                    active.last_seen_block = block_number
                    active.last_seen_time = now
                    active.update_count += 1
                    active.peak_profit_usd = max(active.peak_profit_usd, opp.net_profit_usd)
                    active.opp = opp
            
            # Handle CLOSED (disappeared) or STALE (TTL expired) opportunities
            MAX_STALE_BLOCKS = 5  # evict if not re-detected within 5 blocks
            
            for fp, active in list(self._active.items()):
                if fp in this_block:
                    continue
                    
                opp = active.opp
                
                # Check if this opportunity was likely 'filled' by a competitor
                matched_pool = next((p.lower() for p in opp.pools if p.lower() in filled_pools), None)
                is_filled = matched_pool is not None
                
                # Not filled and not stale yet - keep tracking
                if not is_filled and (block_number - active.last_seen_block) <= MAX_STALE_BLOCKS:
                    continue
                
                # Actually closing this opp - remove from active
                self._active.pop(fp)
                
                opp_type_str = (
                    opp.opp_type.value if hasattr(opp.opp_type, "value") else str(opp.opp_type)
                )
                duration_blocks = active.last_seen_block - active.open_block + 1
                duration_seconds = active.last_seen_time - active.open_time
                
                metadata = opp.metadata.copy()
                if is_filled:
                    metadata["outcome"] = "FILLED_BY_COMPETITOR"
                    metadata["tx_hash"] = filled_pools[matched_pool]
                else:
                    metadata["outcome"] = "EXPIRED"
    
                outcome_label = "FILLED" if is_filled else "EXPIRED"
                log.info(
                    f"CLOSED [{opp_type_str}] fp={fp} outcome={outcome_label} "
                    f"lived={duration_blocks}blk/{duration_seconds:.1f}s "
                    f"peak=${active.peak_profit_usd:.4f}"
                )
    
                evt = LifecycleEvent(
                    fingerprint=fp,
                    status="CLOSED",
                    opp_type=opp_type_str,
                    chain_id=opp.chain_id,
                    open_block=active.open_block,
                    open_time=active.open_time,
                    close_block=block_number,
                    close_time=now,
                    last_seen_block=active.last_seen_block,
                    duration_blocks=duration_blocks,
                    duration_seconds=duration_seconds,
                    peak_profit_usd=active.peak_profit_usd,
                    open_profit_usd=opp.net_profit_usd,
                    close_profit_usd=active.opp.net_profit_usd,
                    update_count=active.update_count,
                    tokens=opp.tokens,
                    pools=opp.pools,
                    dexes=opp.dexes,
                    metadata=metadata,
                )
                self._write_lifecycle(evt)
                self._update_lifetime_stats(evt)
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ════════════════════════════════════════════════════════════════════════════════
    
    def close_executed(self, opp: Opportunity, block_number: int, result: ExecutionResult, actual_profit_usd: float) -> None:
        """Close an opportunity with execution result data."""
        with self._lock:
            fp = _fingerprint(opp)
            if fp not in self._active:
                return
            
            active = self._active.pop(fp)
            now = time.time()
            
            opp_type_str = opp.opp_type.value if hasattr(opp.opp_type, "value") else str(opp.opp_type)
            duration_blocks = block_number - active.open_block
            duration_seconds = now - active.open_time
            
            log.info(
                f"CLOSED [{opp_type_str}] fp={fp} outcome=EXECUTED "
                f"lived={duration_blocks}blk/{duration_seconds:.1f}s "
                f"profit=${actual_profit_usd:.4f} tx={result.tx_hash[:16]}"
            )

            self._logger.log_execution(
                block_number, result.tx_hash, result.profit_wei, actual_profit_usd,
                result.gas_cost_usd, result.lender, result.method
            )
            
            evt = LifecycleEvent(
                fingerprint=fp,
                status="CLOSED",
                opp_type=opp_type_str,
                chain_id=opp.chain_id,
                open_block=active.open_block,
                open_time=active.open_time,
                close_block=block_number,
                close_time=now,
                last_seen_block=block_number,
                duration_blocks=duration_blocks,
                duration_seconds=duration_seconds,
                peak_profit_usd=actual_profit_usd,
                open_profit_usd=active.opp.net_profit_usd,
                close_profit_usd=actual_profit_usd,
                update_count=active.update_count,
                tokens=opp.tokens,
                pools=opp.pools,
                dexes=opp.dexes,
                metadata={"outcome": "EXECUTED", "tx_hash": result.tx_hash, "actual_profit_usd": actual_profit_usd},
            )
            self._write_lifecycle(evt)
            self._update_lifetime_stats(evt)
    
    @property
    def active(self) -> list[_ActiveOpp]:
        """Get active opportunities sorted by age."""
        return sorted(self._active.values(), key=lambda a: a.open_block)
    
    @property
    def active_count(self) -> int:
        return len(self._active)
    
    def lifetime_stats(self) -> list:
        return sorted(
            self._lifetime_stats.values(),
            key=lambda s: s.avg_duration_blocks,
            reverse=True,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # INTERNAL
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def _write_lifecycle(self, evt: LifecycleEvent) -> None:
        import json
        from pathlib import Path
        from config import LOG_DIR

        try:
            path = Path(LOG_DIR) / "lifecycle.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            
            # Read existing events, update if same fingerprint, else append
            events = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            data = json.loads(line)
                            fp = data.get("fingerprint")
                            if fp:
                                events[fp] = data
                        except:
                            continue
            
            # Update sequence
            events[evt.fingerprint] = evt.to_dict()
            
            # Rewrite file (consolidated)
            with open(path, "w", encoding="utf-8") as f:
                for ev_data in events.values():
                    f.write(json.dumps(ev_data) + "\n")
                f.flush()
                
        except Exception as e:
            log.warning(f"Failed to write lifecycle event {evt.fingerprint}: {e}")
    
    def _update_lifetime_stats(self, evt: LifecycleEvent) -> None:
        key = evt.opp_type
        if key not in self._lifetime_stats:
            self._lifetime_stats[key] = _LifetimeStats(opp_type=key)
        self._lifetime_stats[key].record(evt)


# ════════════════════════════════════════════════════════════════════════════════
# LIFETIME STATISTICS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class _LifetimeStats:
    """Lifetime statistics for a strategy type."""
    opp_type: str
    closed_count: int = 0
    total_duration_blocks: int = 0
    max_duration_blocks: int = 0
    min_duration_blocks: int = 999_999
    avg_duration_blocks: float = 0.0
    total_duration_secs: float = 0.0
    avg_duration_secs: float = 0.0
    max_duration_secs: float = 0.0
    avg_peak_profit_usd: float = 0.0
    total_peak_profit_usd: float = 0.0
    
    def record(self, evt: LifecycleEvent) -> None:
        self.closed_count += 1
        self.total_duration_blocks += evt.duration_blocks
        self.total_duration_secs += evt.duration_seconds
        self.total_peak_profit_usd += evt.peak_profit_usd
        self.max_duration_blocks = max(self.max_duration_blocks, evt.duration_blocks)
        self.min_duration_blocks = min(self.min_duration_blocks, evt.duration_blocks)
        self.max_duration_secs = max(self.max_duration_secs, evt.duration_seconds)
        self.avg_duration_blocks = self.total_duration_blocks / self.closed_count
        self.avg_duration_secs = self.total_duration_secs / self.closed_count
        self.avg_peak_profit_usd = self.total_peak_profit_usd / self.closed_count


# ═══════════════════════════════════════════════════════════════════════════════
# FINGERPRINTING
# ═══════════════════════════════════════════════════════════════════════════════

def _fingerprint(opp: Opportunity) -> str:
    """
    Create a stable fingerprint for an opportunity.
    Uses opp_type + sorted pool addresses + root token + sorted dex list.

    FIX: dexes are now included so that two opps on the same pools but
    different strategy types (e.g. DEX_SPREAD vs TRIANGULAR) don't collide.
    """
    opp_type  = opp.opp_type.value if hasattr(opp.opp_type, "value") else str(opp.opp_type)
    pools_key = "|".join(sorted(p.lower() for p in opp.pools))
    root_token = opp.tokens[0].lower() if opp.tokens else ""
    dex_key   = "|".join(sorted(set(d.lower() for d in opp.dexes)))
    raw = f"{opp_type}:{pools_key}:{root_token}:{dex_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

