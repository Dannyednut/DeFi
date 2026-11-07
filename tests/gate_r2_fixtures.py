"""DSS-WO-ENG-015 R2 — TEST-ONLY isolation helpers for the security policy.

PRODUCTION CODE MUST NEVER IMPORT THIS MODULE. It exists so the offline
suite can keep exercising the gate orchestrations deterministically after
R2 made the security policy non-configurable:

  * the wallet custody locations, the audit destination and the reviewed
    relay destination are code-controlled policy constants (the
    ``SECURITY_POLICY`` singleton in ``executor.testnet_gate`` reads the
    reviewed module constants at access time);
  * no production interface accepts any of them from a caller.

The sanctioned offline-test technique (identical to the established
controlled-mock pattern for ``ALLOWED_TESTNET_CHAINS`` and
``KNOWN_TARGETS``) is to patch the reviewed MODULE CONSTANTS for the
duration of a test:

  ``throwaway_store(tmpdir)`` redirects the custody and audit constants
  to a throwaway store; ``relay_url=...`` optionally redirects the relay
  constant for credential-containment tests. Both are pure
  ``unittest.mock.patch.object`` scopes — they are a test technique,
  NOT an operational interface, and they cannot weaken any enforcement:
  the orchestrations always read the constants at execution time.

  ``IsolatedTestnetConfig`` is a thin TEST-ONLY subclass of the slim
  operational ``TestnetExecutionConfig``. It accepts the security-named
  keyword arguments the pre-R2 tests used (keystore_path, passphrase_path,
  address_path, tx_log_path, relay_url) ONLY to keep those call sites
  meaningful: every such argument is VALIDATED against the live
  ``SECURITY_POLICY`` value and a mismatch is a loud test error, and the
  attributes are re-exposed as live reads of ``SECURITY_POLICY`` — so a
  test asserting against ``cfg.tx_log_path`` asserts against exactly the
  destination production writes to. It carries NO authorization state and
  cannot weaken any security control (its parent is operational-only).
"""
from __future__ import annotations

import contextlib
import os
from unittest.mock import patch

import executor.testnet_gate as _gate
from executor.testnet_gate import TestnetExecutionConfig

__all__ = ["throwaway_store", "IsolatedTestnetConfig"]


@contextlib.contextmanager
def throwaway_store(tmpdir: str | None = None, *, relay_url: str | None = None):
    """Redirect the gate's code-controlled custody/audit constants (and
    optionally the reviewed relay constant) to a throwaway store for the
    duration of the block.

    ``tmpdir`` redirects keystore/passphrase/address/audit paths into the
    throwaway directory; with ``tmpdir=None`` only the relay patch (if
    requested) is applied — for tests that exercise URL-material
    registration without touching the store.

    This is the controlled-mock seam for offline tests — the equivalent of
    patching ``ALLOWED_TESTNET_CHAINS``: production never sees it, every
    enforcement point still reads the constants at execution time, and no
    API parameter is involved.
    """
    patches = []
    if tmpdir is not None:
        patches.extend([
            patch.object(_gate, "DEFAULT_KEYSTORE_PATH",
                         os.path.join(tmpdir, "keystore.json")),
            patch.object(_gate, "DEFAULT_PASSPHRASE_PATH",
                         os.path.join(tmpdir, "pass")),
            patch.object(_gate, "DEFAULT_ADDRESS_PATH",
                         os.path.join(tmpdir, "address")),
            patch.object(_gate, "DEFAULT_TX_LOG_PATH",
                         os.path.join(tmpdir, "txlog.jsonl")),
        ])
    if relay_url is not None:
        patches.append(
            patch.object(_gate, "FLASHBOTS_SEPOLIA_RELAY", relay_url)
        )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


_SECURITY_KWARG_NAMES = (
    "keystore_path",
    "passphrase_path",
    "address_path",
    "tx_log_path",
    "relay_url",
)


class IsolatedTestnetConfig(TestnetExecutionConfig):
    """TEST-ONLY operational config whose security-named attributes are
    LIVE reads of the gate's code-controlled ``SECURITY_POLICY``.

    * Constructor security kwargs are accepted for pre-R2 call-site
      compatibility and are VALIDATED against the live policy values —
      a mismatch (e.g. the test forgot to activate ``throwaway_store``)
      is a loud AssertionError, never a silent divergence.
    * The security-named attributes are read-only properties delegating
      to ``SECURITY_POLICY``, so assertions against them always see the
      values production enforces.
    * The parent is operational-only (no enabled/allowlist/limit fields,
      R1/R2), so this class cannot weaken any security control. It is
      never imported by production code.
    """

    # Immutable like the parent: no new instance state can be attached.
    # (A plain subclass of a frozen dataclass is not itself frozen, so the
    # refusal is explicit — same pattern as PreparedFlashbotsBundle.)
    __slots__ = ()

    def __setattr__(self, name, value):
        raise AttributeError(
            "IsolatedTestnetConfig is immutable: attribute assignment is "
            "refused (test-only operational config; the security policy is "
            "code-controlled and cannot be altered from here)"
        )

    def __delattr__(self, name):
        raise AttributeError(
            "IsolatedTestnetConfig is immutable: attribute deletion is refused"
        )

    # Not a pytest test class despite the Test prefix.
    __test__ = False

    def __init__(self, *, rpc_url=None, expected_chain_id=11155111, **security):
        unknown = set(security) - set(_SECURITY_KWARG_NAMES)
        if unknown:
            raise TypeError(
                f"IsolatedTestnetConfig got unexpected keyword arguments: "
                f"{sorted(unknown)}"
            )
        for name, value in security.items():
            live = getattr(_gate.SECURITY_POLICY, name)
            if value != live:
                raise AssertionError(
                    f"test configuration {name}={value!r} does not match the "
                    f"live SECURITY_POLICY value {live!r} — activate "
                    f"throwaway_store(...) for this region (test technique "
                    "only; production accepts no such override)"
                )
        super().__init__(rpc_url=rpc_url, expected_chain_id=expected_chain_id)

    # Live reads of the code-controlled policy (no stored values).
    @property
    def keystore_path(self):
        return _gate.SECURITY_POLICY.keystore_path

    @property
    def passphrase_path(self):
        return _gate.SECURITY_POLICY.passphrase_path

    @property
    def address_path(self):
        return _gate.SECURITY_POLICY.address_path

    @property
    def tx_log_path(self):
        return _gate.SECURITY_POLICY.tx_log_path

    @property
    def relay_url(self):
        return _gate.SECURITY_POLICY.relay_url
