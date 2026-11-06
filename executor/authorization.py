"""Unified execution authorization boundary (DSS-WO-ENG-015).

THE single mandatory authorization boundary for every transaction-producing
route in the DSS agent. This module resolves WHICH execution policy applies,
authorizes or refuses transaction signing, and hands out the only sanctioned
signing/submission capabilities — both of which are the existing
``executor.testnet_gate`` orchestrations:

  * ``AuthorizedExecution.sign_and_submit``  → ``sign_and_submit_testnet_tx``
    (direct-RPC transport: chain verification before signing AND immediately
    before submission, transaction policy, wallet custody binding, audit);
  * ``AuthorizedExecution.prepare_flashbots_bundle`` →
    ``prepare_flashbots_bundle`` (Flashbots transport: gate-controlled
    capability/session model; simulate-before-submit; payload containment).

This module does NOT reimplement any authorization check: policy selection
here only decides WHICH policy is consulted, and every actual check (explicit
opt-in, allowlist, chain identity, wallet custody, transaction policy, fee
exposure, audit) stays in ``executor.testnet_gate`` — there is exactly one
authorization implementation.

Design invariants (all fail closed):

  * ``CHAIN_ID`` is network SELECTION only. It is never read by policy
    resolution and can never authorize execution by itself.
  * The ONLY operationally selectable policy is the explicit Sepolia testnet
    policy, which requires ``DSS_TESTNET_EXEC=1`` plus a fully provisioned
    secret store (checked by the gate's ``require_ready`` at every
    authorization).
  * There is NO production/mainnet execution policy. A named
    ``ProductionExecutionPolicy`` class exists to make the architecture
    explicit; its ``authorize`` ALWAYS refuses and ``execution_available`` is
    hard-coded False. Enabling production execution requires a separately
    reviewed authorization decision (a code change, never an environment
    setting). ``EXECUTE_ONCHAIN`` cannot select, enable or bypass any policy.
  * Without ``DSS_TESTNET_EXEC=1`` the ``DisabledExecutionPolicy`` applies:
    every signing/submission request is refused with
    ``AuthorizationRefused`` and no wallet is ever decrypted.
  * The runtime chain (executor's configured chain ID) must equal the
    policy's expected chain, and the gate re-verifies the RPC-REPORTED chain
    immediately before signing and again immediately before submission — a
    mismatched, unavailable or unverifiable RPC is a refusal.
  * The boundary never returns or logs key material. ``AuthorizedExecution``
    carries the recorded PUBLIC agent address only; the private key exists
    exclusively inside the gate's wallet-custody path for the duration of a
    signing operation.
  * Refused execution attempts are audited (outcome ``refused``) whenever an
    execution context exists (a selected policy with its transaction audit
    log). When no policy is configured there is no execution context —
    nothing could ever sign — and refusals are surfaced through the standard
    logger only (this also keeps disabled-by-default runs from writing audit
    files).

HONEST BOUNDARY (inherited from executor/testnet_gate.py): this is an
application-level boundary for the agent's own code paths. Arbitrary hostile
code executing inside the same interpreter can defeat Python-level
protections; no cryptographic unforgeability against same-process code is
claimed. What this module removes is the STRUCTURAL legacy route: the
import-time ``PRIVATE_KEY`` signer and the executor's private relay-identity
provisioning no longer exist, so agent code cannot sign or broadcast outside
the gate even by accident.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .testnet_gate import (
    TestnetExecutionConfig,
    TestnetSafetyError,
    _sanitize_error,
    _registered_secrets,
    prepare_flashbots_bundle,
    record_testnet_tx,
    sign_and_submit_testnet_tx,
)

from log import get_logger

log = get_logger("executor.authorization")


class AuthorizationRefused(TestnetSafetyError):
    """Raised when the unified boundary refuses an execution request.

    Subclasses ``TestnetSafetyError`` so every existing fail-closed handler
    for gate refusals also handles boundary refusals."""

    # Not a pytest test class despite the Test prefix.
    __test__ = False


# ── Execution policies ───────────────────────────────────────────────────────


class ExecutionPolicy:
    """Base class for execution policies.

    A policy answers exactly one question: may this request be authorized,
    and if so, which gate configuration governs it? The actual authorization
    checks live in the gate."""

    name = "base"
    network = "none"  # "none" | "testnet" | "production"

    def authorize(
        self, purpose: str, chain_id: Optional[int] = None
    ) -> TestnetExecutionConfig:
        """Return the governing gate config, or raise ``TestnetSafetyError``."""
        raise AuthorizationRefused(
            "execution policy does not authorize any execution"
        )

    def execution_available(self) -> bool:
        """Cheap readiness probe (no wallet decryption, no network I/O)."""
        return False

    def refusal_reason(self) -> str:
        return "no execution policy is enabled"


class DisabledExecutionPolicy(ExecutionPolicy):
    """Default policy: NOTHING may be signed or submitted.

    Applies whenever the explicit testnet opt-in is absent. There is no
    configuration that turns execution on from here — only
    ``DSS_TESTNET_EXEC=1`` selects the testnet policy."""

    name = "disabled"
    network = "none"

    def authorize(
        self, purpose: str, chain_id: Optional[int] = None
    ) -> TestnetExecutionConfig:
        raise AuthorizationRefused(
            "execution refused: no execution policy is enabled "
            "(EXECUTE_ONCHAIN is the production switch and stays false; "
            "DSS_TESTNET_EXEC is unset — the authorization boundary "
            "refuses to load a wallet, sign or submit)"
        )

    def refusal_reason(self) -> str:
        return (
            "no execution policy enabled: DSS_TESTNET_EXEC is unset "
            "(testnet execution requires the explicit opt-in) and no "
            "production policy is authorized"
        )


class TestnetExecutionPolicy(ExecutionPolicy):
    """The explicitly authorized Sepolia testnet policy.

    Every authorization runs the gate's ``require_ready`` — the explicit
    ``DSS_TESTNET_EXEC=1`` opt-in, allowlist membership of the expected
    chain and the full secret-store custody checks — and additionally
    requires the requested runtime chain to equal the policy's configured
    chain. The gate then re-verifies the RPC-reported chain ID before
    signing and before submission."""

    # Not a pytest test class despite the Test prefix.
    __test__ = False

    name = "sepolia_testnet"
    network = "testnet"

    def __init__(self, config: TestnetExecutionConfig):
        self._config = config

    @property
    def config(self) -> TestnetExecutionConfig:
        return self._config

    def authorize(
        self, purpose: str, chain_id: Optional[int] = None
    ) -> TestnetExecutionConfig:
        # Gate refusal (disabled gate, missing provisioning, custody
        # failure) propagates unchanged — a policy refusal keeps its exact
        # type and message.
        self._config.require_ready()
        if chain_id is not None and int(chain_id) != int(
            self._config.expected_chain_id
        ):
            raise AuthorizationRefused(
                f"execution requested for chain {chain_id} but the "
                f"authorized testnet policy binds chain "
                f"{self._config.expected_chain_id} — refusing (network "
                "selection is not execution authorization)"
            )
        return self._config

    def execution_available(self) -> bool:
        try:
            self._config.require_ready()
            return True
        except TestnetSafetyError:
            return False

    def refusal_reason(self) -> str:
        try:
            self._config.require_ready()
        except TestnetSafetyError as exc:
            return f"testnet policy not ready: {exc}"
        return "testnet policy ready"


class ProductionExecutionPolicy(ExecutionPolicy):
    """The production execution policy — PERMANENTLY DISABLED.

    This class exists so the architecture explicitly shows where a
    production policy WOULD live. It authorizes nothing: enabling it
    requires a separately reviewed authorization decision recorded as a
    code change. ``EXECUTE_ONCHAIN`` cannot enable it."""

    name = "production"
    network = "production"

    def authorize(
        self, purpose: str, chain_id: Optional[int] = None
    ) -> TestnetExecutionConfig:
        raise AuthorizationRefused(
            "production execution policy is DISABLED (DSS-WO-ENG-015): "
            "no production/mainnet execution is authorized; enabling it "
            "requires a separately reviewed authorization decision"
        )

    def execution_available(self) -> bool:
        return False  # hard-coded: never available from the environment

    def refusal_reason(self) -> str:
        return "production execution policy is disabled"


def resolve_execution_policy(env: Optional[dict] = None) -> ExecutionPolicy:
    """THE single authoritative policy-selection point.

    Reads only the execution-gate variables via the gate's own
    ``TestnetExecutionConfig.from_env``:

      * ``DSS_TESTNET_EXEC=1`` (+ ``DSS_TESTNET_RPC_URL``, optionally
        ``DSS_TESTNET_CHAIN_ID``) selects the Sepolia testnet policy;
      * anything else selects the disabled policy.

    ``EXECUTE_ONCHAIN`` is read ONLY to emit an explicit warning that it
    cannot authorize execution. ``CHAIN_ID`` is never read here at all:
    network selection is not execution authorization.
    """
    e = env if env is not None else os.environ
    if str(e.get("EXECUTE_ONCHAIN", "false")).lower() == "true":
        log.warning(
            "EXECUTE_ONCHAIN=true requested, but no production execution "
            "policy exists: the unified authorization boundary refuses "
            "production/mainnet execution regardless of this switch"
        )
    cfg = TestnetExecutionConfig.from_env(env)
    if cfg.enabled:
        return TestnetExecutionPolicy(cfg)
    return DisabledExecutionPolicy()


# ── The authorized execution handle ──────────────────────────────────────────


@dataclass(frozen=True)
class AuthorizedExecution:
    """One authorized execution context minted by the boundary.

    Carries the governing gate configuration and the recorded PUBLIC agent
    address. It holds NO key material: the wallet is decrypted only inside
    the gate orchestrations, per signing operation. Every method delegates
    to the gate — there is no second signing or submission implementation.
    """

    policy_name: str
    network: str
    chain_id: int
    sender: str
    purpose: str
    config: TestnetExecutionConfig = field(repr=False, compare=False)

    # Not a pytest test class despite the Test prefix.
    __test__ = False

    def sign_and_submit(
        self,
        tx: dict,
        *,
        known_targets: Optional[dict[int, dict[str, str]]] = None,
        receipt_timeout: int = 120,
        web3_factory: Optional[Callable[[str], Any]] = None,
    ) -> dict:
        """THE only direct-RPC signing-and-submission route.

        Delegates to the gate orchestration: gate enablement, RPC chain
        verification (before signing AND before submission), transaction
        policy, wallet custody binding, submission, receipt and audit.
        Returns the gate's audit record; raises ``TestnetSafetyError`` on
        any refusal or failure. Signed raw bytes never leave the gate.
        """
        return sign_and_submit_testnet_tx(
            tx,
            purpose=self.purpose,
            config=self.config,
            web3_factory=web3_factory,
            known_targets=known_targets,
            receipt_timeout=receipt_timeout,
        )

    def prepare_flashbots_bundle(
        self,
        unsigned_txs: Sequence[dict],
        *,
        known_targets: Optional[dict[int, dict[str, str]]] = None,
        web3_factory: Optional[Callable[[str], Any]] = None,
        relay_factory: Optional[Callable[[str, Any, str], Any]] = None,
    ) -> Any:
        """THE only Flashbots bundle preparation route.

        Delegates to the gate: same explicit gate, chain verification,
        transaction policy (including the aggregate bundle fee cap), wallet
        custody binding and signing. Returns ONLY the controlled
        ``FlashbotsBundleSession`` — signed raw bytes never leave the gate.
        """
        return prepare_flashbots_bundle(
            [dict(t) for t in unsigned_txs],
            purpose=self.purpose,
            config=self.config,
            web3_factory=web3_factory,
            relay_factory=relay_factory,
            known_targets=known_targets,
        )


# ── The boundary ─────────────────────────────────────────────────────────────


class ExecutionAuthorizationBoundary:
    """THE mandatory execution authorization boundary.

    Every component that intends to produce a signed transaction must obtain
    an :class:`AuthorizedExecution` from ``authorize_execution`` first. There
    is no route around it: the legacy signer module no longer exists, the
    executor's relay-identity provisioning no longer exists, and the raw
    broadcast helpers were removed from ``executor.relay`` — the only
    functions in the repository that sign or broadcast transactions are the
    gate orchestrations, which are reachable only through this boundary (or
    through the gate module directly, which enforces the identical checks).
    """

    def __init__(
        self,
        policy: Optional[ExecutionPolicy] = None,
        env: Optional[dict] = None,
    ):
        self._policy = (
            policy if policy is not None else resolve_execution_policy(env)
        )

    @property
    def policy(self) -> ExecutionPolicy:
        return self._policy

    def execution_available(self) -> bool:
        """Cheap readiness probe for pipeline wiring (router start-up).

        No wallet decryption, no network I/O. True only when a policy is
        selected AND its provisioning checks pass."""
        return self._policy.execution_available()

    def refusal_reason(self) -> str:
        return self._policy.refusal_reason()

    def authorize_execution(
        self, purpose: str, chain_id: Optional[int] = None
    ) -> AuthorizedExecution:
        """Authorize one execution context, fail closed on every refusal.

        Refusals are audited (outcome ``refused``) whenever the selected
        policy provides an execution context (audit log path); with no
        policy configured the refusal is logged and nothing else happens —
        no wallet is decrypted, no RPC is contacted.
        """
        try:
            cfg = self._policy.authorize(purpose, chain_id)
        except TestnetSafetyError as exc:
            self._audit_refusal(purpose, chain_id, exc)
            raise
        return AuthorizedExecution(
            policy_name=self._policy.name,
            network=self._policy.network,
            chain_id=int(cfg.expected_chain_id),
            sender=cfg.load_agent_address(),
            purpose=purpose,
            config=cfg,
        )

    def sender_address_for_chain(self, chain_id: Optional[int] = None) -> str:
        """The authorized PUBLIC sender address for transaction/simulation
        construction — no key material, ever.

        Runs the same authorization checks (without auditing: this is a
        read-only probe, not an execution attempt) and returns the recorded
        agent address. Raises ``TestnetSafetyError`` when no policy is
        available or the chain does not match the policy's chain.
        """
        cfg = self._policy.authorize("authorized-sender-address", chain_id)
        return cfg.load_agent_address()

    def _audit_refusal(
        self,
        purpose: str,
        chain_id: Optional[int],
        exc: TestnetSafetyError,
    ) -> None:
        """Audit one refused execution attempt (DSS-WO-ENG-015 §4).

        Audited ONLY when the selected policy carries an execution context
        (a testnet policy with its transaction audit log): an enabled-but-
        failing policy refusing a real attempt must leave a record. With no
        policy configured there is no execution context (nothing could ever
        sign, no wallet exists) — the refusal is logged and no file is
        written, keeping disabled-by-default runs free of audit writes.
        """
        log.warning(
            "execution authorization refused | purpose=%s chain=%s reason=%s",
            purpose,
            chain_id,
            exc,
        )
        cfg = getattr(self._policy, "config", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return
        try:
            record_testnet_tx(
                cfg.tx_log_path,
                purpose=purpose,
                chain_id=chain_id,
                target=None,
                function=None,
                estimated_gas=None,
                actual_gas_used=None,
                tx_hash=None,
                outcome="refused",
                error=_sanitize_error(str(exc), *_registered_secrets(cfg)),
                max_fee_exposure_wei=None,
            )
        except Exception as audit_exc:  # auditing must never mask the refusal
            log.error(
                "failed to write refusal audit record: %s",
                type(audit_exc).__name__,
            )


# ── Process-wide default boundary ────────────────────────────────────────────
#
# main.py wires ONE boundary instance into the executor and the router so
# policy selection happens once per process. Components constructed without
# an explicit boundary get a lazily resolved default built from the
# environment at first use (never at import time). Offline tests may inject
# their own boundary/policy instances explicitly.

_DEFAULT_BOUNDARY: Optional[ExecutionAuthorizationBoundary] = None
_DEFAULT_BOUNDARY_LOCK = threading.Lock()


def get_execution_authorization() -> ExecutionAuthorizationBoundary:
    """Return the process-wide default boundary (lazily resolved)."""
    global _DEFAULT_BOUNDARY
    if _DEFAULT_BOUNDARY is None:
        with _DEFAULT_BOUNDARY_LOCK:
            if _DEFAULT_BOUNDARY is None:
                _DEFAULT_BOUNDARY = ExecutionAuthorizationBoundary()
    return _DEFAULT_BOUNDARY


def reset_execution_authorization() -> None:
    """Drop the cached default boundary (offline test isolation only)."""
    global _DEFAULT_BOUNDARY
    with _DEFAULT_BOUNDARY_LOCK:
        _DEFAULT_BOUNDARY = None


def authorized_sender_address(chain_id: Optional[int] = None) -> str:
    """Convenience access to the authorized PUBLIC sender address via the
    default boundary.

    Used by validation modules that need the `from` address for exact
    execution simulations. Raises ``TestnetSafetyError`` when no execution
    policy is available — validation that cannot know the authorized sender
    must not claim execution readiness (fail closed, exactly like the
    legacy signer's behavior when no key was configured).
    """
    return get_execution_authorization().sender_address_for_chain(chain_id)
