"""
Enhanced Logger Module
==================
Standardized opportunity model with JSON logging and stats tracking.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional
from collections import deque

from config import LOG_DIR


class OppType(str, Enum):
    """All opportunity types supported by the system."""
    # ── Cycle-based (graph DFS) ──────────────────────────────────────────
    DEX_SPREAD       = "DEX_SPREAD"       # 2-hop, cross-DEX (classic arb)
    SAME_DEX_CYCLE   = "SAME_DEX_CYCLE"  # 2-hop, same-DEX (usually noise/reflexive)
    TRIANGULAR       = "TRIANGULAR"       # 3-hop, same or cross-DEX
    CROSS_DEX_TRI    = "CROSS_DEX_TRI"   # 3-hop, explicitly cross-DEX
    MULTI_HOP        = "MULTI_HOP"        # 4+ hops, same-DEX
    CROSS_DEX_MULTI  = "CROSS_DEX_MULTI" # 4+ hops, cross-DEX
    # ── Detector-based ───────────────────────────────────────────────────
    NEW_POOL         = "NEW_POOL"
    CROSS_PROTOCOL   = "CROSS_PROTOCOL"
    LIQUIDATION      = "LIQUIDATION"
    # ── Future stubs ─────────────────────────────────────────────────────
    ORACLE_MANIPULATION = "ORACLE_MANIPULATION"
    CROSS_CHAIN         = "CROSS_CHAIN"
    LENDING_ARBITRAGE   = "LENDING_ARBITRAGE"
    PERPETUALS_BASIS    = "PERPETUALS_BASIS"
    RESEARCH            = "RESEARCH"


@dataclass
class Opportunity:
    """Standardized opportunity representation."""
    # Identity
    opp_type: OppType
    chain_id: int
    block_number: int
    timestamp: float = field(default_factory=time.time)
    
    # Tokens & pools
    tokens: list[str] = field(default_factory=list)
    pools: list[str] = field(default_factory=list)
    fees: list[str] = field(default_factory=list)
    dexes: list[str] = field(default_factory=list)
    
    # Economics
    estimated_profit_usd: float = 0.0
    estimated_profit_ratio: float = 0.0
    amount_in_usd: float = 0.0
    gas_cost_usd: float = 0.0
    net_profit_usd: float = 0.0
    
    # PRODUCTION SPEC 3.2 & 4.2.1
    iddfs_depth: int = 0
    validation_probe_usd: float = 10.0 # Standard $10 probe
    
    # Simulation
    simulated_via_contract: bool = False
    simulation_profit_wei: int = 0
    
    # Metadata (flexible per detector)
    metadata: dict = field(default_factory=dict)
    
    # NEW: Enhanced fields
    confidence: float = 1.0  # 0-1, how confident we are
    competition_level: str = "unknown"  # low/medium/high
    execution_window_seconds: float = 0.0  # How long the opp was available

    def to_dict(self) -> dict:
        d = asdict(self)
        d["opp_type"] = self.opp_type.value
        return d
    
    def __repr__(self) -> str:
        return (
            f"[{self.opp_type.value}] block={self.block_number} "
            f"profit=${self.net_profit_usd:.4f} "
            f"path={' → '.join(self.dexes[:3])}"
        )


@dataclass
class _TypeStats:
    """Statistics for a specific opportunity type."""
    opp_type: str
    count: int = 0
    total_profit_usd: float = 0.0
    best_profit_usd: float = 0.0
    avg_profit_usd: float = 0.0
    avg_profit_ratio: float = 0.0
    simulated_count: int = 0
    last_seen_block: int = 0
    
    # NEW: Competition metrics
    avg_competition_level: str = "unknown"
    high_competition_count: int = 0
    
    # Rolling window
    _recent_profits: list = field(default_factory=list)

    def record(self, opp: Opportunity) -> None:
        self.count += 1
        self.total_profit_usd += opp.net_profit_usd
        self.best_profit_usd = max(self.best_profit_usd, opp.net_profit_usd)
        self.avg_profit_usd = self.total_profit_usd / self.count
        self.last_seen_block = opp.block_number
        
        if opp.simulated_via_contract:
            self.simulated_count += 1
        
        if opp.competition_level == "high":
            self.high_competition_count += 1
        
        self._recent_profits.append(opp.net_profit_usd)
        if len(self._recent_profits) > 10:
            self._recent_profits.pop(0)
        
        if self._recent_profits:
            self.avg_profit_ratio = sum(self._recent_profits) / len(self._recent_profits)


class OppLogger:
    """
    Thread-safe opportunity logger.
    - Writes to per-type JSONL files
    - Maintains in-memory ring buffer for dashboard
    - Tracks statistics
    """
    
    def __init__(self, log_dir: str = LOG_DIR, buffer_size: int = 500):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        
        # Ensure subdirectories exist
        (self._dir / "reports").mkdir(exist_ok=True)
        (self._dir / "detailed").mkdir(exist_ok=True)
        
        self._buffer: deque[Opportunity] = deque(maxlen=buffer_size)
        self._stats: dict[str, _TypeStats] = {}
        self._file_handles: dict[str, object] = {}
        self._total_logged = 0
        
        # Lifetime tracking
        self._started_at = time.time()
    
    # ══════════════════════════════════════════════════════════════════════════════
    # LOGGING
    # ══════════════════════════════════════════════════════════════════════════════
    
    def log(self, opp: Opportunity) -> None:
        """Log an opportunity."""
        self._buffer.append(opp)
        self._update_stats(opp)
        self._write_jsonl(opp)
        self._total_logged += 1
    
    def _write_jsonl(self, opp: Opportunity) -> None:
        """Write to JSONL file using a persistent handle (opened once per type)."""
        key = opp.opp_type.value.lower()
        if key not in self._file_handles:
            path = self._dir / f"{key}.jsonl"
            self._file_handles[key] = open(path, "a", encoding="utf-8")
        fh = self._file_handles[key]
        fh.write(json.dumps(opp.to_dict()) + "\n")
        fh.flush()  # Manually flush to disk
    
    def log_execution(self, block_number: int, tx_hash: str, profit_wei: int,
                      actual_profit_usd: float, gas_cost_usd: float,
                      lender: str, method: str) -> None:
        """Log a minimal execution entry for PnL tracking."""
        exec_entry = {
            "timestamp": time.time(),
            "block_number": block_number,
            "tx_hash": tx_hash,
            "profit_wei": profit_wei,
            "actual_profit_usd": round(actual_profit_usd, 6),
            "gas_cost_usd": round(gas_cost_usd, 6),
            "net_profit_usd": round(actual_profit_usd - gas_cost_usd, 6),
            "lender": lender,
            "method": method,
        }
        
        key = "executions"
        if key not in self._file_handles:
            path = self._dir / "executions.jsonl"
            self._file_handles[key] = open(path, "a", encoding="utf-8")
        fh = self._file_handles[key]
        fh.write(json.dumps(exec_entry) + "\n")
        fh.flush()

    def _write_detailed(self, opp: Opportunity) -> None:
        """Write detailed log with full context (opens handle once per type)."""
        key = f"{opp.opp_type.value.lower()}_detail"
        if key not in self._file_handles:
            path = self._dir / "detailed" / f"{opp.opp_type.value.lower()}_detailed.jsonl"
            self._file_handles[key] = open(path, "a", encoding="utf-8")
        fh = self._file_handles[key]
        fh.write(json.dumps(opp.to_dict(), indent=2) + "\n")
        fh.flush()  # Manually flush to disk

    def close(self) -> None:
        """Flush and close all open file handles. Call on shutdown."""
        for fh in self._file_handles.values():
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        self._file_handles.clear()
    
    def __del__(self) -> None:
        self.close()
    
    def _update_stats(self, opp: Opportunity) -> None:
        """Update statistics."""
        key = opp.opp_type.value
        if key not in self._stats:
            self._stats[key] = _TypeStats(opp_type=key)
        self._stats[key].record(opp)
    
    # ══════════════════════════════════════════════════════════════════════════════
    # READ API
    # ══════════════════════════════════════════════════════════════════════════════
    
    @property
    def recent(self) -> list[Opportunity]:
        return list(self._buffer)
    
    @property
    def total_logged(self) -> int:
        return self._total_logged
    
    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._started_at
    
    def stats(self) -> list[_TypeStats]:
        return sorted(self._stats.values(), key=lambda s: s.total_profit_usd, reverse=True)
    
    def top_opportunities(self, n: int = 10) -> list[Opportunity]:
        all_opps = list(self._buffer)
        return sorted(all_opps, key=lambda o: o.net_profit_usd, reverse=True)[:n]
    
    def recent_by_type(self, opp_type: OppType, n: int = 5) -> list[Opportunity]:
        return list(reversed([o for o in self._buffer if o.opp_type == opp_type]))[:n]
    
    def opportunities_since(self, block: int) -> list[Opportunity]:
        """Get all opportunities since a specific block."""
        return [o for o in self._buffer if o.block_number > block]
    
    def total_profit_since(self, block: int) -> float:
        """Calculate total profit since a block."""
        return sum(o.net_profit_usd for o in self.opportunities_since(block))
    
    def get_opportunities_in_range(
        self, 
        start_block: int, 
        end_block: int,
        opp_type: Optional[OppType] = None
    ) -> list[Opportunity]:
        """Get opportunities within a block range."""
        opps = [o for o in self._buffer 
                if start_block <= o.block_number <= end_block]
        if opp_type:
            opps = [o for o in opps if o.opp_type == opp_type]
        return opps


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def format_opportunity_summary(opp: Opportunity) -> str:
    """Format an opportunity as a human-readable string."""
    tokens_str = " → ".join([t[:8] for t in opp.tokens[:4]])
    dexes_str = " → ".join(opp.dexes[:3])
    
    return (
        f"[{opp.opp_type.value}] "
        f"Block {opp.block_number} | "
        f"Path: {tokens_str} | "
        f"DEX: {dexes_str} | "
        f"Profit: ${opp.net_profit_usd:.4f} | "
        f"Ratio: {opp.estimated_profit_ratio*100:.3f}%"
    )


def calculate_opportunity_score(opp: Opportunity) -> float:
    """
    Calculate a composite score for ranking opportunities.
    Factors in profit, confidence, and competition level.
    """
    profit_score = opp.net_profit_usd * 10
    
    confidence_multiplier = opp.confidence
    
    competition_penalty = {
        "low": 1.0,
        "medium": 0.7,
        "high": 0.4,
        "unknown": 0.5,
    }.get(opp.competition_level, 0.5)
    
    return profit_score * confidence_multiplier * competition_penalty

