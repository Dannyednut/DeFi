"""Shared execution-safety exceptions (DSS-WO-ENG-015 R4).

The single execution path is

    ExecutionRouter → ArbExecutor → Builder → Signer → Relay → Receipt/Result

and every enforcement point on that path — builder policy validation,
signer chain/custody checks, relay submission checks — refuses through the
ONE exception type defined here. A refusal is fail-closed: callers must
treat it as a failed execution attempt, never as a success and never as a
silent skip.
"""
from __future__ import annotations


class ExecutionRefused(RuntimeError):
    """Raised whenever an execution-safety precondition fails.

    Typical causes: the master execution switch (EXECUTE_ONCHAIN) is
    disabled, a chain-identity mismatch, a transaction-policy violation,
    a wallet-custody failure, or a signed payload that did not originate
    from the approved signing process. Never carries secret material in
    its message (callers additionally sanitize error text before audit
    or logging — see executor/audit.py).
    """
