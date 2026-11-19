"""Bounded event-independent discovery support (DSS-WO-MI-102B).

Reference findings: DSS-WO-MI-001A F-01/F-02 -- discovery was strictly
event-gated (CycleDetector only searched pools touched in the current block)
and pair searches were limited to hardcoded anchor pairs, so profitable routes
in untouched pools, non-anchor pairs, and freshly registered pools were
invisible until an unrelated event happened to touch them.

Design constraints (DSS-WO-MI-102B §3/§4, MI-001A bounded design):

- Supplement, never replace, the existing anchor pairs.
- At most ``additional_pair_limit`` (24) additional TVL-ranked pairs.
- At most ``max_pair_checks`` (30) total pair checks per pass
  (chain 1: 6 anchor pairs + 24 additional = 30).
- Cache the ranked selection and refresh it approximately every
  ``refresh_interval_blocks`` (50) blocks, or immediately when the graph
  materially changes (pool/edge count signature).
- Pair keys are normalized (case + direction), mirroring
  TokenGraph._make_pair_key, so opposite directions can never create
  duplicate pair candidates.
- Multi-venue pairs are preserved: every venue is retrieved through
  all_edges_for_pair(); the seed cap only bounds cycle-search origins,
  never the venue list a pair search sees.
- Existing minimum-liquidity and unpriced-pool filtering semantics are
  reused verbatim: a pool with no usable TVL (dust or genuinely unpriceable)
  cannot enter the ranked selection because its tvl_usd is 0 / below the
  same MIN_POOL_LIQUIDITY_USD gate the reactive paths apply.

This module deliberately contains no scheduling and no execution logic: the
canonical OpportunityEngine owns the cadence and every post-discovery
transition, exactly as before (DSS-WO-ENG-017 §5).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import MIN_POOL_LIQUIDITY_USD, get_chain
from log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from graph import TokenGraph

log = get_logger("bounded_discovery")


@dataclass(frozen=True)
class BoundedDiscoveryConfig:
    """Knobs of the bounded design. Defaults are the MI-001A numbers."""

    additional_pair_limit: int = 24
    max_pair_checks: int = 30
    refresh_interval_blocks: int = 50
    # How many top pools to scan when building the ranked pair selection.
    top_pool_scan: int = 400
    # Same liquidity gate the reactive pair searches apply
    # (MIN_LIQUIDITY_USD / MIN_POOL_LIQUIDITY_USD family).
    min_pair_tvl_usd: float = MIN_POOL_LIQUIDITY_USD
    # Cycle-search seeds: venues per selected pair and overall cap.
    # Anchor pairs contribute their top venue (they are additionally covered
    # by the reactive touched-pool scan and the per-pass pair checks); the
    # ranked additional pairs may contribute up to max_venues_per_pair each.
    # The overall cap bounds the batch: one find_cycles() per seed pool, the
    # same primitive the reactive path pays per touched pool. The batch is
    # processed in slices of scheduled_seeds_per_slice seeds per engine
    # invocation (DSS-WO-MI-102B-R1 §3), so no single invocation runs the
    # whole batch synchronously.
    max_venues_per_pair: int = 3
    max_seed_pools: int = 36
    # Newly registered pools consumed from the lossless backlog per scheduled
    # invocation. Consumption is bounded; the backlog itself is NOT bounded —
    # every eligible registration is retained until consumed (R1 §2).
    max_new_pool_seeds_per_pass: int = 24
    # Deterministic cycle-search seeds processed per engine invocation while
    # a scheduled batch is active (R1 §3). Default 1: the 20k-pool benchmark
    # (scripts/mi102b_benchmark.py) measures per-find_cycles cost as
    # median ~69 ms / p95 ~2.9 s / max ~3.2 s on the most hub-connected
    # seeds — a single call can exceed the ~2 s per-invocation target on its
    # own, so pairing seeds only widens the worst case (max slice 3.2 s at
    # slice=2 vs 2.9 s at slice=1). See the benchmark evidence in the R1
    # delivery report for the smallest necessary follow-up (a per-call node
    # budget inside the shared find_cycles primitive).
    scheduled_seeds_per_slice: int = 1


def normalize_pair(t1: str, t2: str) -> tuple[str, str]:
    """Direction- and case-normalized pair key (mirrors _make_pair_key)."""
    a, b = (t1 or "").lower(), (t2 or "").lower()
    return (a, b) if a <= b else (b, a)


def anchor_pairs(chain_id: int) -> list[tuple[str, str]]:
    """The existing hardcoded anchor pairs: wrapped native x stablecoins.

    Same anchor set CrossProtocolDetector/StableImbalanceDetector iterate
    today; normalized and deduplicated, order-stable.
    """
    chain = get_chain(chain_id)
    anchors = [chain.wrapped_native] + list(chain.stablecoins)
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for i, a in enumerate(anchors):
        for b in anchors[i + 1:]:
            key = normalize_pair(a, b)
            if key[0] == key[1] or key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def pool_is_searchable(graph: "TokenGraph", pool_address: str,
                       min_tvl_usd: float = MIN_POOL_LIQUIDITY_USD) -> bool:
    """True when a registered pool has valid, usable search state.

    Deliberately conservative (DSS-WO-MI-102B §5, R1 §4): incomplete,
    zero-reserve, pending ("waiting room"), unpriced, dust and otherwise
    sub-threshold pools are NOT discovery-eligible. The TVL gate reuses the
    existing production threshold and comparator (>= the same
    MIN_POOL_LIQUIDITY_USD gate the ranked pair selection applies), not a
    new independent threshold.
    """
    edges = graph.get_edges_for_pool(pool_address)
    if not edges:
        return False
    if any(getattr(e, "is_pending", False) for e in edges):
        return False
    if float(getattr(edges[0], "tvl_usd", 0.0) or 0.0) < float(min_tvl_usd):
        return False
    for e in edges:
        if e.version == 2:
            if e.reserve_in <= 0 or e.reserve_out <= 0:
                return False
        elif e.version == 3:
            if int(getattr(e, "liquidity", 0) or 0) <= 0 or int(getattr(e, "sqrt_price_x96", 0) or 0) <= 0:
                return False
    return True


class RankedPairUniverse:
    """Cached, TVL-ranked supplementary pair selection over the existing graph.

    One instance is shared by every consumer (the engine passes it to the
    pair-searching detector and uses its seeds for the scheduled cycle pass),
    so the graph is not re-enumerated per detector (DSS-WO-MI-102B §2).
    """

    def __init__(self, graph: "TokenGraph", chain_id: int, config: BoundedDiscoveryConfig | None = None):
        self._graph = graph
        self._chain_id = int(chain_id)
        self.cfg = config or BoundedDiscoveryConfig()
        self._additional: list[tuple[str, str]] = []
        self._refresh_block: int | None = None
        self._signature: tuple | None = None
        self.last_stats: dict = {}

    # ── selection ────────────────────────────────────────────────────────────

    def refresh(self, block_number: int, force: bool = False) -> bool:
        """Rebuild the ranked selection when stale; returns True if rebuilt.

        Staleness: >= refresh_interval_blocks since the last build, OR the
        graph's (pool_count, edge_count) signature changed (material growth),
        OR forced.
        """
        sig = (self._graph.pool_count, self._graph.edge_count)
        if not force and self._additional and self._signature == sig:
            if self._refresh_block is not None and block_number - self._refresh_block < self.cfg.refresh_interval_blocks:
                return False
        anchors = {normalize_pair(*p) for p in anchor_pairs(self._chain_id)}

        best: dict[tuple[str, str], float] = {}
        pools_scanned = 0
        for edge in self._graph.get_top_pools_by_tvl(self.cfg.top_pool_scan):
            pools_scanned += 1
            if self._chain_id and getattr(edge, "chain_id", self._chain_id) not in (0, self._chain_id):
                continue
            if getattr(edge, "is_pending", False):
                continue
            tvl = float(getattr(edge, "tvl_usd", 0.0) or 0.0)
            if tvl < self.cfg.min_pair_tvl_usd:
                continue  # dust and unpriced pools never seed pair selection
            key = normalize_pair(edge.token_in, edge.token_out)
            if key[0] == key[1] or key in anchors:
                continue
            if tvl > best.get(key, 0.0):
                best[key] = tvl

        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        budget = max(0, self.cfg.max_pair_checks - len(anchors))
        limit = min(self.cfg.additional_pair_limit, budget)
        self._additional = [pair for pair, _ in ranked[:limit]]
        self._refresh_block = int(block_number)
        self._signature = sig
        self.last_stats = {
            "pools_scanned": pools_scanned,
            "unique_pairs": len(best),
            "anchor_pairs": len(anchors),
            "additional_pairs": len(self._additional),
            "total_pair_checks": len(anchors) + len(self._additional),
            "refresh_block": self._refresh_block,
        }
        log.debug("[UNIVERSE] refreshed at block %s: %s", block_number, self.last_stats)
        return True

    def additional_pairs(self) -> list[tuple[str, str]]:
        """The selected supplementary pairs (normalized, TVL-ranked)."""
        return list(self._additional)

    # ── seeds ────────────────────────────────────────────────────────────────

    def seed_pools(self) -> list[str]:
        """Bounded cycle-search seeds for the scheduled pass.

        Anchor pairs contribute their single top-TVL venue (they are already
        covered reactively whenever their pools are touched, and by the
        per-pass pair checks); the ranked additional pairs contribute up to
        ``max_venues_per_pair`` venues each; the total is capped at
        ``max_seed_pools``. Pair SEARCHES always see every venue via
        all_edges_for_pair() — this cap only bounds the DFS origins of the
        cycle search.
        """
        anchors = anchor_pairs(self._chain_id)
        seeds: list[str] = []
        seen: set[str] = set()
        venues_retrieved = 0

        def collect(pair, per_pair_limit):
            nonlocal venues_retrieved
            if len(seeds) >= self.cfg.max_seed_pools:
                return
            edges = [
                e for e in self._graph.all_edges_for_pair(pair[0], pair[1])
                if not getattr(e, "is_pending", False)
                # R1 §4: cycle-search origins obey the SAME minimum-liquidity
                # eligibility the ranked pair selection applies. Sub-threshold
                # venues stay visible to pair searches (which filter with
                # their own existing semantics) but never become DFS origins.
                and float(getattr(e, "tvl_usd", 0.0) or 0.0) >= self.cfg.min_pair_tvl_usd
            ]
            venues_retrieved += len(edges)
            # One seed per VENUE (pool): dedupe pool addresses before the
            # top-K cut, otherwise a pool's two directions can both rank into
            # the top K and crowd out a real venue.
            edges.sort(key=lambda e: float(e.tvl_usd or 0.0), reverse=True)
            pool_seen: set[str] = set()
            for e in edges:
                if len(per_pair_limit) >= (1 if pair in anchor_set else self.cfg.max_venues_per_pair):
                    break
                addr = e.pool_address.lower()
                if addr in pool_seen:
                    continue
                pool_seen.add(addr)
                per_pair_limit.append(addr)
                if addr not in seen and len(seeds) < self.cfg.max_seed_pools:
                    seen.add(addr)
                    seeds.append(addr)

        anchor_set = set(anchors)
        for pair in anchors:
            collect(pair, [])
        for pair in self._additional:
            collect(pair, [])
        self.last_stats["venues_retrieved"] = venues_retrieved
        self.last_stats["seed_pools"] = len(seeds)
        return seeds
        self.last_stats["venues_retrieved"] = venues_retrieved
        self.last_stats["seed_pools"] = len(seeds)
        return seeds


class RecentlyRegistered:
    """Lossless FIFO of pools newly registered into the graph (R1 §2).

    Every registration source funnels through TokenGraph.upsert_pool_edges;
    recording the address there makes the pool eligible for the scheduled
    discovery batches without waiting for its first swap event and without
    any new event framework.

    The backlog is deliberately NOT a fixed-size ring: it grows with the
    number of genuinely unprocessed registrations and never silently drops
    an entry to overflow (the MI-102B deque(maxlen=256) did, which this
    class replaces). Addresses are case-normalized and deduplicated, so
    mixed-case re-registrations cannot create duplicates. Consumption is
    bounded per invocation via pop_upto(); work per engine call stays
    bounded even while the backlog is large.
    """

    def __init__(self) -> None:
        self._queue: deque = deque()
        self._member: set[str] = set()
        self.received = 0

    def record(self, pool_address: str) -> None:
        addr = (pool_address or "").lower()
        if not addr:
            return
        self.received += 1
        if addr in self._member:
            return
        self._member.add(addr)
        self._queue.append(addr)

    def pop_upto(self, n: int) -> list[str]:
        """Pop at most ``n`` oldest entries; the rest remain queued."""
        out: list[str] = []
        while self._queue and len(out) < max(0, n):
            addr = self._queue.popleft()
            self._member.discard(addr)
            out.append(addr)
        return out

    def drain(self) -> list[str]:
        """Return and clear ALL pending addresses (oldest first)."""
        out = list(self._queue)
        self._queue.clear()
        self._member.clear()
        return out

    def __len__(self) -> int:
        return len(self._queue)
