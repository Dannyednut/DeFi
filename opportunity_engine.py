"""Single canonical opportunity pipeline.

Detector theses are distinct; this engine owns every transition after discovery.
No detector or mempool component may independently promote an opportunity to
execution-ready.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import statistics
import time

from logger import Opportunity, ValidationStage
from optimizer.opportunity_ranker import rank, RankConfig
from config import NATIVE_PRICE_USD
from bounded_discovery import BoundedDiscoveryConfig, RankedPairUniverse, pool_is_searchable
from log import get_logger

@dataclass(frozen=True)
class PipelineResult:
    candidates: tuple[Opportunity, ...] = ()
    ready: tuple[Opportunity, ...] = ()
    rejected: tuple[Opportunity, ...] = ()

@dataclass
class OpportunityEngine:
    w3: object
    chain_id: int
    detectors: list = field(default_factory=list)
    rank_config: RankConfig = field(default_factory=RankConfig)
    validation_workers: int = 4
    max_ready: int = 12
    graph: object | None = None
    # ── MI-102B: bounded event-independent discovery ─────────────────────────
    # The engine owns the cadence and the shared pair universe; detectors are
    # invoked through their existing on_block contracts and every candidate
    # converges on the same dedup/validation pipeline below.
    scheduled_discovery: bool = True
    scheduled_pass_interval: int = 50
    bounded_config: BoundedDiscoveryConfig = field(default_factory=BoundedDiscoveryConfig)
    pair_universe: RankedPairUniverse | None = None
    _last_scheduled_block: int | None = None
    _last_scheduled_info: dict = field(default_factory=dict)
    _scheduled_batch: dict | None = None
    _last_batch_summary: dict = field(default_factory=dict)
    _notified_new_pools: list[str] = field(default_factory=list)

    def _context(self, block_number: int) -> tuple[int, str]:
        header = self.w3.eth.get_block(block_number)
        h = header.get("hash") if hasattr(header, "get") else getattr(header, "hash", None)
        return int(block_number), h.hex() if hasattr(h, "hex") else str(h)

    def _canonicalize(self, opp: Opportunity, detector_name: str | None = None, block_number: int | None = None, block_hash: str = "", state_block: int | None = None, state_source: str = "") -> Opportunity:
        meta = dict(opp.metadata or {})
        if detector_name:
            meta["detector"] = detector_name
        meta.setdefault("arb_kind", opp.arb_kind.value)
        meta.setdefault("schema_version", opp.schema_version)
        meta.setdefault("validation_status", ValidationStage.DISCOVERED.value)
        meta.setdefault("execution_simulated", False)
        meta.setdefault("execution_ready", False)
        if block_number is not None:
            meta["discovery_block"] = int(block_number)
            meta["discovery_block_hash"] = block_hash
        if state_block is not None:
            meta["state_block"] = int(state_block)
            meta["state_source"] = state_source or meta.get("state_source", "")
        opp.metadata = meta
        return opp

    def _discover(self, block_number: int, touched_pools: set[str] | None = None, block_context=None) -> list[Opportunity]:
        candidates = []
        if block_context is not None:
            bh = block_context.block_hash
            state_block = block_context.number
            state_source = "onchain:block_information"
        else:
            _, bh = self._context(block_number)
            state_block = block_number
            state_source = "onchain:block_header"

        def finalize(found, detector_name: str | None) -> None:
            """Shared canonicalization + route-state annotation for every
            discovery source (reactive detectors AND the MI-102B scheduled
            pass), so both paths converge on identical candidate shaping."""
            for opp in found or []:
                if int(opp.chain_id) != int(self.chain_id):
                    continue
                canonical = self._canonicalize(opp, detector_name, block_number, bh, state_block, state_source)
                # Discovery provenance (MI-102B): scheduled outputs are tagged
                # "scheduled" before finalize; everything else is reactive.
                canonical.metadata.setdefault("discovery_mode", "reactive")
                if self.graph is not None and canonical.pools:
                    edges = [e for pool in canonical.pools for e in self.graph.get_edges_for_pool(pool)]
                    if edges:
                        route_blocks = [int(e.state_block) for e in edges if int(e.state_block) > 0]
                        route_statuses = {getattr(e, "state_status", "UNKNOWN") for e in edges}
                        canonical.metadata["route_state_block"] = min(route_blocks) if route_blocks else 0
                        canonical.metadata["route_state_status"] = "INVALID" if "INVALID" in route_statuses else ("UNCERTAIN" if "UNCERTAIN" in route_statuses else ("FRESH" if "FRESH" in route_statuses else "STALE"))
                        canonical.metadata["route_state_source"] = sorted({getattr(e, "state_source", "") for e in edges if getattr(e, "state_source", "")})
                candidates.append(canonical)

        for detector in self.detectors:
            try:
                interval = max(1, int(getattr(detector, "block_interval", 1) or 1))
                requires_touched = bool(getattr(detector, "requires_touched_pools", False))
                if requires_touched and not touched_pools:
                    continue
                if interval > 1 and block_number % interval != 0:
                    continue
                found = detector.on_block(block_number, touched_pools=touched_pools) if detector.name == "cycles" else detector.on_block(block_number)
                finalize(found, detector.name)
            except Exception as exc:
                get_logger("opportunity_engine").error(
                    f"Detector '{getattr(detector, 'name', type(detector).__name__)}' failed: {exc}", exc_info=True
                )

        # MI-102B: bounded event-independent pass. Runs through the same
        # finalize() shaping and the same _dedupe/_apply_validation pipeline
        # as the reactive path; a candidate reachable by both paths carries
        # one executable-route identity and cannot enter the pipeline twice.
        try:
            finalize(self._scheduled_pass_opportunities(block_number, touched_pools), None)
        except Exception as exc:
            get_logger("opportunity_engine").error(f"Scheduled discovery pass failed: {exc}", exc_info=True)
        return self._dedupe(candidates)

    # ════════════════════════════════════════════════════════════════════════════
    # MI-102B: bounded event-independent discovery
    # ════════════════════════════════════════════════════════════════════════════

    def notify_new_pools(self, pool_addresses) -> None:
        """Graph-growth hook (MI-102B §5): make newly registered pools
        eligible for the next scheduled discovery pass without waiting for
        their first swap event. Nothing is discovered here; pools are
        filtered for searchability when the next pass consumes them."""
        for addr in pool_addresses or []:
            a = (addr or "").lower()
            if a and a not in self._notified_new_pools:
                self._notified_new_pools.append(a)

    @property
    def last_scheduled_pass_block(self) -> int | None:
        """Block number where the most recent scheduled BATCH started."""
        return self._last_scheduled_block

    @property
    def last_scheduled_pass_info(self) -> dict:
        """Start stats of the most recent scheduled batch (observability)."""
        return dict(self._last_scheduled_info)

    @property
    def last_scheduled_batch_info(self) -> dict:
        """Live status of the active scheduled batch, or the completion
        summary of the last one (R1 §3.9). A partially processed batch is
        reported with ``complete: False`` — never as finished work."""
        batch = self._scheduled_batch
        if batch is not None:
            return {
                "complete": False,
                "start_block": batch["start_block"],
                "reason": batch["reason"],
                "seeds_total": len(batch["seeds"]) + batch["priority_consumed"],
                "seeds_processed": batch["seeds_processed"],
                "seeds_skipped_reactive_overlap": batch["seeds_skipped"],
                "main_pending": len(batch["seeds"]) - batch["cursor"],
                "priority_pending": len(batch["priority"]),
                "slices": batch["slices"],
            }
        return dict(self._last_batch_summary)

    def _consume_new_pool_seeds(self, limit: int | None = None) -> list[str]:
        """Bounded consumption from the LOSSLESS registration backlog (R1 §2).

        Pops at most ``limit`` (default ``max_new_pool_seeds_per_pass``)
        entries per invocation (retained overflow from earlier invocations is
        consumed first so FIFO order is preserved); the backlog itself keeps
        everything not yet popped. Registrations are filtered for valid
        searchable state — including the same minimum-liquidity gate the
        ranked pair selection applies (R1 §4). Ineligible registrations are
        dropped, not retried: they become discovery-eligible again only
        through a real state update (which touches them reactively) or
        re-registration."""
        budget = max(1, int(self.bounded_config.max_new_pool_seeds_per_pass))
        if limit is not None:
            budget = max(0, min(budget, int(limit)))
        if budget == 0:
            return []
        pending: list[str] = list(self._notified_new_pools)  # retained overflow = oldest first
        self._notified_new_pools = []
        ring = getattr(self.graph, "recently_registered", None)
        if ring is not None:
            pending.extend(ring.pop_upto(max(0, budget - len(pending))))
        searchable = []
        seen: set[str] = set()
        for p in pending:
            if p in seen:
                continue
            seen.add(p)
            if pool_is_searchable(self.graph, p):
                searchable.append(p)
        kept = searchable[:budget]
        self._notified_new_pools = searchable[len(kept):]
        return kept

    @staticmethod
    def _tag_scheduled(found, reason: str) -> None:
        for opp in found or []:
            meta = dict(opp.metadata or {})
            meta["discovery_mode"] = "scheduled"
            meta["scheduled_pass"] = True
            meta["scheduled_pass_reason"] = reason
            opp.metadata = meta

    @staticmethod
    def _p95(values: list) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]

    def _scheduled_pass_opportunities(self, block_number: int, touched_pools: set[str] | None) -> list[Opportunity]:
        """Bounded, INCREMENTAL scheduled search (R1 §3) — or nothing.

        One scheduled BATCH is active at a time; it is processed in
        deterministic slices of ``scheduled_seeds_per_slice`` cycle-search
        seeds per ``process()`` invocation, so a 20k-pool batch never
        stalls one block the way the single-pass MI-102B design did
        (measured 21.7 s). The batch state (seed list + cursor + priority
        queue) lives on the engine; no daemon, no second scheduler.

        Batch start (when no batch is active and the cadence is due or
        registrations are pending): refresh the ranked pair selection and
        run the cross-protocol pair checks ONCE (never repeated per slice;
        anchor pairs are skipped here when the reactive detector already
        checked them in the same block, keeping the block within the
        30-pair budget), then process the first slice. Newly registered
        pools join the seed queue with PRIORITY (consumed from the lossless
        backlog, bounded per invocation) ahead of the ranked-universe
        venues, which are retained until searched — nothing is discarded.

        Every invocation afterwards continues the active batch with exactly
        one slice; a batch is never started while a previous one is
        incomplete, and it completes only when the cursor AND the priority
        queue are exhausted (recorded with start AND completion info).

        Seeds the reactive detector scanned in the current block are
        dropped from the scheduled slice (no duplicate work in one block).
        """
        if not self.scheduled_discovery or self.graph is None:
            return []
        touched_lower = {p.lower() for p in (touched_pools or ())}
        out: list[Opportunity] = []
        batch = self._scheduled_batch
        if batch is None:
            ring = getattr(self.graph, "recently_registered", None)
            has_pending = (len(ring) if ring is not None else 0) > 0 or bool(self._notified_new_pools)
            due = self._last_scheduled_block is None or \
                (int(block_number) - self._last_scheduled_block) >= max(1, int(self.scheduled_pass_interval))
            if not due and not has_pending:
                return []
            reason = "cadence" if due else "new_pools"

            universe = self.pair_universe
            if universe is None:
                universe = RankedPairUniverse(self.graph, self.chain_id, self.bounded_config)
                self.pair_universe = universe
            universe.refresh(block_number)

            # Newly registered pools seed FIRST so ranking cannot starve
            # them; the TVL-ranked universe venues fill the remaining seed
            # budget. Pools the reactive path just scanned are skipped.
            new_seeds = self._consume_new_pool_seeds()
            seeds: list[str] = []
            seen: set[str] = set()
            for s in new_seeds:
                if s not in seen and s not in touched_lower:
                    seen.add(s)
                    seeds.append(s)
            for s in universe.seed_pools():
                if len(seeds) >= self.bounded_config.max_seed_pools:
                    break
                if s in seen or s in touched_lower:
                    continue
                seen.add(s)
                seeds.append(s)

            self._last_scheduled_block = int(block_number)
            self._last_scheduled_info = {
                **universe.last_stats,
                "block": int(block_number),
                "reason": reason,
                "new_pool_seeds": len(new_seeds),
                "seed_pools_selected": len(seeds),
            }

            # Pair checks run ONCE per batch. If the reactive detector
            # already checked the anchor pairs in this block (touched pools
            # present), check ONLY the supplementary pairs here so the
            # block-wide total respects the 30-pair budget (R1 §5).
            anchors_checked_reactively = bool(touched_lower)
            for detector in self.detectors:
                if getattr(detector, "name", "") == "cross_proto":
                    try:
                        if anchors_checked_reactively:
                            found = detector.on_block(block_number, extra_pairs=universe.additional_pairs(),
                                                      check_anchors=False)
                        else:
                            found = detector.on_block(block_number, extra_pairs=universe.additional_pairs())
                        self._tag_scheduled(found, reason)
                        out.extend(found or [])
                    except Exception as exc:
                        get_logger("opportunity_engine").error(
                            f"Scheduled batch start: cross-protocol detector failed: {exc}", exc_info=True
                        )

            batch = {
                "start_block": int(block_number),
                "reason": reason,
                "seeds": seeds,
                "cursor": 0,
                "priority": [],
                "priority_consumed": 0,
                "seeds_skipped": 0,
                "seeds_deduped": 0,
                "processed": set(),
                "slices": 0,
                "slice_ms": [],
                "seeds_processed": 0,
                "new_pool_seeds": len(new_seeds),
                "additional_pairs": list(universe.additional_pairs()),
                "anchors_checked_reactively": anchors_checked_reactively,
                "start_info": dict(self._last_scheduled_info),
            }
            self._scheduled_batch = batch
            get_logger("opportunity_engine").info(
                "[SCHEDULED-BATCH] start block=%s reason=%s seeds=%d new_pools=%d additional_pairs=%d "
                "slice_size=%d anchors_via_reactive=%s",
                block_number, reason, len(seeds), len(new_seeds),
                len(batch["additional_pairs"]), self.bounded_config.scheduled_seeds_per_slice,
                anchors_checked_reactively,
            )
        else:
            # Active batch: never start an overlapping sweep (R1 §3.6).
            # Mid-batch registrations join the priority queue; consumption
            # from the lossless backlog stays bounded per invocation AND the
            # staging queue itself stays bounded — the backlog lives in the
            # ring until seeds are actually about to be processed.
            cap = max(1, int(self.bounded_config.max_new_pool_seeds_per_pass))
            if len(batch["priority"]) < cap:
                taken = self._consume_new_pool_seeds(limit=cap - len(batch["priority"]))
                if taken:
                    batch["priority"].extend(taken)
                    batch["priority_consumed"] += len(taken)

        # ── one deterministic slice: priority seeds first, then the ranked
        # universe seeds; both filtered against this block's reactive work ──
        slice_budget = max(1, int(self.bounded_config.scheduled_seeds_per_slice))
        slice_seeds: list[str] = []
        t_slice = time.time()
        priority: list[str] = batch["priority"]
        while len(slice_seeds) < slice_budget and priority:
            cand = priority.pop(0)
            if cand in touched_lower or cand in slice_seeds:
                batch["seeds_skipped"] += 1
                continue
            if cand in batch["processed"]:
                batch["seeds_deduped"] += 1  # already searched earlier in this batch
                continue
            slice_seeds.append(cand)
        while len(slice_seeds) < slice_budget and batch["cursor"] < len(batch["seeds"]):
            cand = batch["seeds"][batch["cursor"]]
            batch["cursor"] += 1
            if cand in touched_lower or cand in slice_seeds:
                batch["seeds_skipped"] += 1
                continue
            if cand in batch["processed"]:
                batch["seeds_deduped"] += 1
                continue
            slice_seeds.append(cand)
        if slice_seeds:
            batch["processed"].update(slice_seeds)
        if slice_seeds:
            for detector in self.detectors:
                if getattr(detector, "name", "") == "cycles":
                    try:
                        found = detector.on_block(block_number, touched_pools=None,
                                                  scheduled_seeds=set(slice_seeds))
                        self._tag_scheduled(found, batch["reason"])
                        out.extend(found or [])
                    except Exception as exc:
                        get_logger("opportunity_engine").error(
                            f"Scheduled slice: cycle detector failed: {exc}", exc_info=True
                        )
        batch["slices"] += 1
        batch["seeds_processed"] += len(slice_seeds)
        batch["slice_ms"].append(round((time.time() - t_slice) * 1000.0, 3))

        if batch["cursor"] >= len(batch["seeds"]) and not batch["priority"]:
            self._last_batch_summary = {
                "complete": True,
                "start_block": batch["start_block"],
                "end_block": int(block_number),
                "reason": batch["reason"],
                "seeds_total": len(batch["seeds"]) + batch["priority_consumed"],
                "seeds_processed": batch["seeds_processed"],
                "seeds_skipped_reactive_overlap": batch["seeds_skipped"],
                "seeds_deduped": batch["seeds_deduped"],
                "slices": batch["slices"],
                "slice_ms_median": round(statistics.median(batch["slice_ms"]), 3) if batch["slice_ms"] else 0.0,
                "slice_ms_p95": self._p95(batch["slice_ms"]),
                "slice_ms_max": max(batch["slice_ms"], default=0.0),
                "batch_ms": round(sum(batch["slice_ms"]), 3),
                "new_pool_seeds": batch["new_pool_seeds"],
                "additional_pairs": len(batch["additional_pairs"]),
                "anchors_checked_reactively": batch["anchors_checked_reactively"],
            }
            self._scheduled_batch = None
            get_logger("opportunity_engine").info(
                "[SCHEDULED-BATCH] complete start_block=%s end_block=%s seeds=%d slices=%d batch_ms=%s "
                "slice_ms(p50/p95/max)=%s/%s/%s",
                self._last_batch_summary["start_block"], self._last_batch_summary["end_block"],
                self._last_batch_summary["seeds_processed"], self._last_batch_summary["slices"],
                self._last_batch_summary["batch_ms"], self._last_batch_summary["slice_ms_median"],
                self._last_batch_summary["slice_ms_p95"], self._last_batch_summary["slice_ms_max"],
            )
        else:
            get_logger("opportunity_engine").debug(
                "[SCHEDULED-SLICE] block=%s processed=%d main_pending=%d priority_pending=%d (batch incomplete)",
                block_number, len(slice_seeds),
                len(batch["seeds"]) - batch["cursor"], len(batch["priority"]),
            )
        return out

    @staticmethod
    def _identity_key(opp: Opportunity) -> tuple:
        """Executable-route identity (DSS-WO-ENG-017 §5).

        Two candidates are the SAME executable opportunity when they would
        produce the same transaction: same chain, same ordered token path
        (token_in = tokens[0], the borrowed asset), same ordered pool path,
        same dex set. Detector THESIS information (arb_kind, opp_type,
        detector name) is deliberately NOT part of the identity: it is
        semantic strategy classification and must never let two otherwise
        identical executable routes survive as separate candidates (or be
        queued/executed twice via different detector sources).
        """
        return (
            int(getattr(opp, "chain_id", 0) or 0),
            tuple(x.lower() for x in opp.tokens),
            tuple(x.lower() for x in opp.pools),
            tuple(sorted({x.lower() for x in opp.dexes})),
        )

    @staticmethod
    def _merge_provenance(winner: Opportunity, loser: Opportunity) -> None:
        """Absorb a consolidated candidate's detector provenance into the
        canonical opportunity (DSS-WO-ENG-017 §5): thesis and research
        information are retained, never discarded — but the winner keeps
        the strongest valid evidence and economics."""
        meta = dict(winner.metadata or {})
        provenance = dict(meta.get("detector_provenance") or {})
        name = str(loser.metadata.get("detector") or getattr(loser.arb_kind, "value", "unknown"))
        entry = dict(provenance.get(name) or {})
        entry.setdefault("arb_kind", getattr(loser.arb_kind, "value", str(loser.arb_kind)))
        entry.setdefault("opp_type", getattr(loser.opp_type, "value", str(loser.opp_type)))
        if loser.metadata.get("quote_profit_wei") is not None:
            entry["alt_quote_profit_wei"] = loser.metadata.get("quote_profit_wei")
        for research_key in ("spread_pct", "buy_protocol", "sell_protocol",
                             "curve_pool", "comparison_pool", "trigger_tx"):
            if loser.metadata.get(research_key) is not None:
                entry.setdefault(research_key, loser.metadata.get(research_key))
        provenance[name] = entry
        meta["detector_provenance"] = provenance
        meta["consolidated_theses"] = sorted(
            set(meta.get("consolidated_theses") or [])
            | {getattr(winner.arb_kind, "value", str(winner.arb_kind)),
               getattr(loser.arb_kind, "value", str(loser.arb_kind))}
        )
        winner.metadata = meta

    @classmethod
    def _dedupe(cls, candidates: list[Opportunity]) -> list[Opportunity]:
        """Consolidate candidates by EXECUTABLE-ROUTE identity (see
        _identity_key). The candidate with the strongest validated quote
        wins; every absorbed candidate's detector provenance is merged
        into the canonical opportunity instead of being discarded."""
        best = {}
        for opp in candidates:
            key = cls._identity_key(opp)
            score = float((opp.metadata or {}).get("quote_profit_wei", 0) or 0)
            cur = best.get(key)
            if cur is None:
                best[key] = (score, opp)
                continue
            if score > cur[0]:
                cls._merge_provenance(opp, cur[1])
                best[key] = (score, opp)
            else:
                cls._merge_provenance(cur[1], opp)
        return [v[1] for v in best.values()]

    def _apply_validation(self, candidates: list[Opportunity], block_number: int) -> list[Opportunity]:
        executable = [o for o in candidates if o.metadata.get("execution_supported", False)
                      and not o.metadata.get("execution_simulated", False)
                      and o.metadata.get("route_state_status") != "INVALID"]
        if not executable:
            return candidates
        from validation import validate_many
        validated = validate_many(self.w3, executable, self.chain_id,
                                  block_number=block_number, max_workers=self.validation_workers)
        by_id = {id(o): o for o in validated}
        return [by_id.get(id(o), o) for o in candidates]

    def _apply_adversarial(self, candidates: list[Opportunity], block_number: int) -> list[Opportunity]:
        targets = [o for o in candidates if o.arb_kind.value == "backrun" and o.metadata.get("execution_simulated")
                   and o.metadata.get("trigger_tx") and o.metadata.get("execution_supported", False)]
        if not targets:
            return candidates
        from validation.adversarial import AdversarialSimulator
        sim = AdversarialSimulator(self.w3, self.chain_id)
        for opp in targets:
            result = sim.simulate(opp, target_tx_hash=opp.metadata["trigger_tx"], block_number=block_number)
            meta = dict(opp.metadata)
            meta["adversarial_backend"] = result.backend
            meta["adversarial_reason"] = result.reason
            meta["adversarial_gas_units"] = result.gas_estimate
            meta["adversarial_simulated"] = result.ok
            if result.ok:
                meta["validation_status"] = ValidationStage.ADVERSARIAL_SIMULATED.value
                meta["execution_ready"] = True
                meta["validated_gas_units"] = result.gas_estimate
            else:
                meta["execution_ready"] = False
                meta["validation_status"] = ValidationStage.REJECTED.value
            opp.metadata = meta
        return candidates

    def process_candidates(self, candidates: list[Opportunity], block_number: int, *, rank_ready: bool = True, block_context=None) -> PipelineResult:
        if block_context is not None:
            bh = block_context.block_hash
            state_block = block_context.number
            state_source = "onchain:block_information"
        else:
            bh = self._context(block_number)[1]
            state_block = block_number
            state_source = "onchain:block_header"
        candidates = [self._canonicalize(o, block_number=block_number, block_hash=bh, state_block=state_block, state_source=state_source) for o in candidates]
        candidates = self._dedupe(candidates)
        discovered_count = len(candidates)
        candidates = self._apply_validation(candidates, block_number)
        candidates = self._apply_adversarial(candidates, block_number)
        ready = [o for o in candidates if o.metadata.get("execution_simulated", False)
                 and o.metadata.get("execution_ready", False)]
        rejected = [o for o in candidates if o not in ready]
        try: gas_price = int(self.w3.eth.gas_price)
        except Exception: gas_price = 0
        for opp in ready:
            meta = dict(opp.metadata)
            meta["gas_price_wei"] = gas_price
            meta["state_validated_at"] = time.time()
            opp.metadata = meta
        if ready:
            from optimizer.opportunity_economics import enrich_many
            ready = enrich_many(ready, native_price_usd=NATIVE_PRICE_USD)
            ready = [o for o in ready if o.metadata.get("economics_profitable", False)]
            if rank_ready:
                ready = rank(ready, now=time.time(), cfg=self.rank_config)[:self.max_ready]
        try:
            from log import get_logger
            get_logger("opportunity_engine").info(
                "[PIPELINE] block=%s discovered=%d ready=%d rejected=%d touched=%d",
                block_number, discovered_count, len(ready), len(rejected),
                len(getattr(block_context, "touched_pools", ()) or ()),
            )
        except Exception:
            pass
        return PipelineResult(tuple(candidates), tuple(ready), tuple(rejected))

    def process(self, block_number: int, *, touched_pools: set[str] | None = None, block_context=None) -> PipelineResult:
        return self.process_candidates(self._discover(block_number, touched_pools, block_context), block_number, block_context=block_context)

    def scan(self, block_number: int, touched_pools: set[str] | None = None) -> list[Opportunity]:
        return list(self.process(block_number, touched_pools=touched_pools).ready)
