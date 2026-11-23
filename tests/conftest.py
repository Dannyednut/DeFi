"""DSS-WO-MI-105-R1: keep the offline test suite OFF the production recorder
path.

Established by the R1 review: any test that runs the pipeline without
installing a recorder makes ``get_recorder()`` lazily create the DEFAULT
ShadowRecorder, which appends to ``logs/shadow_validation.jsonl`` — polluting
the operator's evidence collection with synthetic fixture records (verified
reproducibly across tests/test_eng017_opportunity_identity.py,
test_mi102a_integration.py, test_mi102b_*.py and others).

This autouse fixture is TEST-ONLY: it points the recorder path at a
per-test temporary file and resets the process-wide recorder singleton
before/after every test, so the production ``logs/`` collection can never be
written by a test run. No production module is changed.
"""
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolate_shadow_recorder_from_production_logs(tmp_path, monkeypatch):
    path = tmp_path / "shadow_validation.jsonl"
    monkeypatch.setenv("SHADOW_VALIDATION_PATH", str(path))
    # Reset any recorder left over from a previous test so no test can inherit
    # a recorder bound to the production path.
    from shadow import recorder as shadow_rec
    shadow_rec.set_recorder(None)
    shadow_rec.reset_recorder()
    yield
    shadow_rec.set_recorder(None)
    shadow_rec.reset_recorder()
