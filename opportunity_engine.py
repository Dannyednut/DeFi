"""Single canonical opportunity pipeline.

Detector theses are distinct; this engine owns every transition after discovery.
No detector or mempool component may independently promote an opportunity to
execution-ready.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import time

from logger import Opportunity, ValidationStage
from optimizer.opportunity_ranker import rank, RankConfig
from config import NATIVE_PRICE_USD

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
        for detector in self.detectors:
            try:
                interval = max(1, int(getattr(detector, "block_interval", 1) or 1))
                requires_touched = bool(getattr(detector, "requires_touched_pools", False))
                if requires_touched and not touched_pools:
                    continue
                if interval > 1 and block_number % interval != 0:
                    continue
                found = detector.on_block(block_number, touched_pools=touched_pools) if detector.name == "cycles" else detector.on_block(block_number)
                for opp in found or []:
                    if int(opp.chain_id) != int(self.chain_id):
                        continue
                    canonical = self._canonicalize(opp, detector.name, block_number, bh, state_block, state_source)
                    if self.graph is not None and canonical.pools:
                        edges = [e for pool in canonical.pools for e in self.graph.get_edges_for_pool(pool)]
                        if edges:
                            route_blocks = [int(e.state_block) for e in edges if int(e.state_block) > 0]
                            route_statuses = {getattr(e, "state_status", "UNKNOWN") for e in edges}
                            canonical.metadata["route_state_block"] = min(route_blocks) if route_blocks else 0
                            canonical.metadata["route_state_status"] = "INVALID" if "INVALID" in route_statuses else ("UNCERTAIN" if "UNCERTAIN" in route_statuses else ("FRESH" if "FRESH" in route_statuses else "STALE"))
                            canonical.metadata["route_state_source"] = sorted({getattr(e, "state_source", "") for e in edges if getattr(e, "state_source", "")})
                    candidates.append(canonical)
            except Exception as exc:
                from log import get_logger
                get_logger("opportunity_engine").error(
                    f"Detector '{getattr(detector, 'name', type(detector).__name__)}' failed: {exc}", exc_info=True
                )
        return self._dedupe(candidates)

    @staticmethod
    def _dedupe(candidates: list[Opportunity]) -> list[Opportunity]:
        best = {}
        for opp in candidates:
            key = (opp.chain_id, opp.arb_kind.value,
                   tuple(x.lower() for x in opp.tokens),
                   tuple(x.lower() for x in opp.pools),
                   tuple(x.lower() for x in opp.dexes))
            score = float((opp.metadata or {}).get("quote_profit_wei", 0))
            cur = best.get(key)
            if cur is None or score > cur[0]:
                best[key] = (score, opp)
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
