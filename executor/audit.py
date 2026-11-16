"""Execution audit log and credential containment (DSS-WO-ENG-015 R4).

Every execution attempt — successful, refused, failed or uncertain — is
appended to a JSONL audit log in ``logs/`` (gitignored, so records stay in
the agent runtime and never reach Git). Error text reaching the audit log,
exception surfaces or relay results is scrubbed through structural
credential-containment rules ported from the R0–R3 gate work (DSS-WO-ENG-008
Finding A, DSS-WO-ENG-010 Finding B, DSS-WO-ENG-011):

  * any scheme:// URL is replaced wholesale — a URL can carry user
    information, a path-embedded provider key and query tokens that do not
    have to look like hex;
  * Bearer/Basic scheme tokens are redacted;
  * credential-shaped assignments (api key / token / secret / password /
    passphrase / authorization / credentials = value) are redacted;
  * any hex blob of 100+ characters — raw signed transactions serialize to
    ≥ ~200 hex chars while hashes are 64 — is treated as potential secret
    material and redacted, regardless of prefix casing.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from log import get_logger

log = get_logger("executor.audit")

# Where the per-attempt audit log is written. logs/*.jsonl is gitignored —
# the log stays in the agent runtime, never in Git.
DEFAULT_AUDIT_LOG_PATH = os.path.join("logs", "execution_audit.jsonl")

AUDIT_FIELDS = (
    "timestamp", "purpose", "chain_id", "target", "function",
    "estimated_gas", "actual_gas_used", "tx_hash", "outcome", "error",
    "max_fee_exposure_wei",
)

# Raw signed transactions serialize to ≥ ~200 hex characters, while
# keystore/bundle/transaction hashes are exactly 64. Any hex blob of 100+
# characters inside an error string is therefore treated as potential
# secret material and redacted before it can reach the audit log — with a
# lowercase or UPPERCASE 0x/0X prefix, or with no prefix at all.
_LONG_HEX_RE = re.compile(r"(?:0[xX])?[0-9a-fA-F]{100,}")

_CRED_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")
_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._+/=:\-]{8,}"
)
_CRED_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|passwd|"
    r"pwd|passphrase|authorization|auth|credential|credentials)\b"
    r"(\s*[=:]\s*)[^\s'\";&,]+"
)


def sanitize_text(text: str) -> str:
    """Structural credential-containment rules shared by every module that
    surfaces error text. Order matters: URLs first (they can embed query
    tokens), then Bearer/Basic values, then credential-shaped assignments,
    then any remaining long hex."""
    text = _CRED_URL_RE.sub("<redacted-url>", text)
    text = _AUTH_SCHEME_RE.sub(r"\1 <redacted>", text)
    text = _CRED_KV_RE.sub(r"\1\2<redacted>", text)
    return _LONG_HEX_RE.sub("0x<redacted-hex>", text)


def sanitize_error(message: object, *forbidden: str) -> str:
    """Make an error message safe for the audit log AND every
    caller-visible surface.

    Layers, in order:
      1. every explicitly forbidden string — configured RPC/relay URLs,
         their credential-bearing components, and the exact signed payload
         hex handed in by the caller — is replaced with a redaction marker;
      2. the structural rules from sanitize_text (URLs, Bearer/Basic
         values, credential-shaped assignments, any 100+ character hex
         blob regardless of prefix casing).

    Truncated to 300 characters so a hostile error cannot bloat the log.
    """
    text = str(message)
    for secret in forbidden:
        if secret:
            text = text.replace(secret, "<redacted>")
    return sanitize_text(text)[:300]


def record_execution(
    log_path: Optional[str] = None,
    **fields,
) -> dict:
    """Append one execution-attempt record to the JSONL audit log.

    Credentials never enter this function's callers' records in the first
    place; unexpected fields are dropped so the record shape stays
    auditable, and the error field passes through sanitize_error as
    defence in depth.
    """
    path = log_path or DEFAULT_AUDIT_LOG_PATH
    record = {k: fields.get(k) for k in AUDIT_FIELDS}
    record["timestamp"] = record["timestamp"] or time.time()
    if record.get("error"):
        record["error"] = sanitize_error(record["error"])
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        # Auditing must never mask, replace or rewrite the execution
        # outcome — and it must never silently swallow the accounting
        # failure either.
        log.error(
            "failed to write execution audit record: %s",
            type(exc).__name__,
        )
    return record
