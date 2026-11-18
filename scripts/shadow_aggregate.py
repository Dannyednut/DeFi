#!/usr/bin/env python3
"""
Offline shadow-validation aggregation (DSS-WO-MI-103 §7)
========================================================

Simple OFFLINE aggregation over the append-only shadow-validation records
(and, when present, the replay evaluation records). One-shot JSON + CSV
output — no continuously running reporting service.

Honesty rules (work order §7):
- A candidate count is NOT a detection-yield rate without a corresponding
  searched-universe denominator. The runtime does not persist searched-universe
  exposure at the two permitted recording points, so detection-yield RATES are
  reported as unavailable with the missing evidence source named; only counts
  and distributions are produced.
- Observed pool-overlap outcomes are labelled as tracker overlap heuristics —
  never as confirmed competitor capture.
- Metrics that cannot be calculated from the available records are marked
  unavailable with the reason, never fabricated.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

AGG_SCHEMA_VERSION = 1
TVL_BANDS = [("<10k", 0, 10_000), ("10k-100k", 10_000, 100_000),
             ("100k-1M", 100_000, 1_000_000), (">1M", 1_000_000, float("inf"))]


def read_records(path: Path) -> list:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except FileNotFoundError:
        pass
    return out


def _percentiles(values: list) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)

    def pct(p):
        return round(ordered[min(n - 1, int(round(p / 100 * (n - 1))))], 4)
    return {"n": n, "min": ordered[0], "p50": pct(50), "p90": pct(90), "p95": pct(95),
            "max": ordered[-1], "mean": round(statistics.fmean(ordered), 4)}


def _tvl_band(tvl) -> str:
    if not isinstance(tvl, (int, float)) or tvl <= 0:
        return "unknown"
    for name, lo, hi in TVL_BANDS:
        if lo <= tvl < hi:
            return name
    return "unknown"


def aggregate(shadow_path: Path, evaluation_path: Path) -> dict:
    records = read_records(shadow_path)
    evaluations = read_records(evaluation_path)
    detections = [r for r in records if r.get("record_type") == "detection"]
    closes = [r for r in records if r.get("record_type") == "lifecycle" and r.get("event") == "CLOSE"]
    latest_by_fp = {}
    for r in detections:
        latest_by_fp[r["fingerprint"]] = r

    # ── detection yield: counts by chain / protocol family / TVL band / origin.
    # The searched-universe DENOMINATOR is not persisted at the recording
    # points → rates unavailable (work order §7).
    by_chain = Counter()
    by_protocol = Counter()
    by_band = Counter()
    by_origin = Counter()
    for r in detections:
        ident = r.get("identity") or {}
        by_chain[ident.get("chain_id")] += 1
        for proto in (ident.get("protocol_set") or ["unknown"]):
            by_protocol[proto] += 1
        by_origin[ident.get("origin")] += 1
        edges = (r.get("market_inputs") or {}).get("edges") or []
        max_tvl = max([e.get("tvl_usd") or 0 for e in edges], default=0)
        by_band[_tvl_band(max_tvl)] += 1
    detection_yield = {
        "counts_by_chain": dict(by_chain),
        "counts_by_protocol_family": dict(by_protocol),
        "counts_by_tvl_band": dict(by_band),
        "counts_by_origin": dict(by_origin),
        "detection_yield_rate": {
            "status": "unavailable",
            "reason": ("searched-universe exposure (detector sweeps / pool candidates searched) "
                       "is not persisted at the two permitted recording points; a candidate "
                       "count is not a detection-yield rate without that denominator"),
            "missing_evidence_source": "searched-universe denominator",
        },
    }

    # ── opportunity lifetime distributions (OPEN → CLOSE) ──
    open_by_fp = {}
    for r in detections:
        if (r.get("event") == "OPEN"):
            open_by_fp[r["fingerprint"]] = r
    durations_blocks, durations_secs = [], []
    by_outcome_durations = defaultdict(list)
    for c in closes:
        term = c.get("terminal") or {}
        db, ds = term.get("duration_blocks"), term.get("duration_seconds")
        if isinstance(db, (int, float)) and db >= 0:
            durations_blocks.append(db)
            by_outcome_durations[str(term.get("outcome"))].append(db)
        if isinstance(ds, (int, float)) and ds >= 0:
            durations_secs.append(ds)
    lifetime = {
        "duration_blocks": _percentiles(durations_blocks),
        "duration_seconds": _percentiles(durations_secs),
        "duration_blocks_by_outcome": {k: _percentiles(v) for k, v in by_outcome_durations.items()},
    }

    # ── observable pool-overlap / competitor-fill outcomes (HEURISTIC-labelled) ──
    overlap = Counter()
    overlap_venues = Counter()
    for c in closes:
        term = c.get("terminal") or {}
        if term.get("outcome") == "FILLED_BY_COMPETITOR":
            overlap["observed_pool_overlap_closes"] += 1
            for venue in (term.get("venues") or ["unknown"]):
                overlap_venues[venue] += 1
    competition = {
        "observed_pool_overlap_closes": overlap["observed_pool_overlap_closes"],
        "by_route_class_venue": dict(overlap_venues),
        "confirmed_competitor_captures": 0,
        "claim": ("observed overlap heuristic only: an involved pool was touched by a mined "
                  "transaction while the opportunity was no longer detected. This is NOT "
                  "confirmed competitor identity, strategy classification, market share or "
                  "capture probability (work order §6)"),
        "failed_or_invisible_private_submissions_counted_as_captures": 0,
    }

    # ── simulation-accuracy distributions (only where both sides exist) ──
    sim_deltas = []
    sim_pairs = 0
    for r in detections:
        sp = (r.get("size_and_profit") or {})
        sim, quote = sp.get("simulation_profit_wei"), sp.get("gross_quote_profit_wei")
        if isinstance(sim, int) and isinstance(quote, int):
            sim_pairs += 1
            sim_deltas.append(sim - quote)
    simulation_accuracy = {
        "pairs_with_both_simulation_and_quote": sim_pairs,
        "delta_wei_simulation_minus_quote": _percentiles(sim_deltas) if sim_deltas else {
            "status": "unavailable",
            "reason": "no detection records carry both simulation profit and gross quote profit",
        },
    }

    # ── later-state profitability survival (from replay evaluation, if present) ──
    surv = Counter()
    for e in evaluations:
        if e.get("record_type") == "shadow_evaluation":
            surv[str(e.get("outcome"))] += 1
    later_state_survival = {
        "from": str(evaluation_path),
        "counts": dict(surv) if surv else None,
        "unavailable": None if surv else "no replay evaluation records found; run scripts/shadow_replay.py first",
    }

    # ── execution-eligibility rejection breakdown by refusal reason ──
    rejections = Counter()
    for r in detections:
        elig = r.get("eligibility") or {}
        if not elig.get("execution_ready", False):
            reason = (str(elig.get("adversarial_reason") or elig.get("validation_status")
                          or elig.get("execution_reason") or "unspecified"))
            rejections[reason] += 1
    rejection_breakdown = {
        "rejected_candidates": sum(rejections.values()),
        "by_refusal_reason": dict(rejections),
        "note": ("router decision/refusal evidence is not observable at the permitted recording "
                 "points; this breakdown reflects validation/eligibility evidence at detection "
                 "recording time"),
    }

    # ── actual-versus-predicted profit for independently authorised executions ──
    deltas = []
    executed = 0
    for c in closes:
        term = c.get("terminal") or {}
        if term.get("outcome") != "EXECUTED":
            continue
        executed += 1
        ex = c.get("execution_outcome") or {}
        actual = ex.get("actual_profit_usd")
        predicted = term.get("close_profit_usd")
        if isinstance(actual, (int, float)) and isinstance(predicted, (int, float)):
            deltas.append(actual - predicted)
    actual_vs_predicted = {
        "executed_closes": executed,
        "delta_usd_actual_minus_predicted": _percentiles(deltas) if deltas else {
            "status": "unavailable",
            "reason": ("no executed lifecycle with both actual and predicted profit recorded "
                       "(EXECUTE_ONCHAIN=false; no independently authorised executions yet)"),
        },
    }

    unavailable = {
        "detection_yield_rate": "missing searched-universe denominator (not persisted at recording points)",
        "inclusion_block_bundle_position": "bundle placement is not tracked by the runtime",
        "builder_tip_tier": "no builder-tip tier is persisted by the runtime",
        "confirmed_competitor_capture": "not derivable from overlap evidence (observability class limits)",
    }

    return {
        "schema_version": AGG_SCHEMA_VERSION,
        "source_log": str(shadow_path),
        "detection_records": len(detections),
        "close_records": len(closes),
        "fingerprints": len(latest_by_fp),
        "detection_yield": detection_yield,
        "lifetime": lifetime,
        "competition_evidence": competition,
        "simulation_accuracy": simulation_accuracy,
        "later_state_survival": later_state_survival,
        "rejection_breakdown": rejection_breakdown,
        "actual_vs_predicted_profit": actual_vs_predicted,
        "unavailable_aggregates": unavailable,
    }


def write_csv(agg: dict, csv_path: Path) -> None:
    rows = [("section", "key", "value")]
    dy = agg["detection_yield"]
    for key, val in dy["counts_by_chain"].items():
        rows.append(("detection_count_by_chain", str(key), val))
    for key, val in dy["counts_by_protocol_family"].items():
        rows.append(("detection_count_by_protocol", str(key), val))
    for key, val in dy["counts_by_tvl_band"].items():
        rows.append(("detection_count_by_tvl_band", str(key), val))
    for key, val in dy["counts_by_origin"].items():
        rows.append(("detection_count_by_origin", str(key), val))
    for key, val in (agg["lifetime"]["duration_blocks_by_outcome"] or {}).items():
        rows.append((f"lifetime_blocks_{key}", "n", val.get("n", 0)))
    for key, val in agg["competition_evidence"]["by_route_class_venue"].items():
        rows.append(("observed_overlap_by_venue_heuristic", str(key), val))
    for key, val in agg["rejection_breakdown"]["by_refusal_reason"].items():
        rows.append(("rejection_by_reason", str(key), val))
    ls = agg["later_state_survival"].get("counts") or {}
    for key, val in ls.items():
        rows.append(("later_state_survival", str(key), val))
    for key, val in agg["unavailable_aggregates"].items():
        rows.append(("unavailable", str(key), val))
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline shadow-validation aggregates (DSS-WO-MI-103 §7)")
    ap.add_argument("--shadow-log", default="logs/shadow_validation.jsonl")
    ap.add_argument("--evaluation-log", default="logs/shadow_evaluation.jsonl")
    ap.add_argument("--json-out", default="logs/shadow_aggregates.json")
    ap.add_argument("--csv-out", default="logs/shadow_aggregates.csv")
    args = ap.parse_args()

    agg = aggregate(Path(args.shadow_log), Path(args.evaluation_log))
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(agg, f, indent=2, default=str)
        f.write("\n")
    write_csv(agg, Path(args.csv_out))
    print(f"[shadow-agg] detections={agg['detection_records']} closes={agg['close_records']} "
          f"fingerprints={agg['fingerprints']} -> {args.json_out}, {args.csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
