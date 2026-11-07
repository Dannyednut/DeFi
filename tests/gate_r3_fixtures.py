"""DSS-WO-ENG-015 R3 — TEST-ONLY transport and map-isolation helpers.

PRODUCTION CODE MUST NEVER IMPORT THIS MODULE. It exists so the offline
suite can keep exercising the gate orchestrations deterministically after
R3 (a) made the code-controlled authorization maps genuinely immutable
and (b) removed every transport-factory parameter from the production
signing/submission interfaces:

  * ``ALLOWED_TESTNET_CHAINS`` and ``KNOWN_TARGETS`` (including nested
    chain-to-target maps) are deep-frozen ``MappingProxyType`` views in
    ``executor.testnet_gate``; no caller can mutate any exposed policy
    mapping. Offline tests that need a second allowlisted chain or a
    known target REPLACE the module attribute wholesale with
    ``frozen_map`` + ``unittest.mock.patch.object`` — the established
    controlled-mock technique, never an operational interface.
  * The production orchestrations construct their transports internally
    through the PRIVATE builders ``_build_rpc_transport(url)`` and
    ``_build_relay_transport(rpc_url, auth_account)``. No public
    interface accepts a ``web3_factory``/``relay_factory`` (or any other
    transport/provider/relay-client) argument. Offline tests inject
    controlled mock transports by patching those PRIVATE builders with
    ``offline_transports`` — a test technique on private module
    attributes, never an ordinary runtime execution argument and never a
    public dependency-injection surface.

Both helpers are pure ``unittest.mock.patch`` scopes: production never
sees them, every enforcement point still reads the constants/builders at
execution time, and no test seam can weaken a security control.
"""
from __future__ import annotations

import contextlib
from typing import Mapping, Optional
from unittest.mock import patch

import executor.testnet_gate as _gate

__all__ = ["frozen_map", "offline_transports"]


def frozen_map(mapping: Mapping):
    """Build a deep-frozen policy mapping exactly like production does
    (the gate's ``_frozen_policy_mapping``): a ``MappingProxyType`` whose
    nested dict values are frozen too. Used by controlled mocks that
    replace ``ALLOWED_TESTNET_CHAINS`` / ``KNOWN_TARGETS`` wholesale."""
    return _gate._frozen_policy_mapping(dict(mapping))


@contextlib.contextmanager
def offline_transports(rpc=None, relay=None):
    """Redirect the gate's PRIVATE transport builders to controlled mock
    transports for the duration of the block (DSS-WO-ENG-015 R3).

    ``rpc`` replaces the product of ``_build_rpc_transport`` — the mock
    direct-RPC transport used by ``sign_and_submit_testnet_tx``.
    ``relay`` replaces the product of ``_build_relay_transport`` — the
    mock Flashbots relay transport used by ``prepare_flashbots_bundle``.

    The gate's real transport construction NEVER runs while the patch is
    active: no production network object is created, nothing touches a
    network. This is the controlled-mock seam for the transport builders —
    the equivalent of ``throwaway_store`` for the custody/audit constants
    and of patching the allowlist map: a test technique, NOT an
    operational interface, and it cannot weaken any enforcement (the
    orchestrations still verify the REPORTED chain ID against the
    code-controlled allowlist and the configured expectation at every
    checkpoint).
    """
    chosen: list = []
    if rpc is not None:
        chosen.append(patch.object(
            _gate, "_build_rpc_transport", lambda url: rpc,
        ))
    if relay is not None:
        chosen.append(patch.object(
            _gate, "_build_relay_transport",
            lambda rpc_url, auth_account: relay,
        ))
    with contextlib.ExitStack() as stack:
        for p in chosen:
            stack.enter_context(p)
        yield
