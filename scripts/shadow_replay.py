#!/usr/bin/env python3
"""
Offline shadow-validation replay (DSS-WO-MI-103 §5, R1 corrections)
===================================================================

Reads the append-only shadow-validation records (``logs/shadow_validation.jsonl``),
reconstructs graph state from the locally persisted cache and the recorded
per-edge state, and evaluates every recorded candidate LIFECYCLE against
LATER pool state — writing evaluation results to a SEPARATE file
(``logs/shadow_evaluation.jsonl``). The source records are never modified.

R1 corrections implemented here:
- §1 Source immutability: the ENTIRE replay (including the optional detection
  re-run) executes with an explicitly DISABLED shadow recorder installed
  process-wide; the prior recorder state is restored afterwards. No
  persistent environment or repository runtime configuration is altered, and
  the live recording integration is untouched.
- §2 Exact route recompute: ordered route legs are reconstructed from the
  recorded ``identity.ordered_token_path`` + ``identity.pool_addresses``;
  each hop consumes exactly ONE direction-specific market-state edge matching
  its pool and token_in→token_out. Opposite directed edges are never collapsed
  and a missing directed edge is declared not-evaluable — the reverse edge or
  an unrelated pool edge is never substituted. V3 stays explicitly
  not-evaluable (no concentrated-liquidity approximation).
- §3 Chain isolation: replay inputs are filtered to the requested ``chain_id``
  before candidate selection, timeline construction, reorg-hash comparison
  and evaluation; other-chain observations are preserved in the source and
  counted in the run header.
- §4 Coherent later-state fallback: an explicit COMMON ANCHOR is resolved for
  the route; EVERY leg's selected state (recorded or warmed-cache) must be
  later than the detection block and within the catch-up window of that
  anchor; each pool's actual state block and lag from the anchor is reported.
  A graph-wide maximum block never establishes freshness for a pool.
- §8 Lifecycle identity: evaluations are keyed by
  (fingerprint, lifecycle open_seq); UPDATE records belong to the OPEN they
  follow; CLOSE records are associated with their matching lifecycle; a
  lifecycle whose OPEN is outside the recorded history keeps the fallback
  classification without fabricating opening evidence.

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
from unittest import mock

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


# ── record loading, chain isolation (R1 §3) and lifecycle identity (R1 §8) ────

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


def _record_chain(rec: dict):
    """The chain a record belongs to (identity for detections, terminal for
    lifecycle closes)."""
    if rec.get("record_type") == "detection":
        return (rec.get("identity") or {}).get("chain_id")
    if rec.get("record_type") == "lifecycle":
        return (rec.get("terminal") or {}).get("chain_id")
    return None


def filter_by_chain(records: list, chain_id: int) -> tuple:
    """R1 §3: replay inputs are restricted to the requested chain BEFORE any
    candidate selection, state-timeline construction, reorg-hash comparison
    or evaluation — pool addresses and block numbers are not globally unique
    across EVM chains. Other-chain records are preserved in the source and
    only excluded from THIS replay's inputs."""
    selected = [r for r in records if _record_chain(r) == chain_id]
    return selected, len(records) - len(selected)


def lifecycle_groups(records: list) -> list:
    """R1 §8: group detection records into DISTINCT lifecycles keyed by
    (fingerprint, lifecycle open sequence). An OPEN starts a lifecycle; UPDATE
    records belong to the OPEN sequence they follow and never create separate
    candidates; a CLOSE is associated with the lifecycle it terminates. A
    lifecycle whose OPEN is outside the recorded history is kept with
    ``open_seq=None`` and ``open_evidence="outside_recorded_history"`` — its
    missing opening evidence is never fabricated."""
    groups = {}
    current: dict = {}  # fingerprint -> open_seq of its current lifecycle
    for rec in records:
        rtype = rec.get("record_type")
        fp = rec.get("fingerprint")
        if not fp:
            continue
        if rtype == "detection":
            event = rec.get("event")
            if event == "OPEN":
                seq = rec.get("seq")
                current[fp] = seq
                groups.setdefault((fp, seq), {
                    "fingerprint": fp, "open_seq": seq, "open": rec,
                    "snapshots": [], "close": None,
                    "open_evidence": "recorded"})
            elif event == "UPDATE":
                seq = current.get(fp)
                g = groups.get((fp, seq))
                if g is None:
                    g = {"fingerprint": fp, "open_seq": seq, "open": None,
                         "snapshots": [], "close": None,
                         "open_evidence": "outside_recorded_history"}
                    groups[(fp, seq)] = g
                g["snapshots"].append(rec)
        elif rtype == "lifecycle" and rec.get("event") == "CLOSE":
            seq = current.get(fp)
            g = groups.get((fp, seq))
            if g is None:
                g = {"fingerprint": fp, "open_seq": seq, "open": None,
                     "snapshots": [], "close": None,
                     "open_evidence": "outside_recorded_history"}
                groups[(fp, seq)] = g
            g["close"] = rec
            current[fp] = None  # closed; a later OPEN starts a NEW lifecycle
    return list(groups.values())


def build_pool_timeline(records: list) -> dict:
    """pool -> {(block, hash, token_in, token_out): entry} from recorded
    per-edge market inputs. Direction-specific: opposite directed edges of a
    pool are NEVER collapsed into one pool-level entry (R1 §2) — a route leg
    consumes reserves from exactly its own direction."""
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
            tin = str(edge.get("token_in", "")).lower()
            tout = str(edge.get("token_out", "")).lower()
            key = (int(block), edge.get("state_block_hash") or base_hash or "", tin, tout)
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
    """Same (block, hash, direction) recorded twice: keep the entry that
    carries reserves when the earlier one lacked them (still the SAME block
    observation)."""
    ne, oe = new["edge"], old["edge"]
    return ne.get("reserve_in") is not None and oe.get("reserve_in") is None


def route_legs(det_record: dict) -> Optional[list]:
    """R1 §2: reconstruct the EXACT ordered executable route from the recorded
    identity — one leg per ordered hop: (pool, token_in, token_out). Returns
    None when the ordered token path is not recorded or does not fit the pool
    list: legs are never guessed, because a wrong hop would fabricate a route
    that was never detected."""
    ident = det_record.get("identity") or {}
    pools = [str(p) for p in (ident.get("pool_addresses") or [])]
    tokens = [str(t) for t in (ident.get("ordered_token_path") or [])]
    if not pools or not tokens or len(tokens) != len(pools) + 1:
        return None
    return [(pools[i].lower(), tokens[i].lower(), tokens[i + 1].lower())
            for i in range(len(pools))]


# ── graph reconstruction (cache + MI-102A corrected TVL restore) ─────────────

def _iter_graph_edges(graph):
    """Iterate every directed edge of a warmed TokenGraph. TokenGraph exposes
    its adjacency privately (``_edges`` keyed by token_in); the replay only
    READS it. An unexpected graph shape yields nothing — the detection re-run
    then declares a gap instead of guessing."""
    adjacency = getattr(graph, "_edges", {}) or {}
    for edges in adjacency.values():
        for e in edges or []:
            yield e


def load_warm_graph(cache_dir: str, chain_id: int):
    """Load the persisted PoolCache offline and warm a fresh TokenGraph.

    Uses the corrected restore-time TVL handling from MI-102A inside
    ``PoolCache.warm_graph`` — a replay graph that silently prunes restored
    pools is not valid evidence (work order §5). Token price anchors are
    seeded the same way main.py seeds them (tvl.init_prices via the chain's
    wrapped native) so the detection re-run's fallback quote behaves like the
    runtime's; failures leave the re-run to declare a gap."""
    import cache as cache_mod
    from cache import PoolCache
    from graph import TokenGraph
    import tvl as tvl_mod

    try:
        from config import NATIVE_PRICE_USD, get_chain
        chain = get_chain()
        tvl_mod.init_prices(NATIVE_PRICE_USD, chain.wrapped_native, chain.stablecoins)
    except Exception:
        pass  # offline price seeding unavailable → re-run gaps, never guesses

    cache = PoolCache(chain_id=chain_id, cache_dir=cache_dir)
    loaded = False
    try:
        with mock.patch.object(cache_mod, "GraphClient", _OfflineGraphClient):
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
        blocks = [int(e.state_block) for e in _iter_graph_edges(graph)
                  if getattr(e, "state_block", 0)]
        cache_state_block = max(blocks) if blocks else None
    except Exception:
        cache_state_block = None
    return graph, cache, warmed, cache_state_block


# ── later-state resolution: coherent common anchor over EXACT route legs ──────

def _cache_entry_directed(graph, pool: str, token_in: str, token_out: str) -> Optional[dict]:
    """The warmed-cache state for ONE pool in EXACTLY the requested direction.
    The reverse edge or any unrelated edge is never returned (R1 §2.5)."""
    try:
        edges = graph.get_edges_for_pool(pool) or []
    except Exception:
        edges = []
    for e in edges:
        if str(getattr(e, "token_in", "")).lower() != token_in \
                or str(getattr(e, "token_out", "")).lower() != token_out:
            continue
        sb = int(getattr(e, "state_block", 0) or 0)
        if not sb:
            continue
        return {
            "block": sb,
            "block_hash": getattr(e, "state_block_hash", None) or None,
            "edge": {
                "pool": str(getattr(e, "pool_address", pool)).lower(),
                "token_in": token_in,
                "token_out": token_out,
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


def resolve_later_states(legs: list, detected_block: int, timeline: dict,
                         graph, cache_state_block,
                         window: int = DEFAULT_CATCHUP_WINDOW) -> tuple:
    """Resolve ONE COHERENT later-state snapshot for the route's EXACT ordered
    legs (R1 §2 + §4). Every leg must be satisfied by its direction-specific
    state; an explicit COMMON ANCHOR is resolved and EVERY selected state —
    recorded OR warmed-cache — must be (a) later than the detection block and
    (b) within ``window`` blocks of the anchor. A graph-wide maximum block
    never establishes freshness for an individual pool.

    Resolution order:
    1. exact coherent recorded snapshot — the LATEST block where EVERY leg has
       a recorded direction-specific observation AT that block;
    2. windowed coherent recorded snapshot — the latest anchor where every leg
       has a recorded observation within the runtime's own catch-up window
       below the anchor (cache fallback per leg allowed under the same rule);
    3. coherent cache-only fallback — every leg resolved from the warmed cache
       (MI-102A TVL restore), anchored at the MAXIMUM of the legs' OWN cache
       state blocks, each later than detection and inside the window.

    Returns (resolved: {leg_index: entry}, reason: str | None, anchor: int | None).
    Each entry carries ``state_lag_from_anchor``.
    """
    leg_entries = []
    for (pool, tin, tout) in legs:
        entries = sorted(
            (e for k, e in (timeline.get(str(pool).lower()) or {}).items()
             if k[2] == tin and k[3] == tout and e["block"] > detected_block),
            key=lambda e: (e["block"], e.get("block_hash") or ""))
        leg_entries.append(entries)

    def _lagged(i: int, entry: dict, anchor: int) -> dict:
        e = dict(entry)
        e["state_lag_from_anchor"] = anchor - e["block"]
        return e

    def _pick(anchor: int):
        resolved = {}
        for i, (pool, tin, tout) in enumerate(legs):
            cand = [e for e in leg_entries[i]
                    if e["block"] <= anchor and anchor - e["block"] <= window]
            if cand:
                resolved[i] = _lagged(i, cand[-1], anchor)
                continue
            # cache fallback for this leg — same common-anchor discipline:
            # the pool's OWN cache state must be later than detection and
            # within the window of the anchor (R1 §4).
            if cache_state_block and abs(anchor - cache_state_block) <= window:
                ce = _cache_entry_directed(graph, pool, tin, tout)
                if ce and ce["block"] > detected_block \
                        and anchor - ce["block"] <= window:
                    resolved[i] = _lagged(i, ce, anchor)
                    continue
            return None
        return resolved

    anchors = sorted({e["block"] for entries in leg_entries for e in entries},
                     reverse=True)
    for anchor in anchors:
        if all(any(e["block"] == anchor for e in leg_entries[i])
               for i in range(len(legs))):
            resolved = {}
            for i in range(len(legs)):
                exact = [e for e in leg_entries[i] if e["block"] == anchor][-1]
                resolved[i] = _lagged(i, exact, anchor)
            return resolved, None, anchor
        resolved = _pick(anchor)
        if resolved is not None:
            return resolved, None, anchor

    # coherent cache-only fallback (no recorded later snapshot at all):
    # anchor = the MAXIMUM of the legs' OWN cache state blocks — never a
    # graph-wide maximum (R1 §4).
    cache_cands = []
    for (pool, tin, tout) in legs:
        ce = _cache_entry_directed(graph, pool, tin, tout)
        if not ce:
            return {}, (f"coherent cache fallback failed: the warmed cache has no "
                        f"state for the exact directed leg {pool} {tin}->{tout}; the "
                        "reverse direction or an unrelated pool edge is never "
                        "substituted"), None
        cache_cands.append(ce)
    anchor = max(e["block"] for e in cache_cands)
    stale = [f"{legs[i][0]} {legs[i][1]}->{legs[i][2]} (state block {e['block']})"
             for i, e in enumerate(cache_cands)
             if e["block"] <= detected_block or anchor - e["block"] > window]
    if stale:
        return {}, ("coherent cache fallback rejected by the common-anchor rule: "
                    + "; ".join(stale)
                    + f" (detection block {detected_block}, anchor {anchor}, "
                      f"window {window})"), anchor
    resolved = {i: _lagged(i, e, anchor) for i, e in enumerate(cache_cands)}
    return resolved, None, anchor


def detect_reorg_evidence(det_record: dict, involved_pools: list, timeline: dict,
                          records: list, relevant_blocks: set) -> dict:
    """Conflicting block-hash observations RELEVANT to this candidate — its
    discovery block or any involved pool's state blocks it actually relies on
    (work order §4.5/§5): replay evidence only — no runtime reorg handling.
    Conflicts at blocks the candidate does not rely on are preserved as
    observations but do not invalidate it. ``records`` is already
    chain-filtered (R1 §3): hashes from other chains can never trigger a
    false invalidation."""
    conflicts = []
    det = det_record.get("detection") or {}
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


def recompute_route_profit(amount_in, legs: list, resolved: dict) -> dict:
    """Recompute the route profit at the later state over EXACTLY the ordered
    route legs (R1 §2): one leg per hop, exact direction, evaluated exactly
    once per pool. A missing directed edge makes the candidate not evaluable —
    the reverse direction or an unrelated pool edge is never substituted. V3
    legs stay explicitly not-evaluable (no concentrated-liquidity
    approximation)."""
    if amount_in is None or int(amount_in) <= 0:
        return {"evaluable": False, "profit_wei": None,
                "reason": "no recorded trade size; profitability not inferred from missing data",
                "legs": []}
    amt = int(amount_in)
    leg_evidence = []
    for i, (pool, tin, tout) in enumerate(legs):
        later = resolved.get(i)
        if later is None:
            return {"evaluable": False, "profit_wei": None,
                    "reason": (f"no later state for directed route leg {i} "
                               f"{pool} {tin}->{tout}; the exact directed edge is "
                               "required and the reverse direction or an unrelated "
                               "pool edge is never substituted"),
                    "legs": leg_evidence}
        le = later["edge"]
        version = int(le.get("version") or 2)
        if version != 2:
            return {"evaluable": False, "profit_wei": None,
                    "reason": ("V3 leg requires the on-chain quoter; offline recompute is "
                               "not attempted rather than approximating "
                               "concentrated-liquidity mathematics"),
                    "legs": leg_evidence}
        r_in, r_out = le.get("reserve_in"), le.get("reserve_out")
        if not r_in or not r_out:
            return {"evaluable": False, "profit_wei": None,
                    "reason": (f"later state for directed leg {i} "
                               f"{pool} {tin}->{tout} lacks reserves"),
                    "legs": leg_evidence}
        amt = _v2_amount_out(amt, int(r_in), int(r_out), le.get("fee_bps"))
        leg_evidence.append({
            "leg": i,
            "pool": pool,
            "token_in": tin,
            "token_out": tout,
            "later_state_block": later["block"],
            "later_state_hash": later.get("block_hash"),
            "state_source": later.get("source"),
            "state_lag_from_anchor": later.get("state_lag_from_anchor"),
        })
    return {"evaluable": True, "profit_wei": amt - int(amount_in),
            "reason": None, "legs": leg_evidence}


# ── bounded detection re-run at the latest reconstructible state ──────────────

def rerun_detection(graph, chain_id: int, records: list) -> dict:
    """Re-run detection ONCE at the warmed cache tip (the only state block
    where graph reconstruction is genuine) and compare against recorded
    fingerprints. Runs under the explicitly disabled recorder guard installed
    by ``evaluate`` (R1 §1) — it cannot append to the source log. Failures are
    returned as gaps, never silently skipped."""
    try:
        from detectors.cycles import CycleDetector
        from opportunity_engine import OpportunityEngine
        from tracker import _fingerprint  # canonical fingerprint (MI-103 §4)
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
                touched = {str(e.pool_address).lower() for e in _iter_graph_edges(graph)}
            except Exception:
                touched = set()
            recorded_fps = {r["fingerprint"] for r in records if r.get("record_type") == "detection"}
            try:
                tip_block = max(int(e.state_block) for e in _iter_graph_edges(graph)
                                if getattr(e, "state_block", 0))
            except Exception:
                tip_block = 0
            ctx = types.SimpleNamespace(block_hash="", number=tip_block,
                                        touched_pools=frozenset(touched))
            result = engine.process(tip_block, touched_pools=frozenset(touched), block_context=ctx)
            redetected = {_fingerprint(r) for r in
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
    """Run one offline replay under an explicitly DISABLED recorder (R1 §1):
    neither the evaluation itself nor the optional detection re-run can append
    to the source shadow log or to any configured live shadow-validation log.
    The prior process-wide recorder is restored in ``finally``; no persistent
    environment or repository runtime configuration is touched."""
    from shadow import recorder as shadow_rec
    from shadow.recorder import ShadowRecorder

    prior = shadow_rec._recorder
    guard = ShadowRecorder(
        Path(tempfile.gettempdir()) / "shadow_replay_recorder_guard.jsonl",
        enabled=False)
    shadow_rec.set_recorder(guard)
    try:
        return _evaluate_impl(shadow_path, evaluation_path, cache_dir,
                              chain_id, catchup_window, rerun)
    finally:
        shadow_rec.set_recorder(prior)


def _evaluate_impl(shadow_path: Path, evaluation_path: Path, cache_dir: str,
                   chain_id: int, catchup_window: int, rerun: bool) -> dict:
    records_all = read_records(shadow_path)
    source_sha = hashlib.sha256(shadow_path.read_bytes()).hexdigest() if shadow_path.exists() else None
    # R1 §3: chain isolation — everything below operates on THIS chain only.
    records, other_chain_excluded = filter_by_chain(records_all, chain_id)
    timeline = build_pool_timeline(records)
    groups = lifecycle_groups(records)
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

    def _group_sort_key(g):
        det = g["open"] or (g["snapshots"][0] if g["snapshots"] else None)
        block = (det.get("detection") or {}).get("block_number") or 0 if det else 0
        return (block, g["fingerprint"], g["open_seq"] if g["open_seq"] is not None else -1)

    for g in sorted(groups, key=_group_sort_key):
        det = g["open"] or (g["snapshots"][0] if g["snapshots"] else None)
        close_rec = g["close"] or {}
        close_terminal = close_rec.get("terminal") or {}
        record = {
            "record_type": "shadow_evaluation",
            "schema_version": REPLAY_SCHEMA_VERSION,
            "run_id": run_id,
            "fingerprint": g["fingerprint"],
            # R1 §8: lifecycle identity — (fingerprint, open_seq); two OPEN
            # lifecycles of one route are TWO candidates, never collapsed.
            "lifecycle": {
                "open_seq": g["open_seq"],
                "open_evidence": g["open_evidence"],
                "close_seq": close_rec.get("seq"),
                "close_outcome": close_terminal.get("outcome"),
                "close_outcome_basis": close_terminal.get("outcome_basis"),
            },
            "origin": (det.get("identity") or {}).get("origin") if det else None,
            "detected_block": ((det.get("detection") or {}).get("block_number") if det else None),
            "detected_seq": det.get("seq") if det else None,
            "recorded_gross_quote_profit_wei":
                (det.get("size_and_profit") or {}).get("gross_quote_profit_wei") if det else None,
        }

        if det is None:
            # CLOSE-only lifecycle: the OPEN is outside the recorded history —
            # classify without fabricating opening evidence (R1 §8).
            record.update({"outcome": OUTCOME_MISSING,
                           "reason": ("lifecycle has no recorded detection snapshot (its OPEN is "
                                      "outside the recorded history); opening evidence is not "
                                      "fabricated and no route is guessed"),
                           "recomputed_gross_profit_wei": None, "state_lag_blocks": None,
                           "beyond_catchup_window": False})
            outcomes[record["outcome"]] += 1
            evaluated += 1
            eval_records.append(record)
            continue

        det_block = int((det.get("detection") or {}).get("block_number") or 0)
        ident = det.get("identity") or {}
        pools = [str(p).lower() for p in (ident.get("pool_addresses") or [])]
        legs = route_legs(det)
        amount_in = (det.get("size_and_profit") or {}).get("trade_size_amount_in")

        later, resolve_reason, anchor = ({}, None, None)
        if legs is not None:
            later, resolve_reason, anchor = resolve_later_states(
                legs, det_block, timeline, graph, cache_state_block, catchup_window)
        relevant_blocks = {det_block} | {e["block"] for e in later.values()}
        if (det.get("detection") or {}).get("state_block") is not None:
            relevant_blocks.add(int(det["detection"]["state_block"]))
        reorg = detect_reorg_evidence(det, pools, timeline, records, relevant_blocks)

        record.update({
            "recorded_pool_addresses": pools or None,
            "route_leg_count": len(legs) if legs is not None else None,
            "later_state_blocks": {
                f"{legs[i][0]}:{legs[i][1]}->{legs[i][2]}": e["block"]
                for i, e in later.items()} if legs is not None else None,
            "later_state_lag_from_anchor": {
                f"{legs[i][0]}:{legs[i][1]}->{legs[i][2]}": e.get("state_lag_from_anchor")
                for i, e in later.items()} if legs is not None else None,
            "later_state_hashes": {
                f"{legs[i][0]}:{legs[i][1]}->{legs[i][2]}": e.get("block_hash")
                for i, e in later.items() if e.get("block_hash")} if legs is not None else None,
        })

        if reorg["invalidated"]:
            record.update({"outcome": OUTCOME_REORG, "reason":
                           "conflicting block-hash observations for the discovery block or an "
                           "involved pool (within this chain)",
                           "reorg_evidence": reorg["conflicts"],
                           "recomputed_gross_profit_wei": None, "state_lag_blocks": None,
                           "beyond_catchup_window": False})
        elif legs is None:
            record.update({"outcome": OUTCOME_MISSING,
                           "reason": ("ordered token path not recorded or inconsistent with the "
                                      "pool list; route legs cannot be reconstructed without "
                                      "fabricating directions"),
                           "recomputed_gross_profit_wei": None, "state_lag_blocks": None,
                           "beyond_catchup_window": False})
        else:
            recompute = recompute_route_profit(amount_in, legs, later)
            if not recompute["evaluable"]:
                reason = recompute["reason"]
                if resolve_reason:
                    reason = (reason + "; " if reason else "") + resolve_reason
                record.update({"outcome": OUTCOME_MISSING, "reason": reason,
                               "recomputed_gross_profit_wei": None,
                               "state_lag_blocks": None,
                               "beyond_catchup_window": False,
                               "route_legs": recompute["legs"] or None})
            else:
                lag = (anchor - det_block) if anchor is not None else None
                rec_profit = (det.get("size_and_profit") or {}).get("gross_quote_profit_wei")
                outcome = OUTCOME_STILL if recompute["profit_wei"] > 0 else OUTCOME_GONE
                beyond = lag is not None and lag > catchup_window
                beyond_window += 1 if beyond else 0
                record.update({
                    "outcome": outcome,
                    "reason": None,
                    "recomputed_gross_profit_wei": recompute["profit_wei"],
                    "recompute_basis": "constant_product_v2_adapter_math_offline",
                    "recompute_legs": recompute["legs"],
                    "common_anchor_block": anchor,
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
    if not records_all:
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
    if earliest_state and groups:
        detection_blocks = []
        for g in groups:
            det = g["open"] or (g["snapshots"][0] if g["snapshots"] else None)
            if det:
                detection_blocks.append((det.get("detection") or {}).get("block_number") or 0)
        if detection_blocks:
            earliest_detection = min(detection_blocks)
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
        "source_unchanged_promise": ("the replay opens the source log read-only; the whole replay "
                                     "(including the detection re-run) runs under an explicitly "
                                     "disabled shadow recorder restored afterwards (R1 §1)"),
        "recorder_guard": "explicitly disabled recorder installed for the replay; prior process-wide recorder restored",
        "cache_dir": str(cache_dir),
        "chain_id": chain_id,
        "chain_isolation": {
            "records_total": len(records_all),
            "records_for_chain": len(records),
            "records_other_chain_excluded": other_chain_excluded,
            "note": "pool addresses and block numbers are not globally unique; replay inputs are filtered per chain (R1 §3)",
        },
        "catchup_window": catchup_window,
        "cache_loaded": bool(warmed) or cache_state_block is not None,
        "warm_graph_pools_restored": warmed,
        "cache_state_block": cache_state_block,
        "tvl_restore": "PoolCache.warm_graph (MI-102A corrected restore-time TVL handling)",
        "route_leg_reconstruction": "identity.ordered_token_path + identity.pool_addresses; one direction-exact edge per hop (R1 §2)",
        "lifecycles_evaluated": evaluated,
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
    header["gap_count"] = gap_count
    return header


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline shadow-validation replay (DSS-WO-MI-103 §5)")
    ap.add_argument("--shadow-log", default="logs/shadow_validation.jsonl")
    ap.add_argument("--evaluation-log", default="logs/shadow_evaluation.jsonl")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--chain-id", type=int, default=None)
    ap.add_argument("--catchup-window", type=int, default=DEFAULT_CATCHUP_WINDOW)
    ap.add_argument("--no-rerun-detection", action="store_true")
    args = ap.parse_args()

    try:
        from config import CACHE_DIR, CHAIN_ID as default_chain
    except Exception:
        try:
            from config import CACHE_DIR
            default_chain = 1
        except Exception:
            CACHE_DIR, default_chain = "./cache", 1
    cache_dir = args.cache_dir or CACHE_DIR
    chain_id = args.chain_id if args.chain_id is not None else default_chain

    shadow_path = Path(args.shadow_log)
    if not shadow_path.exists():
        print(f"[shadow-replay] source log not found: {shadow_path}", file=sys.stderr)
        return 2
    header = evaluate(shadow_path, Path(args.evaluation_log), cache_dir,
                      chain_id, args.catchup_window, not args.no_rerun_detection)
    print(f"[shadow-replay] run {header['run_id']} chain={header['chain_id']}: "
          f"lifecycles={header['lifecycles_evaluated']} "
          f"outcomes={header['outcome_counts']} gaps={header['gap_count']} "
          f"beyond_window={header['beyond_catchup_window_count']} "
          f"chain_records={header['chain_isolation']['records_for_chain']}"
          f"(+{header['chain_isolation']['records_other_chain_excluded']} other-chain excluded) "
          f"source_sha256={str(header['source_sha256'])[:16]}…")
    print(f"[shadow-replay] evaluation written to {args.evaluation_log} (source log untouched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
