"""
Shadow-Mode Validation Recorder (DSS-WO-MI-103)
===============================================

Observational, evidence-based shadow validation for block-originated and
mempool-originated opportunities. This module is a RECORDER only: it changes
no detector thesis, no ranking, no validation economics, no execution
routing and no bidding policy.

Design boundaries (work order §1):
- ONE recorder module, ONE append-only file (``logs/shadow_validation.jsonl``).
- Exactly TWO runtime integration points:
  1. ``OpportunityEngine.process_candidates()`` exit — the shared funnel for
     block and mempool origins; records EVERY candidate (execution-ready or
     not) with DETECTION-TIME evidence only.
  2. ``OpportunityTracker._write_lifecycle()`` — the central lifecycle-write
     path; terminal CLOSE events only.
- The canonical executable-route fingerprint is REUSED from
  ``tracker._fingerprint`` (ENG-017 §5, identical components to
  ``OpportunityEngine._identity_key``). No second fingerprint algorithm.
- No daemon, thread, queue, scheduler, database, parallel tracker or
  analytics service. A plain ``threading.Lock`` guards interleaved appends
  from the asyncio mempool promotion tasks and router threads.
- Missing values are ``null`` / explicitly unavailable — never estimates
  disguised as observed data. No credentials, provider URLs, signed
  transaction payloads or private keys are recorded: records carry a
  WHITELIST of evidence fields, never a raw metadata dump.

Record schema (``schema_version`` 1, per-record ``record_type``):
- ``detection`` records: event ``OPEN`` (one per fingerprint per open
  lifecycle) or ``UPDATE`` (every later detection snapshot), monotone
  ``seq`` for deterministic latest-value selection by readers.
- ``lifecycle`` records: event ``CLOSE`` (once per lifecycle; duplicates are
  suppressed idempotently), terminal outcome, competition evidence, and the
  actual execution outcome when an independently authorised execution
  occurred.
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from log import get_logger
from tracker import _fingerprint  # DSS-WO-MI-103 §4: reuse the canonical fingerprint

log = get_logger("shadow_recorder")

RECORD_SCHEMA_VERSION = 1
DEFAULT_TAIL_SCAN_LINES = 20_000     # bounded restart-reconstruction scan (§4.4)
DEFAULT_STATE_CAPACITY = 8_192       # bounded lifecycle bookkeeping per process
DEFAULT_TAIL_SCAN_LINES_ENV = "SHADOW_TAIL_SCAN_LINES"
ENABLED_ENV = "SHADOW_VALIDATION_ENABLED"
PATH_ENV = "SHADOW_VALIDATION_PATH"

# Fields that the runtime genuinely does not persist at the two permitted
# recording points. They are emitted as null and listed here so readers can
# distinguish "not observed" from "observed zero" (work order §3).
STRUCTURALLY_UNAVAILABLE = {
    "router_decision": "router/refusal decisions happen after the permitted recording points",
    "builder_tip_tier": "no builder-tip tier is persisted by the runtime",
    "quote_method": "per-candidate quote method is not persisted by the detectors",
    "size_selection_method": "size-selection method is not persisted by the detectors",
    "inclusion_block": "inclusion block of an execution bundle is not tracked",
    "bundle_position": "bundle position is not tracked",
    "discovery_provenance": "scheduled/reactive discovery provenance is only supplied by the merged discovery implementation when present",
}


class _LifecycleState:
    """Bounded in-memory bookkeeping for one fingerprint."""

    __slots__ = ("open_seq", "open", "updates", "matched", "continued")

    def __init__(self, open_seq: Optional[int], open: bool, matched: str = "open",
                 continued: bool = False):
        self.open_seq = open_seq          # seq of the OPEN record (None: opened before the scanned tail)
        self.open = open
        self.updates = 0
        self.matched = matched            # "open" | "unmatched"
        self.continued = continued        # reconstructed from a previous session's tail


class ShadowRecorder:
    """Append-only shadow validation recorder (see module docstring)."""

    def __init__(self, path, tail_scan_lines: int = DEFAULT_TAIL_SCAN_LINES,
                 state_capacity: int = DEFAULT_STATE_CAPACITY, enabled: bool = True,
                 session_id: Optional[str] = None):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._session_id = session_id or uuid.uuid4().hex[:12]
        self._process_start_unix = time.time()
        self._pid = os.getpid()
        self._capacity = max(1, int(state_capacity))
        self._overflow_warned = False
        # fingerprint -> lifecycle state; bounded for CLOSED tombstones only —
        # OPEN lifecycles are never evicted (R1 §6B).
        self._lifecycles: "collections.OrderedDict[str, _LifecycleState]" = collections.OrderedDict()
        self._seq = 0
        self.stats = {
            "detection_opens": 0, "detection_updates": 0, "terminal_closes": 0,
            "duplicate_closes_suppressed": 0, "unmatched_closes": 0,
            "restart_scanned_records": 0, "restart_continued_lifecycles": 0,
            "append_failures": 0,
            "open_state_capacity_exceeded": 0,
            "restart_scan_bytes_read": 0,
        }
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # never break the host process on init
                log.warning(f"shadow recorder cannot create log directory {self.path.parent}: {exc}")
            self._reconstruct_from_tail(tail_scan_lines)

    # ═══════════════════════════════════════════════════════════════════════════
    # Restart reconstruction (work order §4.4) — bounded scan of the JSONL tail
    # ═══════════════════════════════════════════════════════════════════════════

    _TAIL_CHUNK_BYTES = 64 * 1024

    def _read_tail_lines(self, tail_scan_lines: int) -> list:
        """Read only the LAST ``tail_scan_lines`` lines by seeking BACKWARD
        from EOF in fixed chunks (R1 §7) — startup work does not scale with
        the historical file and no whole-file buffer is retained. Handles an
        empty file, a final line without a trailing newline, malformed lines
        (skipped by the parse loop) and files with fewer records than
        requested. When the backward scan stops at a chunk boundary mid-line,
        the leading partial line is dropped: it belongs to a record older
        than the requested window, and the +1 newline slack keeps the
        requested line count complete."""
        try:
            if not self.path.exists():
                return []
            size = self.path.stat().st_size
            if size <= 0:
                return []
            max_lines = max(0, int(tail_scan_lines))
            with open(self.path, "rb") as f:
                if size <= self._TAIL_CHUNK_BYTES:
                    self.stats["restart_scan_bytes_read"] += size
                    return f.read().decode("utf-8", errors="replace").split("\n")
                needed = max_lines + 1  # slack for the boundary-partial line
                buf = b""
                pos = size
                while pos > 0 and buf.count(b"\n") <= needed:
                    step = min(self._TAIL_CHUNK_BYTES, pos)
                    pos -= step
                    f.seek(pos)
                    chunk = f.read(step)
                    self.stats["restart_scan_bytes_read"] += step
                    buf = chunk + buf
            lines = buf.decode("utf-8", errors="replace").split("\n")
            if pos > 0 and lines:
                lines = lines[1:]  # partial line cut at the chunk boundary
            if max_lines and len(lines) > max_lines:
                lines = lines[-max_lines:]
            return lines
        except Exception as exc:
            log.warning(f"shadow recorder tail read failed (continuing fresh): {exc}")
            return []

    def _reconstruct_from_tail(self, tail_scan_lines: int) -> None:
        """Rebuild open lifecycles and the monotone sequence from the
        recorder's own JSONL tail. BOUNDED: the tail is located by seeking
        backward from EOF (``_read_tail_lines``) — the historical file is
        neither read nor retained. A fingerprint whose latest detection
        record is an OPEN/UPDATE continues its lifecycle in this process (no
        duplicate OPEN); a fingerprint whose latest terminal record is a
        CLOSE starts fresh on the next detection; and an OPEN observed AFTER
        a CLOSE within the same scanned tail starts a FRESH lifecycle with
        the new OPEN's sequence — the previous lifecycle's open_seq, update
        count and matched status are NOT retained (R1 §6A). Lifecycles opened
        BEFORE the scanned window are continued with ``open_seq=None`` (the
        OPEN record itself is outside the scan) — still continued, never
        duplicated, without fabricating the missing opening evidence."""
        try:
            lines = self._read_tail_lines(tail_scan_lines)
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                self.stats["restart_scanned_records"] += 1
                fp = rec.get("fingerprint")
                if not fp:
                    continue
                try:
                    self._seq = max(self._seq, int(rec.get("seq") or 0))
                except Exception:
                    pass
                rtype = rec.get("record_type")
                event = rec.get("event")
                if rtype == "lifecycle" and event == "CLOSE":
                    st = self._lifecycles.get(fp)
                    if st is not None:
                        st.open = False
                    else:
                        # terminal record for a lifecycle opened beyond the
                        # scanned window: fingerprint is free again
                        self._lifecycles[fp] = _LifecycleState(None, open=False, matched="unmatched")
                        self._lifecycles.move_to_end(fp)
                    continue
                if rtype == "detection" and event in ("OPEN", "UPDATE"):
                    st = self._lifecycles.get(fp)
                    if event == "OPEN" and st is not None and not st.open:
                        # R1 §6A: a closed fingerprint starts a FRESH lifecycle
                        # — new OPEN sequence, reset counters and matched
                        # status; the previous lifecycle's open_seq is not
                        # retained. Still a previous-session continuation.
                        self._lifecycles[fp] = _LifecycleState(
                            int(rec.get("seq") or 0), open=True, matched="open",
                            continued=True)
                        self._lifecycles.move_to_end(fp)
                        self.stats["restart_continued_lifecycles"] += 1
                        continue
                    if st is None:
                        # any lifecycle reconstructed from the tail scan
                        # belongs to a PREVIOUS session: an OPEN seen in the
                        # tail opens it with its seq; an UPDATE without a seen
                        # OPEN continues one opened before the scanned window
                        # (open_seq unknown). Both are continuations.
                        open_seq = int(rec.get("seq") or 0) if event == "OPEN" else None
                        self._lifecycles[fp] = _LifecycleState(open_seq, open=True,
                                                               continued=True)
                        self.stats["restart_continued_lifecycles"] += 1
                    else:
                        st.open = True
                        st.continued = True
                        if event == "UPDATE":
                            st.updates += 1
                        if event == "OPEN" and st.open_seq is None:
                            st.open_seq = int(rec.get("seq") or 0)
                        self._lifecycles.move_to_end(fp)
            self._seq = self._seq + 1  # next appendable sequence number
        except Exception as exc:
            log.warning(f"shadow recorder restart reconstruction failed (continuing fresh): {exc}")
            self._seq = self._seq or 0

    # ═══════════════════════════════════════════════════════════════════════════
    # Append-only plumbing
    # ═══════════════════════════════════════════════════════════════════════════

    def _append(self, record: dict) -> None:
        """Append ONE independently readable JSON line. Never rewrites,
        truncates or mutates previously recorded evidence (§3)."""
        try:
            line = json.dumps(record, separators=(",", ":"), default=str)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as exc:
            self.stats["append_failures"] += 1
            log.warning(f"shadow recorder append failed: {exc}")

    def _session(self) -> dict:
        return {
            "session_id": self._session_id,
            "process_start_unix": round(self._process_start_unix, 6),
            "pid": self._pid,
        }

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _touch(self, fp: str, state: _LifecycleState) -> None:
        self._lifecycles[fp] = state
        self._lifecycles.move_to_end(fp)
        while len(self._lifecycles) > self._capacity:
            # R1 §6B: evict only CLOSED tombstones. An OPEN lifecycle is never
            # silently discarded — discarding one would let a later detection
            # append a second OPEN for a lifecycle that was never closed. If
            # every tracked lifecycle is open, retain them all (idempotency
            # first) and expose the capacity condition explicitly.
            tombstone = next((k for k, st in self._lifecycles.items() if not st.open), None)
            if tombstone is None:
                if not self._overflow_warned:
                    self._overflow_warned = True
                    log.warning(
                        f"shadow recorder open-lifecycle population ({len(self._lifecycles)}) "
                        f"exceeds state capacity ({self._capacity}); open lifecycles are "
                        f"retained for idempotency — no duplicate OPEN will be emitted")
                self.stats["open_state_capacity_exceeded"] += 1
                break
            del self._lifecycles[tombstone]

    # ═══════════════════════════════════════════════════════════════════════════
    # Integration point 1 — OpportunityEngine.process_candidates() exit
    # ═══════════════════════════════════════════════════════════════════════════

    def record_detections(self, candidates: list, *, block_number: int,
                          block_hash: str = "", engine=None) -> None:
        """Record every candidate leaving the shared pipeline — block and
        mempool origins, execution-ready or not (§2). Detection-time evidence
        only; later observations belong to the replay, never here."""
        if not self.enabled or not candidates:
            return
        graph = getattr(engine, "graph", None)
        now = time.time()
        for opp in candidates:
            try:
                fp = _fingerprint(opp)
                meta = dict(getattr(opp, "metadata", {}) or {})
                with self._lock:
                    seq = self._next_seq()
                    state = self._lifecycles.get(fp)
                    if state is None or not state.open:
                        event = "OPEN"
                        state = _LifecycleState(open_seq=seq, open=True,
                                                continued=False)
                        self._touch(fp, state)
                        self.stats["detection_opens"] += 1
                        continued = False
                    else:
                        event = "UPDATE"
                        state.updates += 1
                        continued = state.continued
                        state.continued = False
                        self._touch(fp, state)
                        self.stats["detection_updates"] += 1
                    record = self._detection_record(
                        opp, meta, fp, event, seq, state, block_number, block_hash, now, graph,
                        continued)
                    # R1 §5: sequence allocation, lifecycle transition, record
                    # construction and append ordering share ONE critical
                    # section — the physical file order always matches the
                    # allocated sequence order. _append contains its own
                    # exception handling; the outer try guards construction.
                    self._append(record)
            except Exception as exc:
                log.warning(f"shadow detection recording failed: {exc}")

    def _detection_record(self, opp, meta: dict, fp: str, event: str, seq: int,
                          state: _LifecycleState, block_number: int,
                          block_hash: str, now: float, graph,
                          continued: bool = False) -> dict:
        is_mempool = bool(meta.get("is_mempool", False))
        origin = "mempool" if is_mempool else "block"
        detected_at = getattr(opp, "timestamp", None)
        latency = None
        if origin == "mempool" and isinstance(detected_at, (int, float)) and detected_at > 0:
            latency = round(max(0.0, now - float(detected_at)), 6)
        chain_id = int(getattr(opp, "chain_id", 0) or 0)
        pools = [str(p) for p in (getattr(opp, "pools", []) or [])]
        tokens = [str(t) for t in (getattr(opp, "tokens", []) or [])]
        dexes = [str(d) for d in (getattr(opp, "dexes", []) or [])]
        fees = [str(x) for x in (getattr(opp, "fees", []) or [])]
        arb_kind = getattr(opp.arb_kind, "value", None) or meta.get("arb_kind")
        opp_type = getattr(opp.opp_type, "value", None)
        if opp_type is None:
            opp_type = str(getattr(opp, "opp_type", "")) or None

        simulated = bool(meta.get("adversarial_simulated", False)) or bool(getattr(opp, "simulated_via_contract", False))
        sim_profit = getattr(opp, "simulation_profit_wei", 0) or 0
        edge_states = self._edge_market_inputs(graph, pools, tokens)

        discovery_hash = meta.get("discovery_block_hash") or block_hash or None
        edge_hashes = {e["pool"]: e["state_block_hash"] for e in edge_states if e.get("state_block_hash")}

        return {
            "record_type": "detection",
            "event": event,
            "schema_version": RECORD_SCHEMA_VERSION,
            "seq": seq,
            "session": self._session(),
            "recorded_at_unix": round(now, 6),
            "fingerprint": fp,
            "lifecycle": {
                "open_seq": state.open_seq,
                "update_count": state.updates,
                "continued_from_previous_session": continued,
                "matched": state.matched,
            },
            "identity": {
                "chain_id": chain_id,
                "protocol_set": sorted({e["protocol"] for e in edge_states if e.get("protocol")}) or (meta.get("protocols") or None),
                "venues": sorted({d.lower() for d in dexes}) or None,
                "pool_addresses": pools or None,
                "ordered_token_path": tokens or None,
                "fees": fees or None,
                "canonical_fingerprint": fp,
                "detector_source": meta.get("detector"),
                "strategy_thesis": {
                    "arb_kind": arb_kind,
                    "opp_type": opp_type,
                    "detector_provenance": meta.get("detector_provenance") or None,
                    "consolidated_theses": meta.get("consolidated_theses") or None,
                },
                "origin": origin,
                "origin_evidence": {
                    "is_mempool": meta.get("is_mempool") if "is_mempool" in meta else None,
                    "discovery_source": meta.get("discovery_source"),
                    "discovery_provenance": meta.get("discovery_mode"),
                },
            },
            "detection": {
                "detected_at_unix": round(float(detected_at), 6) if isinstance(detected_at, (int, float)) and detected_at > 0 else None,
                "recorded_at_unix": round(now, 6),
                "block_number": int(meta.get("discovery_block", block_number) or 0) or None,
                "block_hash": discovery_hash,
                "state_block": meta.get("state_block"),
                "state_source": meta.get("state_source"),
                "processing_sequence": seq,
                "mempool_to_open_latency_seconds": latency,
                "trigger_tx_hash": meta.get("trigger_tx"),
            },
            "market_inputs": {
                "route_state_status": meta.get("route_state_status"),
                "route_state_block": meta.get("route_state_block"),
                "route_state_source": meta.get("route_state_source"),
                "edges": edge_states or None,
                "quote_inputs": {
                    "optimal_amount_in": meta.get("optimal_amount_in"),
                    "amount_in_usd": getattr(opp, "amount_in_usd", None),
                    "arb_data_present": bool(meta.get("arb_data")),
                },
                "quote_method": None,
            },
            "size_and_profit": {
                "trade_size_amount_in": meta.get("optimal_amount_in"),
                "size_selection_method": None,
                "gross_quote_profit_wei": meta.get("quote_profit_wei"),
                "validated_profit_floor_wei": meta.get("validated_profit_floor_wei"),
                "net_theoretical_profit_usd": getattr(opp, "net_profit_usd", None),
                "net_theoretical_profit_token_wei": meta.get("deterministic_net_token"),
                "simulation_profit_wei": sim_profit if simulated else None,
                "estimated_profit_usd": getattr(opp, "estimated_profit_usd", None),
            },
            "cost_assumptions": {
                "assumed_gas_units": meta.get("gas_units"),
                "validated_gas_units": meta.get("validated_gas_units"),
                "adversarial_gas_units": meta.get("adversarial_gas_units"),
                "gas_price_wei": meta.get("gas_price_wei"),
                "native_asset_price_usd": self._native_price_usd(),
                "profit_token_price_usd": meta.get("profit_token_price_usd"),
                "builder_tip_tier": None,
                "builder_tip_assumed_token_wei": meta.get("builder_tip_token"),
                "minimum_profit_policy": {
                    "min_profit_usd": self._min_profit_usd(),
                    "min_profit_token_wei": meta.get("min_profit"),
                },
            },
            "eligibility": {
                "execution_supported": meta.get("execution_supported") if "execution_supported" in meta else None,
                "execution_reason": meta.get("execution_reason"),
                "execution_simulated": bool(meta.get("execution_simulated", False)),
                "execution_ready": bool(meta.get("execution_ready", False)),
                "validation_status": meta.get("validation_status"),
                "economics_profitable": meta.get("economics_profitable") if "economics_profitable" in meta else None,
                "adversarial_simulated": meta.get("adversarial_simulated"),
                "adversarial_reason": meta.get("adversarial_reason"),
                "router_decision": None,
            },
            "execution_config_at_decision": self._execution_config(),
            "reorg_evidence": {
                "discovery_block_hash": discovery_hash,
                "per_edge_state_block_hashes": edge_hashes or None,
            },
            "unavailable_fields": sorted(STRUCTURALLY_UNAVAILABLE.keys()),
        }

    def _edge_market_inputs(self, graph, pools: list, tokens: list) -> list:
        """Per-edge market state snapshot from the LOCAL graph — detection-time
        state that is already in memory; never a live RPC call (§2/§3)."""
        edges_out = []
        if graph is None or not pools:
            return edges_out
        seen_pairs = set()
        for pool in pools:
            p = str(pool).lower()
            try:
                edges = graph.get_edges_for_pool(pool) or []
            except Exception:
                edges = []
            for e in edges:
                try:
                    pair = (p, str(getattr(e, "token_in", "")).lower(), str(getattr(e, "token_out", "")).lower())
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    edges_out.append({
                        "pool": str(getattr(e, "pool_address", pool)).lower(),
                        "dex": getattr(e, "dex_name", None),
                        "protocol": getattr(e, "protocol", None) or None,
                        "version": getattr(e, "version", None),
                        "fee_bps": getattr(e, "fee_bps", None),
                        "token_in": pair[1],
                        "token_out": pair[2],
                        "reserve_in": getattr(e, "reserve_in", None),
                        "reserve_out": getattr(e, "reserve_out", None),
                        "sqrt_price_x96": getattr(e, "sqrt_price_x96", None),
                        "liquidity": getattr(e, "liquidity", None),
                        "spot_token0_price": self._spot_price(e),
                        "state_block": getattr(e, "state_block", None) or None,
                        "state_block_hash": getattr(e, "state_block_hash", None) or None,
                        "state_source": getattr(e, "state_source", None) or None,
                        "state_status": getattr(e, "state_status", None) or None,
                        "tvl_usd": getattr(e, "tvl_usd", None),
                    })
                except Exception:
                    continue
        return edges_out

    @staticmethod
    def _spot_price(edge) -> Optional[float]:
        try:
            price = getattr(edge, "token0_price", None)
            return round(float(price), 12) if price is not None else None
        except Exception:
            return None

    @staticmethod
    def _native_price_usd() -> Optional[float]:
        try:
            from config import NATIVE_PRICE_USD
            return float(NATIVE_PRICE_USD)
        except Exception:
            return None

    @staticmethod
    def _min_profit_usd() -> Optional[float]:
        try:
            from config import MIN_PROFIT_USD
            return float(MIN_PROFIT_USD)
        except Exception:
            return None

    @staticmethod
    def _execution_config() -> dict:
        """Execution configuration state AT DECISION TIME (§3) — from local
        config only, never reconstructed via RPC."""
        exec_onchain = None
        exec_source = "unavailable"
        try:
            from config import EXECUTE_ONCHAIN
            exec_onchain = bool(EXECUTE_ONCHAIN)
            exec_source = "config.EXECUTE_ONCHAIN"
        except Exception:
            try:
                from config import execute_onchain
                exec_onchain = bool(execute_onchain())
                exec_source = "config.execute_onchain()"
            except Exception:
                pass
        return {
            "EXECUTE_ONCHAIN": exec_onchain,
            "execute_onchain_source": exec_source,
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # Integration point 2 — OpportunityTracker._write_lifecycle() (CLOSE only)
    # ═══════════════════════════════════════════════════════════════════════════

    def record_terminal(self, evt) -> None:
        """Record ONE terminal CLOSE per lifecycle (§4.3). Called from the
        tracker's central lifecycle-write path. Idempotent: a repeated close
        for an already-closed lifecycle appends nothing. A close for a
        fingerprint with no open lifecycle in recorder state is recorded with
        ``lifecycle.matched="unmatched"`` — the terminal evidence is
        preserved, never silently represented as an executed/expired/other
        state the runtime did not establish (§4)."""
        if not self.enabled:
            return
        try:
            fp = evt.fingerprint
            meta = dict(getattr(evt, "metadata", {}) or {})
            outcome = meta.get("outcome")
            with self._lock:
                state = self._lifecycles.get(fp)
                if state is not None and not state.open:
                    # duplicate CLOSE — idempotent, no second terminal record
                    self.stats["duplicate_closes_suppressed"] += 1
                    return
                seq = self._next_seq()
                if state is not None and state.open:
                    state.open = False
                    state.matched = "open"
                    self._touch(fp, state)
                    self.stats["terminal_closes"] += 1
                    matched = "open"
                else:
                    # no open lifecycle known (opened before the scanned tail
                    # or on another session) — preserve terminal evidence
                    self._touch(fp, _LifecycleState(open_seq=None, open=False, matched="unmatched"))
                    self.stats["unmatched_closes"] += 1
                    matched = "unmatched"
                record = self._terminal_record(evt, meta, fp, outcome, seq, matched)
                # R1 §5: same single critical section as detection records —
                # the terminal CLOSE is physically ordered with its allocated
                # sequence and cannot overtake the OPEN it closes.
                self._append(record)
        except Exception as exc:
            log.warning(f"shadow terminal recording failed: {exc}")

    def _terminal_record(self, evt, meta: dict, fp: str, outcome, seq: int, matched: str) -> dict:
        now = time.time()
        executed = outcome == "EXECUTED"
        filled = outcome == "FILLED_BY_COMPETITOR"
        pools = [str(p) for p in (getattr(evt, "pools", []) or [])]
        record = {
            "record_type": "lifecycle",
            "event": "CLOSE",
            "schema_version": RECORD_SCHEMA_VERSION,
            "seq": seq,
            "session": self._session(),
            "recorded_at_unix": round(now, 6),
            "fingerprint": fp,
            "lifecycle": {
                "open_seq": self._lifecycles[fp].open_seq if fp in self._lifecycles else None,
                "matched": matched,
            },
            "terminal": {
                "outcome": outcome,
                "outcome_basis": (
                    "tracker_execution_receipt" if executed
                    else "tracker_pool_overlap_heuristic" if filled
                    else "tracker_absence_or_ttl_heuristic"
                ),
                "opp_type": getattr(evt, "opp_type", None),
                "arb_kind": getattr(evt, "arb_kind", None),
                "chain_id": getattr(evt, "chain_id", None),
                "open_block": getattr(evt, "open_block", None),
                "open_time": getattr(evt, "open_time", None),
                "close_block": getattr(evt, "close_block", None),
                "close_time": getattr(evt, "close_time", None),
                "last_seen_block": getattr(evt, "last_seen_block", None),
                "duration_blocks": getattr(evt, "duration_blocks", None),
                "duration_seconds": getattr(evt, "duration_seconds", None),
                "peak_profit_usd": getattr(evt, "peak_profit_usd", None),
                "open_profit_usd": getattr(evt, "open_profit_usd", None),
                "close_profit_usd": getattr(evt, "close_profit_usd", None),
                "update_count": getattr(evt, "update_count", None),
                "token_path": [str(t) for t in (getattr(evt, "tokens", []) or [])] or None,
                "pool_addresses": pools or None,
                "venues": sorted({str(d).lower() for d in (getattr(evt, "dexes", []) or [])}) or None,
            },
            "execution_outcome": {
                "actual_profit_usd": meta.get("actual_profit_usd") if executed else None,
                "tx_hash": meta.get("tx_hash") if executed else None,
                "inclusion_block": None,
                "bundle_position": None,
            },
            "competition_evidence": {
                "basis": "tracker_pool_overlap_heuristic" if filled else None,
                "affected_pool": meta.get("overlap_matched_pool") if filled else None,
                "observed_tx_hash": meta.get("tx_hash") if filled else None,
                "block": getattr(evt, "close_block", None) if filled else None,
                "confirmed_competitor_capture": False,
                "note": (
                    "An involved pool was touched by a mined transaction while the "
                    "opportunity was no longer detected. This is observed overlap "
                    "evidence, NOT proof that another searcher captured the exact "
                    "arbitrage; no competitor identity, strategy classification, "
                    "market share or capture probability is claimed."
                ) if filled else None,
            },
            "execution_config_at_decision": self._execution_config(),
            "unavailable_fields": sorted(STRUCTURALLY_UNAVAILABLE.keys()),
        }
        return record

    # ═══════════════════════════════════════════════════════════════════════════
    # Introspection (for tests / offline tooling only)
    # ═══════════════════════════════════════════════════════════════════════════

    def read_records(self) -> list:
        """Read back every appended record (offline helper for tests/replay)."""
        out = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except FileNotFoundError:
            pass
        return out


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level access (the two integration points import this lazily)
# ═══════════════════════════════════════════════════════════════════════════════

_recorder: Optional[ShadowRecorder] = None
_recorder_lock = threading.Lock()


def _default_path() -> Path:
    try:
        from config import LOG_DIR
        base = Path(LOG_DIR)
    except Exception:
        base = Path("logs")
    override = os.environ.get(PATH_ENV)
    return Path(override) if override else base / "shadow_validation.jsonl"


def _default_enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "true").strip().lower() != "false"


def get_recorder() -> ShadowRecorder:
    """Lazy process-wide recorder (append-only file under the configured log
    directory; disable with SHADOW_VALIDATION_ENABLED=false)."""
    global _recorder
    if _recorder is None:
        with _recorder_lock:
            if _recorder is None:
                try:
                    scan = int(os.environ.get(DEFAULT_TAIL_SCAN_LINES_ENV, str(DEFAULT_TAIL_SCAN_LINES)))
                except Exception:
                    scan = DEFAULT_TAIL_SCAN_LINES
                _recorder = ShadowRecorder(_default_path(), tail_scan_lines=scan,
                                           enabled=_default_enabled())
    return _recorder


def set_recorder(recorder: Optional[ShadowRecorder]) -> None:
    """Inject a recorder (tests use temporary paths); ``None`` resets to lazy."""
    global _recorder
    with _recorder_lock:
        _recorder = recorder


def reset_recorder() -> None:
    set_recorder(None)
