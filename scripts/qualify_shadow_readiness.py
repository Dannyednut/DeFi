#!/usr/bin/env python3
"""Offline first-run qualification for shadow-mode collection (DSS-WO-MI-104 §6).

Run from the repository root BEFORE an operator starts a live collection run:

    python scripts/qualify_shadow_readiness.py

Verifies, entirely offline (no RPC, no live endpoints, temp files only):

  1. Import smoke and the relevant offline (shadow) tests pass.
  2. The recorder initializes with the intended configuration in a temporary
     directory (and honours the enabled flag and path override).
  3. The two production recording hooks (detection funnel and terminal
     lifecycle close) produce valid records.
  4. Sequence ordering and lifecycle reconstruction work across restart.
  5. scripts/shadow_replay.py reads the source log WITHOUT modifying it.
  6. scripts/shadow_aggregate.py reads the resulting evaluation records.
  7. The configured execution boundary remains disabled.

The qualification uses offline fixtures shaped exactly like the recorder's
own test suite. It is NOT evidence of real market activity and claims
nothing about the searched universe or candidate profitability.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _load_test_module():
    """Load the MI-103 test module for its production-shaped fixtures
    (real Opportunity objects, stubbed W3 engine, graph builder)."""
    spec = importlib.util.spec_from_file_location(
        "mi103_tests", REPO / "tests" / "test_mi103_shadow_validation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _subprocess(args: list[str], timeout: int = 600) -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    proc = subprocess.run(args, cwd=str(REPO), env=env, timeout=timeout,
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def qualify() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mi104_qualify_"))
    shadow_path = tmp / "shadow_validation.jsonl"
    eval_path = tmp / "shadow_evaluation.jsonl"
    cache_dir = tmp / "cache"
    cache_dir.mkdir()
    ok = True

    # ── 1. execution boundary must be disabled in THIS environment ──────────
    try:
        from config import execution_enabled, EXECUTE_ONCHAIN
        disabled = (not execution_enabled()) and (not EXECUTE_ONCHAIN)
        ok &= check("execution boundary disabled in qualification environment",
                    disabled,
                    "EXECUTE_ONCHAIN must not be true; unset it and re-run"
                    if not disabled else
                    "config.execution_enabled() is False (fail-closed default)")
    except Exception as exc:
        ok &= check("execution boundary disabled in qualification environment",
                    False, f"config import failed: {exc}")

    # ── 2. import smoke ──────────────────────────────────────────────────────
    rc, out = _subprocess([sys.executable, "scripts/ci_import_smoke.py"])
    ok &= check("import smoke (scripts/ci_import_smoke.py)", rc == 0,
                "IMPORT SMOKE: PASS" if rc == 0 else out[-300:])

    # ── 3. relevant offline tests: the MI-103 shadow suite ───────────────────
    rc, out = _subprocess([sys.executable, "-m", "pytest",
                           "tests/test_mi103_shadow_validation.py", "-q",
                           "-p", "no:cacheprovider", "--no-header"])
    tail = " ".join(out.split()[-8:])
    ok &= check("offline shadow test suite (tests/test_mi103_shadow_validation.py)",
                rc == 0, tail)

    # ── fixtures from the shadow test module ────────────────────────────────
    try:
        t = _load_test_module()
    except Exception as exc:
        ok &= check("offline fixture module loads", False, f"{exc}")
        _summary(ok)
        return 1

    # ── 4. recorder initializes with the intended configuration ─────────────
    rec = t.shadow_rec.ShadowRecorder(shadow_path, tail_scan_lines=10_000,
                                      enabled=True)
    ok &= check("recorder initializes in a temporary directory",
                rec.stats["detection_opens"] == 0 and rec._seq == 1,
                f"path={shadow_path}")

    disabled_rec = t.shadow_rec.ShadowRecorder(tmp / "disabled.jsonl",
                                               enabled=False)
    disabled_rec.record_detections([t._opp([t.P1, t.P2], [t.WETH, t.A, t.WETH])],
                                   block_number=100)
    ok &= check("recorder disabled flag produces no records",
                not (tmp / "disabled.jsonl").exists())

    import unittest.mock as mock
    with mock.patch.dict(os.environ, {"SHADOW_VALIDATION_PATH": str(shadow_path)}):
        st = t.shadow_rec.describe_startup_state()
    ok &= check("startup diagnostic reports enabled state + effective path "
                "(SHADOW_VALIDATION_PATH override honoured)",
                st["enabled"] is True and st["path"] == str(shadow_path),
                f"enabled={st['enabled']} path={st['path']} "
                f"existing_bytes={st['existing_bytes']}")

    # ── 5. detection + lifecycle hooks produce valid records ────────────────
    t.shadow_rec.set_recorder(rec)
    engine = t._engine(t._graph())
    engine.process_candidates([t._opp([t.P1, t.P2], [t.WETH, t.A, t.WETH])], 100)
    engine.process_candidates([t._opp([t.P1, t.P2], [t.WETH, t.A, t.WETH])], 101)
    close_evt = types.SimpleNamespace(
        fingerprint=t._fingerprint(t._opp([t.P1, t.P2], [t.WETH, t.A, t.WETH])),
        status="CLOSED", opp_type="MULTI_HOP", arb_kind="cycle", chain_id=1,
        open_block=100, open_time=1.0, close_block=110, close_time=2.0,
        last_seen_block=101, duration_blocks=10, duration_seconds=1.0,
        peak_profit_usd=42.0, open_profit_usd=42.0, close_profit_usd=0.0,
        update_count=1, tokens=[t.WETH, t.A, t.WETH], pools=[t.P1, t.P2],
        dexes=["UniV2"], metadata={"outcome": "EXPIRED"})
    rec.record_terminal(close_evt)
    records = rec.read_records()
    events = [(r.get("record_type"), r.get("event")) for r in records]
    ok &= check("detection + lifecycle hooks produce valid records",
                events == [("detection", "OPEN"), ("detection", "UPDATE"),
                           ("lifecycle", "CLOSE")],
                f"events={events}")
    seqs = [r.get("seq") for r in records]
    ok &= check("sequence ordering is monotone", seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
                f"seq={seqs}")
    d0 = records[0]
    schema_ok = (d0.get("schema_version") == 1 and "identity" in d0
                 and "detection" in d0 and "market_inputs" in d0
                 and d0.get("identity", {}).get("chain_id") == 1)
    ok &= check("detection record carries the MI-103 schema fields", schema_ok)

    # ── 6. restart reconstruction continues the OPEN lifecycle ──────────────
    # The lifecycle above was terminal-closed, so the next detection legitimately
    # opens a FRESH lifecycle (R1 §6A); that second OPEN is the one the restart
    # must continue as UPDATE.
    t.shadow_rec.set_recorder(rec)
    engine.process_candidates([t._opp([t.P1, t.P2], [t.WETH, t.A, t.WETH])], 103)
    second_open_seq = rec.read_records()[-1]["seq"]
    restarted = t.shadow_rec.ShadowRecorder(shadow_path, tail_scan_lines=10_000)
    ok &= check("restart reconstruction scans the tail",
                restarted.stats["restart_scanned_records"] >= 4)
    t.shadow_rec.set_recorder(restarted)
    engine.process_candidates([t._opp([t.P1, t.P2], [t.WETH, t.A, t.WETH])], 105)
    last = restarted.read_records()[-1]
    ok &= check("lifecycle reconstruction across restart (UPDATE continues "
                "the open lifecycle, no duplicate OPEN)",
                last.get("event") == "UPDATE"
                and last["lifecycle"]["open_seq"] == second_open_seq
                and last["lifecycle"]["continued_from_previous_session"] is True,
                f"event={last.get('event')} open_seq="
                f"{last.get('lifecycle', {}).get('open_seq')} (expected {second_open_seq})")

    # ── 7. replay reads the source log WITHOUT modifying it ─────────────────
    rspec = importlib.util.spec_from_file_location(
        "shadow_replay_mod", REPO / "scripts" / "shadow_replay.py")
    replay = importlib.util.module_from_spec(rspec)
    rspec.loader.exec_module(replay)
    try:
        source_sha_before = hashlib.sha256(shadow_path.read_bytes()).hexdigest()
        header = replay.evaluate(shadow_path, eval_path, str(cache_dir),
                                 chain_id=1, catchup_window=1000, rerun=False)
        source_sha_after = hashlib.sha256(shadow_path.read_bytes()).hexdigest()
        replayed = int(header.get("candidates_evaluated", 0)) if isinstance(header, dict) else -1
        eval_recs = [json.loads(l) for l in eval_path.read_text().splitlines()
                     if l.strip()] if eval_path.exists() else []
        entries = [r for r in eval_recs
                   if r.get("record_type") != "shadow_evaluation_run"]
        per_lifecycle = [r for r in entries
                         if r.get("fingerprint") and "outcome" in r]
        src_fps = {r.get("fingerprint") for r in rec.read_records()
                   if r.get("record_type") == "detection"}
        covered = {r.get("fingerprint") for r in per_lifecycle}
        ok &= check("replay runs offline and evaluates the captured records",
                    replayed >= 1 and len(per_lifecycle) == replayed
                    and src_fps and src_fps <= covered,
                    f"candidates_evaluated={replayed}, evaluation entries="
                    f"{len(entries)} (per-lifecycle {len(per_lifecycle)}), "
                    f"fingerprints covered={sorted(covered)}")
        ok &= check("replay leaves the source collection byte-identical",
                    source_sha_before == source_sha_after)
        ok &= check("evaluation records are structurally valid",
                    len(per_lifecycle) > 0
                    and all(r.get("outcome") and isinstance(r.get("fingerprint"), str)
                            for r in per_lifecycle))
    except Exception as exc:
        ok &= check("replay runs offline", False, f"{type(exc).__name__}: {exc}")

    # ── 8. aggregation reads the evaluation records ─────────────────────────
    agg_json, agg_csv = tmp / "aggregates.json", tmp / "aggregates.csv"
    rc, out = _subprocess([sys.executable, "scripts/shadow_aggregate.py",
                           "--shadow-log", str(shadow_path),
                           "--evaluation-log", str(eval_path),
                           "--json-out", str(agg_json), "--csv-out", str(agg_csv)])
    agg_ok = rc == 0 and agg_json.exists() and agg_csv.exists()
    detail = ""
    if agg_ok:
        agg = json.loads(agg_json.read_text())
        detail = f"aggregate keys={sorted(agg.keys())[:6]}…"
    ok &= check("aggregation reads the shadow log + evaluation records", agg_ok,
                detail if agg_ok else out[-300:])

    t.shadow_rec.set_recorder(None)  # leave the process-wide singleton clean
    return _summary(ok)


def _summary(ok: bool) -> int:
    failed = [n for n, o, _ in RESULTS if not o]
    print()
    if ok and not failed:
        print("QUALIFICATION: PASS — offline first-run qualification verified; "
              "the runtime is ready for an owner-side operational smoke period.")
        return 0
    print(f"QUALIFICATION: FAIL — {len(failed)} check(s) failed: {failed}")
    return 1


if __name__ == "__main__":
    time.sleep(0)  # no-op; keeps the module import-only when inspected
    sys.exit(qualify())
