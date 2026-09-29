"""Split-route detector.

This detector has one thesis only: fragmented liquidity makes a multi-pool
split execution better than the best single pool.  New-pool and generic
cross-protocol detection are deliberately not implemented here.
"""
from __future__ import annotations
import time
from dataclasses import dataclass

from detectors.base import BaseDetector
from logger import Opportunity, OppType
from config import get_chain
from tvl import token_price, token_decimals
from routing import ArbKind, execution_capability
from optimizer.split_routes import greedy_split
from log import get_logger

log = get_logger("split_route")


@dataclass(frozen=True)
class CandidateConfig:
    min_tvl_usd: float = 25_000.0
    min_fragmentation_usd: float = 100_000.0
    split_probe_usd: float = 100_000.0
    min_split_improvement_bps: float = 2.0
    max_pairs: int = 250


class SplitRouteDetector(BaseDetector):
    name = "split_route"
    requires_touched_pools = True
    arb_kind = ArbKind.SPLIT_ROUTE

    def __init__(self, w3, graph, logger, cache, chain_id, config: CandidateConfig | None = None):
        super().__init__(w3, graph, logger, cache, chain_id)
        self.chain = get_chain(chain_id)
        self.cfg = config or CandidateConfig()

    def on_block(self, block_number: int) -> list[Opportunity]:
        out = []
        for edges in self._pair_candidates():
            # Fix a single canonical direction (lexicographically smaller
            # token address as token_in) before deduping by pool -- without
            # this, {e.pool_address.lower(): e for e in edges} silently keeps
            # whichever direction happened to be LAST in the raw edge list
            # for each pool independently. Since different pools can have
            # their two directions inserted in different orders (confirmed:
            # depends on which order upsert_pool_edges was called in for
            # each pool, which isn't guaranteed consistent across discovery
            # sources), that dedup could end up mixing token_in->token_out
            # edges from one pool with token_out->token_in edges from
            # another for the "same" candidate set -- greedy_split would
            # then be comparing/summing incompatible reciprocal rates across
            # pools, corrupting the whole split-route calculation silently.
            canonical_token_in = min(edges[0].token_in.lower(), edges[0].token_out.lower())
            edges = [e for e in edges if e.token_in.lower() == canonical_token_in]
            by_pool = {e.pool_address.lower(): e for e in edges}
            edges = list(by_pool.values())
            if len(edges) < 2 or sum(e.tvl_usd for e in edges) < self.cfg.min_fragmentation_usd:
                continue
            price = token_price(edges[0].token_in) or 0.0
            if price <= 0:
                continue
            decimals = token_decimals(edges[0].token_in)
            amount = max(1, int(self.cfg.split_probe_usd / price * (10 ** decimals)))
            result = greedy_split(edges, amount, chunks=24)
            if result.amount_out <= 0:
                continue
            # Correct baseline: what a single pool would produce for the
            # FULL amount, not the largest individual allocation WITHIN the
            # split (a pool that only received a fraction of amount) -- see
            # the detailed note on SplitRouteResult/greedy_split in
            # optimizer/split_routes.py. The old
            # `max(a.amount_out for a in result.allocations)` comparison
            # made every split look like a ~100% improvement regardless of
            # whether splitting actually helped, since each allocation is
            # inherently smaller than the undivided amount.
            best_single = result.best_single_amount_out
            if best_single <= 0:
                continue
            improvement = (result.amount_out - best_single) / best_single * 10_000
            if improvement < self.cfg.min_split_improvement_bps:
                continue
            protocols = [a.protocol for a in result.allocations]
            supported, reason = execution_capability(protocols)
            out.append(self._opportunity(
                opp_type=OppType.SPLIT_ROUTE,
                block_number=block_number,
                tokens=[result.token_in, result.token_out],
                pools=[a.pool for a in result.allocations],
                dexes=[a.dex for a in result.allocations],
                estimated_profit_ratio=improvement / 10_000,
                estimated_profit_usd=0.0,
                amount_in_usd=self.cfg.split_probe_usd,
                confidence=0.55 if result.approximate else 0.80,
                metadata={
                    "arb_kind": self.arb_kind.value,
                    "strategy": "split_route",
                    "allocation": [a.__dict__ for a in result.allocations],
                    "improvement_bps": improvement,
                    "approximate": result.approximate,
                    "execution_supported": False,
                    "execution_reason": "split-route executor is not deployed",
                    "requires_exact_validation": supported,
                },
            ))
        return out

    def _pair_candidates(self):
        seen = set()
        count = 0
        for _, edges in getattr(self.graph, "_token_pairs", {}).items():
            usable = [e for e in edges if e.chain_id == self.chain_id and e.tvl_usd >= self.cfg.min_tvl_usd]
            if len(usable) < 2:
                continue
            key = tuple(sorted((usable[0].token_in.lower(), usable[0].token_out.lower())))
            if key in seen:
                continue
            seen.add(key)
            yield usable
            count += 1
            if count >= self.cfg.max_pairs:
                return

# Backwards-compatible import name; do not use it for runtime registration.
AdvancedOpportunityDetector = SplitRouteDetector
