#!/usr/bin/env python3
"""CI test-results summary (DSS-WO-ENG-001).

Parses the pytest JUnit XML and prints an explicit, human-readable count
table into the workflow logs and the GitHub step summary.

This script ONLY reports — it never gates. The job's red/green status comes
solely from pytest's exit code.

Usage: python scripts/ci_test_summary.py <junit-xml-path>
Exit:  always 0 (if the file can be read; 1 with a clear message otherwise).
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def summarize(path: Path) -> str:
    tree = ET.parse(path)
    root = tree.getroot()

    suites = list(root.iter("testsuite"))
    total = failures = errors = skipped = 0
    for s in suites:
        total += int(s.get("tests", 0))
        failures += int(s.get("failures", 0))
        errors += int(s.get("errors", 0))
        skipped += int(s.get("skipped", 0))
    passed = total - failures - errors - skipped

    failing: list[tuple[str, str]] = []
    for tc in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = tc.find(kind)
            if node is not None:
                failing.append((kind.upper(), f"{tc.get('classname', '?')}::{tc.get('name', '?')}"))

    lines = [
        "### Test results",
        "",
        f"- total test units: **{total}** (unittest subTest failures are counted individually)",
        f"- passed: **{passed}** | failed: **{failures}** | errors: **{errors}** | skipped: **{skipped}**",
        "",
        "Reference points (see docs/DEPENDENCIES.md failure register):",
        "- DSS-WO-ENG-004 scope on this branch: the old module-level broadcast "
        "script is replaced by a gate-routed import-safe integration test "
        "(collected; its live phases skip visibly). All gate/fee/simulation/"
        "custody additions must pass; the ONLY skipped units are the two "
        "opt-in live Flashbots phases.",
        "- Still expected on this branch: the 20 pre-existing baseline failures "
        "and 1 fixture error carried over from the DSS-WO-ENG-002 scope (fixed "
        "on work/dss-eng-001-deps-ci, PR #1 — this branch forked before those "
        "fixes). The job stays red until the branches reconcile.",
        "- Historical: DSS-WO-ENG-002 CI scope: 20 failed, 91 passed, 1 error; "
        "DSS-WO-ENG-001 CI scope (pre-retirement): 22 failed, 86 passed, "
        "1 error; DSS-MISSION-001 original baseline: 23 failed, 86 passed, "
        "1 error.",
        "- Failures and errors in THIS run that are NOT part of that known set "
        "are regressions — treat them as blockers. No failure is skipped, "
        "xfail-marked or otherwise hidden by CI.",
        "",
    ]
    if failing:
        lines.append(
            f"Failing testcase elements ({len(failing)}; unittest subTest failures "
            "are collapsed into their parent test, so this list can be shorter "
            "than the failed-unit count above):"
        )
        for kind, name in failing:
            lines.append(f"- [{kind}] {name}")
    else:
        lines.append("No failing test units in this run.")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: ci_test_summary.py <junit-xml-path>")
        return 1
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"test results file not found: {path}")
        return 1

    report = summarize(path)
    print(report)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
