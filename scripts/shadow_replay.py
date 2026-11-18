#!/usr/bin/env python3
"""
Offline shadow-validation replay (DSS-WO-MI-103 §5)
===================================================

Reads the append-only shadow-validation records (``logs/shadow_validation.jsonl``),
reconstructs graph state from the locally persisted cache and the recorded
per-edge state, and evaluates every recorded candidate against LATER pool
state — writing evaluation results to a SEPARATE file
(``logs/shadow_evaluation.jsonl``). The source records are never modified.

Genuinely offline: no live RPC fallback, no submissions, no fabricated
intermediate states. Where historical state is unavailable, the replay
records an evaluation GAP and its reason. Ranges beyond the runtime's own
1,000-block catch-up window (main.py ``_perform_fast_catchup``) are flagged,
never silently skipped.

Outcome classes (work order §5):
- ``still_profitable_at_later_state``      — detected and still profitable at a later state
- ``no_longer_profitable_at_later_state``  — detected but no longer profitable at a later state
- ``not_evaluable_missing_state``          — required historical/later state absent
- ``invalidated_by_reorg_evidence``        — conflicting block-hash observations for the
                                             candidate's discovery or involved-pool state

Method notes (honest scope):
- V2 legs are recomputed with the standard constant-product adapter math
  (``out = r_out*a*(10000-fee)//(r_in*10000 + a*(10000-fee))``) over the
  recorded route direction using later-state reserves — fully offline.
- V3 legs require the on-chain quoter (``optimizer/quoter.py``); no offline
  V3 recompute is attempted — such routes are declared
  ``not_evaluable_missing_state`` with that reason rather than approximated.
- Detection re-run is performed ONLY at the latest reconstructible state
  (the warmed cache tip), where graph reconstruction is genuine. Earlier
  blocks have only PARTIAL recorded state (pools observed in detections);
  re-running detection there would fabricate intermediate states, so those
  blocks are declared gaps instead.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import tempfile
import types
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPLAY_SCHEMA_VERSION = 1
DEFAULT_CATCHUP_WINDOW = 1000  # mirrors main.py _perform_fast_catchup (current_tip - cached_block > 1000)

OUTCOME_STILL = "still_profitable_at_later_state"
OUTCOME_GONE = "no_longer_profitable_at_later_state"
OUTCOME_MISSING = "not_evaluable_missing_state"
OUTCOME_REORG = "invalidated_by_reorg_evidence"


# ── offline stand-ins (same pattern as tests/test_mi102a_integration.py) ──────

class _OfflineEth:
    gas_price = 30 * 10**9

    def get_block(self, n):
        return {"hash": str(n).rjust(30, "0").encode()}


class OfflineW3:
    """Deterministic offline w3 stand-in: graph-math detectors quote via the
    Python fallback path; anything needing real chain access raises and is
    recorded as a gap, never silently skipped."""
    eth = _OfflineEth()


class _OfflineGraphClient:
    """Offline stand-in for GraphClient (subgraph unreachable offline)."""

    async def get_pools_data(self, dex_name: str, pool_addresses: list, intersect: bool = True):
        return []

    async def close(self):
        return None


# ── record loading ────────────────────────────────────────────────────────────

def read_records(path: Path) -> list:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []  # missing source → empty; the caller declares a gap
    return out


def latest_detections(records: list) -> dict:
    """fingerprint -> the FIRST (OPEN) detection record per fingerprint — the
    candidate's original detection evidence, evaluated against LATER state.
    Every snapshot still feeds the pool-state timeline; only the earliest
    detection defines the evaluated candidate (detection-to-outcome lifetime)."""
    latest = {}
    for rec in records:
        if rec.get("record_type") == "detection":
            latest.setdefault(rec["fingerprint"], rec)  # append order: OPEN first
    return latest


def build_pool_timeline(records: list) -> dict:
    """pool -> list of state snapshots {block, block_hash, edge} from recorded
    per-edge market inputs (append order; deduplicated per (pool, block, hash))."""
    timeline = collections.defaultdict(dict)
    for rec in records:
        if rec.get("record_type") != "detection":
            continue
        det = rec.get("detection") or {}
        base_block = det.get("state_block") or det.get("block_number")
        base_hash = det.get("block_hash")
        for edge in ((rec.get("market_inputs") or {}).get("edges") or []):
            block = edge.get("state_block") or base_block
            if block is None:
                continue
            key = (int(block), edge.get("state_block_hash") or base_hash or "")
            entry = {
                "block": int(block),
                "block_hash": key[1] or None,
                "edge": edge,
                "source": "recorded",
            }
            bucket = timeline[str(edge.get("pool", "")).lower()]
            existing = bucket.get(key)
            if existing is None or _later_reserve_hint(entry, existing):
                bucket[key] = entry
    return timeline


def _later_reserve_hint(new: dict, old: dict) -> bool:
    """Same (block, hash) recorded twice: keep the entry that carries reserves
    when the earlier one lacked them (still the SAME block observation)."""
    ne, oe = new["edge"], old["edge"]
    return ne.get("reserve_in") is not None and oe.get("reserve_in") is None


# ── graph reconstruction (cache + MI-102A corrected TVL restore) ─────────────

def load_warm_graph(cache_dir: str, chain_id: int):
    """Load the persisted PoolCache offline and warm a fresh TokenGraph.

    Uses the corrected restore-time TVL handling from MI-102A inside
    ``PoolCache.warm_graph`` — a replay graph that silently prunes restored
    pools is not valid evidence (work order §5)."""
    import cache as cache_mod
    from cache import PoolCache
    from graph import TokenGraph
    import tvl as tvl_mod

    cache = PoolCache(chain_id=chain_id, cache_dir=cache_dir)
    loaded = False
    try:
        with __import__("unittest").mock.patch.object(cache_mod, "GraphClient", _OfflineGraphClient):
            loaded = bool(cache.load())
    except Exception:
        loaded = False
    graph = TokenGraph(w3=None, cache=None, chain_id=chain_id)
    try:
        tvl_mod.set_graph_ref(graph)
    except Exception:
        pass
    warmed = cache.warm_graph(graph) if loaded else 0
    cache_state_block = None
    try:
        blocks = [int(e.state_block) for e in graph.all_edges() if getattr(e, "state_block", 0)]
        cache_state_block = max(blocks) if blocks else None
    except Exception:
        cache_state_block = None
    return graph, cache, warmed, cache_state_block


# ── later-state resolution ────────────────────────────────────────────────────

def resolve_later_states(pools: list, detected_block: int, timeline: dict,
                         graph, cache_state_block,
                         window: int = DEFAULT_CATCHUP_WINDOW) -> tuple:
    """Resolve ONE COHERENT later-state snapshot for the whole route — never a
    per-pool mix from unrelated blocks (a composite of states from different
    blocks would fabricate an intermediate state; work order §5 "no fabricated
    intermediate states").

    Resolution order:
    1. exact coherent recorded snapshot — the LATEST block > detection where
       EVERY involved pool has a recorded state observation;
    2. windowed coherent recorded snapshot — the latest anchor block where
       every involved pool has a recorded observation within the runtime's own
       catch-up window below the anchor (freshness semantics mirroring the
       runtime catch-up path); per-pool state lag is reported;
    3. coherent cache fallback — every involved pool resolved from the warmed
       cache (MI-102A TVL restore) with cache_state_block > detection.

    Returns (resolved: {pool: entry}, reason: str | None).
    """
    pool_entries = {}
    for p in pools:
        pool_entries[p] = sorted(
            (e for e in (timeline.get(str(p).lower()) or {}).values()
             if e["block"] > detected_block),
            key=lambda e: (e["block"], e.get("block_hash") or ""))

    def _pick(anchor: int):
        resolved, lags = {}, {}
        for p in pools:
            entries = [e for e in pool_entries[p] if e["block"] <= anchor
                       and anchor - e["block"] <= window]
            if entries:
                e = entries[-1]
                resolved[p] = e
                lags[p] = anchor - e["block"]
            elif cache_state_block and cache_state_block > detected_block \
                    and abs(anchor - cache_state_block) <= window:
                cache_entry = _cache_entry(graph, p)
                if cache_entry:
                    resolved[p] = cache_entry
                    lags[p] = abs(anchor - cache_state_block)
            else:
                return None, None
        return resolved, lags

    anchors = sorted({e["block"] for entries in pool_entries.values() for e in entries},
                     reverse=True)
    for anchor in anchors:
        # exact coherent snapshot first: every pool observed AT the anchor
        if all(any(e["block"] == anchor for e in pool_entries[p]) for p in pools):
            resolved = {p: [e for e in pool_entries[p] if e["block"] == anchor][-1]
                        for p in pools}
            return resolved, None
        resolved, lags = _pick(anchor)
        if resolved is not None:
            resolved = {p: {**e, "state_lag_from_anchor": lags[p]} for p, e in resolved.items()}
            return resolved, None

    # coherent cache-only fallback (no recorded later snapshot at all)
    if cache_state_block and cache_state_block > detected_block and pools:
        resolved = {}
        for p in pools:
            e = _cache_entry(graph, p)
            if not e:
                return {}, "no coherent later-state snapshot: recorded states exist for other pools only"
            resolved[p] = e
        return resolved, None

    if any(pool_entries[p] for p in pools):
        return {}, ("no coherent later-state snapshot covering ALL involved pools; a "
                    "mixed-block composite would fabricate intermediate state")
    return {}, None


def _cache_entry(graph, pool) -> Optional[dict]:
    """One involved pool's state from the warmed cache (state_block carried by
    the restored edge)."""
    try:
        edges = graph.get_edges_for_pool(pool) or []
    except Exception:
        edges = []
    for e in edges:
        sb = int(getattr(e, "state_block", 0) or 0)
        if sb:
            return {
                "block": sb,
                "block_hash": getattr(e, "state_block_hash", None) or None,
                "edge": {
                    "pool": str(getattr(e, "pool_address", pool)).lower(),
                    "token_in": str(getattr(e, "token_in", "")).lower(),
                    "token_out": str(getattr(e, "token_out", "")).lower(),
                    "reserve_in": getattr(e, "reserve_in", None),
                    "reserve_out": getattr(e, "reserve_out", None),
                    "fee_bps": getattr(e, "fee_bps", None),
                    "version": getattr(e, "version", None),
                    "protocol": getattr(e, "protocol", None),
                    "state_source": getattr(e, "state_source", None),
                    "state_status": getattr(e, "state_status", None),
                },
                "source": "cache_warm_graph",
            }
    return None


def detect_reorg_evidence(fingerprint_rec: dict, involved_pools: list, timeline: dict,
                          records: list, relevant_blocks: set) -> dict:
    """Conflicting block-hash observations RELEVANT to this candidate — its
    discovery block or any involved pool's state blocks it actually relies on
    (work order §4.5/§5): replay evidence only — no runtime reorg handling.
    Conflicts at blocks the candidate does not rely on are preserved as
    observations but do not invalidate it."""
    conflicts = []
    det = fingerprint_rec.get("detection") or {}
    d_block = det.get("block_number")
    d_hash = det.get("block_hash")
    hashes_by_block = collections.defaultdict(set)
    for rec in records:
        d = (rec.get("detection") or {})
        if d.get("block_number") is not None and d.get("block_hash"):
            hashes_by_block[int(d["block_number"])].add(str(d["block_hash"]))
    if d_block is not None and d_hash and int(d_block) in relevant_blocks \
            and len(hashes_by_block.get(int(d_block), set())) > 1:
        conflicts.append({
            "scope": "discovery_block",
            "block": int(d_block),
            "observed_hashes": sorted(hashes_by_block[int(d_block)]),
        })
    for pool in involved_pools:
        p = str(pool).lower()
        by_block = collections.defaultdict(set)
        for entry in (timeline.get(p) or {}).values():
            if entry.get("block_hash") and entry["block"] in relevant_blocks:
                by_block[entry["block"]].add(entry["block_hash"])
        for block, hs in by_block.items():
            if len(hs) > 1:
                conflicts.append({
                    "scope": "pool_state",
                    "pool": p,
                    "block": block,
                    "observed_hashes": sorted(hs),
                })
                break
    return {"invalidated": bool(conflicts), "conflicts": conflicts}


# ── later-state profitability recompute (existing adapter math, offline) ─────

def _v2_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    """Standard constant-product adapter math generalized to fee_bps
    (UniswapV2 getAmountOut with fee = fee_bps/10000)."""
    fee = max(0, min(9999, int(fee_bps if fee_bps is not None else 30)))
    a = int(amount_in) * (10_000 - fee)
    return (int(reserve_out) * a) // (int(reserve_in) * 10_000 + a)


def recompute_route_profit(route_edges: list, amount_in: int, later_states: dict) -> dict:
    """Recompute the route profit at the later state using the recorded route
    direction. Returns {evaluable, profit_wei|None, reason, legs}."""
    amt = int(amount_in)
    if amt <= 0:
        return {"evaluable": False, "profit_wei": None,
                "reason": "no recorded trade size; profitability not inferred from missing data",
                "legs": []}
    legs = []
    for edge in route_edges:
        pool = str(edge.get("pool", "")).lower()
        later = later_states.get(pool)
        if later is None:
            return {"evaluable": False, "profit_wei": None,
                    "reason": f"no later state for involved pool {pool}",
                    "legs": legs}
        le = later["edge"]
        version = int(le.get("version") or edge.get("version") or 2)
        if version != 2:
            return {"evaluable": False, "profit_wei": None,
                    "reason": ("V3 leg requires the on-chain quoter; offline recompute is "
                               "not attempted rather than approximated"),
                    "legs": legs}
        r_in, r_out = le.get("reserve_in"), le.get("reserve_out")
        if not r_in or not r_out:
            return {"evaluable": False, "profit_wei": None,
                    "reason": f"later state for {pool} lacks reserves",
                    "legs": legs}
        amt = _v2_amount_out(amt, int(r_in), int(r_out), le.get("fee_bps"))
        legs.append({"pool": pool, "later_state_block": later["block"],
                     "later_state_hash": later.get("block_hash"),
                     "state_source": later.get("source")})
    profit = amt - int(amount_in)
    return {"evaluable": True, "profit_wei": profit, "reason": None, "legs": legs}


# ── bounded detection re-run at the latest reconstructible state ──────────────

def rerun_detection(graph, chain_id: int, records: list) -> dict:
    """Re-run detection ONCE at the warmed cache tip (the only state block
    where graph reconstruction is genuine) and compare against recorded
    fingerprints. Failures are returned as gaps, never silently skipped."""
    try:
        from detectors.cycles import CycleDetector
        from opportunity_engine import OpportunityEngine
        import detectors.base as detectors_base
        import detectors.cycles as cycles_mod

        # Offline: force the Python fallback quote path (no on-chain ArbExec
        # quoting) — this process-local flag does not change any repository
        # runtime behaviour.
        prev_exec = getattr(cycles_mod, "ARB_EXEC_ADDRESS", "")
        try:
            cycles_mod.ARB_EXEC_ADDRESS = ""
            detectors_base.ARB_EXEC_ADDRESS = ""
            detector = CycleDetector(OfflineW3(), graph, None, None, chain_id)
            engine = OpportunityEngine(w3=OfflineW3(), chain_id=chain_id,
                                       detectors=[detector], graph=graph)
            try:
                touched = {str(e.pool_address).lower() for e in graph.all_edges()}
            except Exception:
                touched = set()
            recorded_fps = {r["fingerprint"] for r in records if r.get("record_type") == "detection"}
            try:
                tip_block = max(int(e.state_block) for e in graph.all_edges()
                                if getattr(e, "state_block", 0))
            except Exception:
                tip_block = 0
            ctx = types.SimpleNamespace(block_hash="", number=tip_block,
                                        touched_pools=frozenset(touched))
            result = engine.process(tip_block, touched_pools=frozenset(touched), block_context=ctx)
            redetected = {r["fingerprint"] for r in
                          (result.candidates or ())} if result else set()
            return {
                "ran": True,
                "replay_block": tip_block,
                "candidates_detected": len(redetected),
                "recorded_fingerprints": len(recorded_fps),
                "still_detected": sorted(recorded_fps & redetected),
                "gaps": [],
            }
        finally:
            cycles_mod.ARB_EXEC_ADDRESS = prev_exec
            try:
                detectors_base.ARB_EXEC_ADDRESS = prev_exec
            except Exception:
                pass
    except Exception as exc:
        return {"ran": False, "replay_block": None, "candidates_detected": 0,
                "recorded_fingerprints": 0, "still_detected": [],
                "gaps": [{"kind": "redetection_unavailable", "block_from": None,
                          "block_to": None, "reason": f"detection re-run failed offline: {exc}"}]}


# ── evaluation core ───────────────────────────────────────────────────────────

def evaluate(shadow_path: Path, evaluation_path: Path, cache_dir: str,
             chain_id: int, catchup_window: int, rerun: bool) -> dict:
    records = read_records(shadow_path)
    source_sha = hashlib.sha256(shadow_path.read_bytes()).hexdigest() if shadow_path.exists() else None
    timeline = build_pool_timeline(records)
    latest = latest_detections(records)
    graph, cache, warmed, cache_state_block = load_warm_graph(cache_dir, chain_id)

    run_id = uuid.uuid4().hex[:12]
    evaluated = 0
    outcomes = collections.Counter()
    beyond_window = 0
    eval_records = []
    gap_records = []
    state_blocks = sorted(
        {e["block"] for entries in timeline.values() for e in entries.values()}
        | ({cache_state_block} if cache_state_block else set()))

    for fp, rec in sorted(latest.items(), key=lambda kv: ((kv[1].get("detection") or {}).get("block_number") or 0, kv[0])):
        det = rec.get("detection") or {}
        det_block = int(det.get("block_number") or 0)
        ident = rec.get("identity") or {}
        pools = [str(p).lower() for p in (ident.get("pool_addresses") or [])]
        route_edges = ((rec.get("market_inputs") or {}).get("edges") or [])
        amount_in = (rec.get("size_and_profit") or {}).get("trade_size_amount_in")
        later, resolve_reason = resolve_later_states(pools, det_block, timeline, graph,
                                                     cache_state_block, catchup_window)
        relevant_blocks = {det_block} | {s["block"] for s in later.values()}
        if det.get("state_block") is not None:
            relevant_blocks.add(int(det["state_block"]))
        reorg = detect_reorg_evidence(rec, pools, timeline, records, relevant_blocks)

        record = {
            "record_type": "shadow_evaluation",
            "schema_version": REPLAY_SCHEMA_VERSION,
            "run_id": run_id,
            "fingerprint": fp,
            "origin": ident.get("origin"),
            "detected_block": det_block or None,
            "detected_seq": rec.get("seq"),
            "recorded_gross_quote_profit_wei": (rec.get("size_and_profit") or {}).get("gross_quote_profit_wei"),
            "recorded_route_edges": [e.get("pool") for e in route_edges] or None,
            "later_state_blocks": {p: s["block"] for p, s in later.items()} or None,
            "later_state_hashes": {p: s.get("block_hash") for p, s in later.items() if s.get("block_hash")} or None,
        }

        if reorg["invalidated"]:
            record.update({"outcome": OUTCOME_REORG, "reason":
                           "conflicting block-hash observations for the discovery block or an involved pool",
                           "reorg_evidence": reorg["conflicts"],
                           "recomputed_gross_profit_wei": None, "state_lag_blocks": None,
                           "beyond_catchup_window": False})
        else:
            missing = [p for p in pools if p not in later]
            recompute = recompute_route_profit(route_edges, amount_in, later) if (pools and amount_in) else {
                "evaluable": False, "profit_wei": None,
                "reason": ("no recorded trade size; profitability not inferred from missing data"
                           if pools and not amount_in else "no involved-pool state recorded"),
                "legs": []}
            if not recompute["evaluable"]:
                reason = recompute["reason"]
                if resolve_reason:
                    reason = (resolve_reason + "; " if reason else "") + resolve_reason
                if missing:
                    reason = (reason + "; " if reason else "") + \
                        f"no later state for: {', '.join(missing)}"
                record.update({"outcome": OUTCOME_MISSING, "reason": reason,
                               "recomputed_gross_profit_wei": None, "state_lag_blocks": None,
                               "beyond_catchup_window": False, "missing_pools": missing or None})
            else:
                lag = max(s["block"] for s in later.values()) - det_block
                rec_profit = (rec.get("size_and_profit") or {}).get("gross_quote_profit_wei")
                outcome = OUTCOME_STILL if recompute["profit_wei"] > 0 else OUTCOME_GONE
                beyond = lag > catchup_window
                beyond_window += 1 if beyond else 0
                record.update({
                    "outcome": outcome,
                    "reason": None,
                    "recomputed_gross_profit_wei": recompute["profit_wei"],
                    "recompute_basis": "constant_product_v2_adapter_math_offline",
                    "recompute_legs": recompute["legs"],
                    "state_lag_blocks": lag,
                    "beyond_catchup_window": beyond,
                    "beyond_catchup_window_note": (
                        "later state is older than the runtime's own 1,000-block catch-up "
                        "window would sync; evaluation retained and flagged, not skipped"
                    ) if beyond else None,
                    "recorded_vs_recomputed_delta_wei": (
                        recompute["profit_wei"] - int(rec_profit)) if isinstance(rec_profit, int) else None,
                })
        outcomes[record["outcome"]] += 1
        evaluated += 1
        eval_records.append(record)

    # gap scan: consecutive stretches of the state timeline beyond the window
    gaps = []
    for a, b in zip(state_blocks, state_blocks[1:]):
        if b - a > catchup_window:
            gaps.append({"record_type": "shadow_evaluation_gap", "schema_version": REPLAY_SCHEMA_VERSION,
                         "run_id": run_id, "kind": "state_gap", "block_from": a, "block_to": b,
                         "gap_blocks": b - a,
                         "reason": (f"no recorded or cached pool state within the {catchup_window}-block "
                                    "catch-up window between these state snapshots; the range is "
                                    "declared a gap, not silently skipped")})
    if not records:
        gaps.append({"record_type": "shadow_evaluation_gap", "schema_version": REPLAY_SCHEMA_VERSION,
                     "run_id": run_id, "kind": "empty_source", "block_from": None, "block_to": None,
                     "gap_blocks": None, "reason": "the shadow log contains no records; nothing to evaluate"})

    redetection = rerun_detection(graph, chain_id, records) if rerun else {
        "ran": False, "replay_block": None, "candidates_detected": 0,
        "recorded_fingerprints": 0, "still_detected": [],
        "gaps": [{"record_type": "shadow_evaluation_gap", "schema_version": REPLAY_SCHEMA_VERSION,
                  "run_id": run_id, "kind": "redetection_skipped", "block_from": None,
                  "block_to": None, "reason": "detection re-run disabled (--no-rerun-detection)"}]}
    gap_records.extend(redetection.get("gaps") or [])

    # Historical blocks before the earliest full-state snapshot: only partial
    # (recorded) state exists — re-running detection there would fabricate
    # intermediate states, so they are declared gaps explicitly.
    earliest_state = state_blocks[0] if state_blocks else None
    if earliest_state and latest:
        earliest_detection = min((r.get("detection") or {}).get("block_number") or 0
                                 for r in latest.values())
        if earliest_detection < earliest_state:
            gap_records.append({
                "record_type": "shadow_evaluation_gap", "schema_version": REPLAY_SCHEMA_VERSION,
                "run_id": run_id, "kind": "partial_state_range",
                "block_from": earliest_detection, "block_to": earliest_state - 1,
                "gap_blocks": earliest_state - earliest_detection,
                "reason": ("only partial recorded per-edge state exists for blocks before the "
                           "earliest full graph snapshot; detection re-run would fabricate "
                           "intermediate states — candidates there are evaluated by later-state "
                           "recompute only")})

    header = {
        "record_type": "shadow_evaluation_run",
        "schema_version": REPLAY_SCHEMA_VERSION,
        "run_id": run_id,
        "evaluated_at_unix": __import__("time").time(),
        "source_log": str(shadow_path),
        "source_sha256": source_sha,
        "source_unchanged_promise": "the replay opens the source log read-only and never rewrites it",
        "cache_dir": str(cache_dir),
        "chain_id": chain_id,
        "catchup_window": catchup_window,
        "cache_loaded": bool(warmed) or cache_state_block is not None,
        "warm_graph_pools_restored": warmed,
        "cache_state_block": cache_state_block,
        "tvl_restore": "PoolCache.warm_graph (MI-102A corrected restore-time TVL handling)",
        "candidates_evaluated": evaluated,
        "outcome_counts": dict(outcomes),
        "beyond_catchup_window_count": beyond_window,
        "gap_count": len(gap_records) + len(gaps),
        "redetection": {k: v for k, v in redetection.items() if k != "gaps"},
        "unavailable": {
            "v3_offline_recompute": "V3 legs require the on-chain quoter; declared not evaluable",
            "full_historical_detection_rerun": "only the latest reconstructible state supports a genuine re-run",
            "searched_universe_denominator": "searched-universe exposure is not persisted at the recording points",
        },
    }

    evaluation_path.parent.mkdir(parents=True, exist_ok=True)
    gap_count = len(gap_records) + len(gaps)
    with open(evaluation_path, "w", encoding="utf-8") as f:  # derived output; source untouched
        f.write(json.dumps(header, separators=(",", ":"), default=str) + "\n")
        for rec in eval_records + gap_records + gaps:
            f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        f.flush()
    return header


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline shadow-validation replay (DSS-WO-MI-103 §5)")
    ap.add_argument("--shadow-log", default="logs/shadow_validation.jsonl")
    ap.add_argument("--evaluation-log", default="logs/shadow_evaluation.jsonl")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--chain-id", type=int, default=1)
    ap.add_argument("--catchup-window", type=int, default=DEFAULT_CATCHUP_WINDOW)
    ap.add_argument("--no-rerun-detection", action="store_true")
    args = ap.parse_args()

    try:
        from config import CACHE_DIR
        cache_dir = args.cache_dir or CACHE_DIR
    except Exception:
        cache_dir = args.cache_dir or "./cache"

    shadow_path = Path(args.shadow_log)
    if not shadow_path.exists():
        print(f"[shadow-replay] source log not found: {shadow_path}", file=sys.stderr)
        return 2
    header = evaluate(shadow_path, Path(args.evaluation_log), cache_dir,
                      args.chain_id, args.catchup_window, not args.no_rerun_detection)
    print(f"[shadow-replay] run {header['run_id']}: candidates={header['candidates_evaluated']} "
          f"outcomes={header['outcome_counts']} gaps={header['gap_count']} "
          f"beyond_window={header['beyond_catchup_window_count']} "
          f"source_sha256={str(header['source_sha256'])[:16]}…")
    print(f"[shadow-replay] evaluation written to {args.evaluation_log} (source log untouched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
