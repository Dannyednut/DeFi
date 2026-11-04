"""
tests/test_flashbots_integration.py
====================================
OPT-IN Flashbots Sepolia integration test — DSS agent wallet only, through
the unified testnet gate (DSS-WO-ENG-004; capability-gated per
DSS-WO-ENG-005).

History and safety contract:

1. The ORIGINAL version of this file executed at IMPORT time — it loaded
   PRIVATE_KEY from .env, built a signer, dialed a live RPC, signed a
   transaction, simulated a bundle and BROADCAST it via
   eth_sendPrivateTransaction. Collecting the file was enough to move
   money. That script was removed in DSS-WO-ENG-003 (Workstream B) and
   rewritten again in DSS-WO-ENG-004 and DSS-WO-ENG-005: importing or
   collecting this module performs NO key loading, NO network activity
   and NO signing. Only stdlib + already-installed package imports run at
   module level.

2. Everything is disabled by default. Live tests skip visibly with their
   reason unless every required testnet-only flag and credential is
   present (see _skip_reason()).

3. SINGLE wallet path (DSS-WO-ENG-004 §2): the wallet is loaded EXCLUSIVELY
   by executor.testnet_gate.load_agent_wallet from the DSS secure store
   (encrypted v3 keystore + hex-encoded passphrase file). This file has no
   key-handling code of its own — no direct keystore decryption call, no
   legacy key access, no .env reading, no passphrase environment override,
   no keystore-path override. There is no code path to any other wallet.

4. Authorization lives in the gate (DSS-WO-ENG-004 §3, DSS-WO-ENG-005
   §2–§3): every live operation goes through prepare_flashbots_bundle →
   PreparedFlashbotsBundle (capability) → FlashbotsBundleSession.
   Preparation DECRYPTS the wallet and SIGNS, so it requires the SAME
   explicit execution gate (DSS_TESTNET_EXEC=1) as direct-RPC submission —
   with the gate disabled, nothing decrypts and nothing signs. The session
   can only be created from a capability minted by the sanctioned
   preparation path; simulate()/submit() take no payload, so an arbitrary
   bundle can never gain submission authority. Wallet identity binding,
   the Sepolia-only production allowlist, mainnet hard rejection,
   RPC chain-ID verification before signing and immediately before
   submission, transaction policy (incl. per-transaction AND aggregate
   bundle fee-exposure caps) and audit records are all enforced in
   production code. The relay is a transport only; it never creates an
   independent authorization path.

5. Simulation-before-submission is ENFORCED IN PRODUCTION CODE
   (DSS-WO-ENG-004 §4, DSS-WO-ENG-005 §4): FlashbotsBundleSession.submit
   refuses to call the relay unless the same session previously simulated
   the EXACT prepared payload for the EXACT target block successfully,
   the target block is still in the future, the chain identity still
   matches and the execution gate is set. The one-use authorization is
   consumed BEFORE the relay call, so a relay timeout cannot authorize
   unlimited retries.

6. The test transaction is a 0-value self-transfer from the agent wallet
   to itself: no third party can receive anything, and the only possible
   cost is gas on testnet ETH, bounded by the gate's monetary fee caps.
   The approved 0.002 native ceiling is a ONE-transaction maximum for the
   initial operation (21,000 × 50 gwei = 0.00105 native worst case); two
   full worst-case transactions would total 0.0021 native and EXCEED it —
   any retry requires a new authorization (DSS-WO-ENG-005 §6).

7. CI never signs, broadcasts, deploys or submits: CI provides none of the
   required flags or credentials, so both live tests skip.

Endpoint provenance (verified 2026-11-04): the Sepolia bundle relay
https://relay-sepolia.flashbots.net is the currently documented Flashbots
testnet relay (docs.flashbots.net, "Flashbots Auction > Testnets", page
last updated 2025-12-16). No endpoint is invented here; if Flashbots
withdraws Sepolia support, the live run will fail at the relay and this
file's documentation must be updated rather than silently pointed
elsewhere.

Gating environment variables (agent runtime only, never set in CI):
    DSS_TESTNET_FLASHBOTS_LIVE=1        opt in to live testnet interaction
    DSS_TESTNET_RPC_URL=<https url>     Sepolia RPC endpoint
    DSS_TESTNET_EXEC=1                  testnet execution gate — REQUIRED
                                        for BOTH phases, because Flashbots
                                        preparation signs (DSS-WO-ENG-005
                                        §2); submission re-checks it at
                                        submit() time

There is deliberately NO environment variable for the keystore path, the
passphrase or any alternative wallet: the recorded DSS agent identity
cannot be bypassed (DSS-WO-ENG-004 §2 reqs 5–6).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account  # noqa: F401 — throwaway keys for offline tests

from executor.testnet_gate import (
    FLASHBOTS_SEPOLIA_RELAY,
    MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
    MAX_TESTNET_TX_FEE_WEI,
    TestnetExecutionConfig,
    TestnetSafetyError,
    _payload_fingerprint,
    prepare_flashbots_bundle,
    verify_secret_store,
)

SEPOLIA_CHAIN_ID = 11155111

# The test transaction moves zero value. Gas on Sepolia ETH is the only
# possible expenditure, and only from the DSS agent wallet.
SELF_TRANSFER_VALUE_WEI = 0


def _skip_reason() -> str | None:
    """Return a human-readable skip reason, or None if fully provisioned.

    Touches only local files and environment flags — no network, no keys.
    """
    if os.environ.get("DSS_TESTNET_FLASHBOTS_LIVE") != "1":
        return (
            "opt-in live testnet test: set DSS_TESTNET_FLASHBOTS_LIVE=1 "
            "(never set in CI)"
        )
    # Secret store presence + permissions through the gate's custody check.
    # The config carries no RPC URL yet; verify only the secret-store part.
    probe = TestnetExecutionConfig(enabled=True, rpc_url=None)
    try:
        verify_secret_store(probe)
    except TestnetSafetyError as exc:
        return f"DSS agent secret store failed custody check: {exc}"
    if not os.environ.get("DSS_TESTNET_RPC_URL"):
        return "DSS_TESTNET_RPC_URL not configured"
    return None


def _read_recorded_address(cfg: TestnetExecutionConfig) -> str:
    """Public metadata only — the recorded agent address, read through the
    gate so a malformed record fails closed."""
    return cfg.load_agent_address()


class TestWalletIdentityBindings(unittest.TestCase):
    """Offline pins for the single wallet path (DSS-WO-ENG-004 §2).

    The live wallet is NEVER touched here: every wallet-bearing test uses
    a throwaway key in a temporary directory. Deep custody coverage
    (roundtrip, mismatch, symlink, permissions, hex format) lives in
    tests/test_testnet_gate.py against the same production function.
    """

    def test_from_env_provides_no_wallet_override(self):
        """No environment variable can redirect the wallet: from_env only
        reads the exec flag, RPC URL and expected chain id. The credential
        paths are structural defaults pointing at the DSS secret store."""
        cfg = TestnetExecutionConfig.from_env(env={
            "DSS_TESTNET_EXEC": "1",
            "DSS_TESTNET_RPC_URL": "https://sepolia.example",
            # Key names built via concatenation so this file's source does
            # not contain the override variable names verbatim (the source
            # pin below asserts they appear nowhere in this file). The gate
            # must ignore them completely.
            ("DSS_TESTNET_KEYSTORE" + ""): "/tmp/evil.json",
            ("DSS_TESTNET_KEYSTORE" + "_PASSPHRASE"): "evil",
            "PRIVATE" + "_KEY": "evil",
        })
        self.assertEqual(
            cfg.keystore_path,
            os.path.join("/home/z/my-project/.secrets",
                         "dss_testnet_wallet.keystore.json"),
        )
        self.assertEqual(
            cfg.passphrase_path,
            os.path.join("/home/z/my-project/.secrets",
                         "dss_testnet_wallet.pass"),
        )
        self.assertEqual(
            cfg.address_path,
            os.path.join("/home/z/my-project/.secrets",
                         "dss_testnet_wallet.address"),
        )
        self.assertEqual(cfg.relay_url, FLASHBOTS_SEPOLIA_RELAY)

    def test_source_pins_no_independent_wallet_or_env_path(self):
        """This file must not contain any independent wallet-loading,
        passphrase-decoding or legacy-key path — and no capability-minting
        shortcut. The gate is the only wallet AND session authority
        (DSS-WO-ENG-004 §2 reqs 5–6, §3; DSS-WO-ENG-005 §3)."""
        with open(__file__, "r") as f:
            src = f.read()
        # Built via concatenation so this file's own source does not contain
        # the forbidden literals verbatim.
        getenv = "os.get" + "env("
        environ_get = "os.environ.get("
        forbidden = (
            getenv + '"PRIVATE' + '_KEY")',
            environ_get + '"PRIVATE' + '_KEY")',
            "load_dot" + "env",
            "Account." + "decrypt",
            "Account." + "from_key",
            "DSS_TESTNET_KEYSTORE" + "_PASSPHRASE",
            environ_get + '"DSS_TESTNET' + '_KEYSTORE")',
            "send_" + "private_transaction",
            "_PREPARATION" + "_AUTHORITY",
            "._" + "mint(",
        )
        for item in forbidden:
            self.assertNotIn(item, src)

    def test_skip_reason_is_visible_and_secret_free(self):
        reason = _skip_reason()
        self.assertIsNotNone(reason)
        self.assertIn("DSS_TESTNET_FLASHBOTS_LIVE", reason)
        self.assertNotIn("passphrase", reason.lower())


class TestPreparePathOffline(unittest.TestCase):
    """The unified prepare path with a throwaway wallet — no network."""

    def _provisioned(self, tmp):
        acct = Account.create()
        keystore = Account.encrypt(acct.key, b"throwaway-passphrase")

        def w(path, data, mode=0o600):
            with open(path, "wb") as f:
                f.write(data)
            os.chmod(path, mode)

        w(os.path.join(tmp, "keystore.json"), json.dumps(keystore).encode())
        w(os.path.join(tmp, "pass"), b"throwaway-passphrase".hex().encode())
        w(os.path.join(tmp, "address"), (acct.address + "\n").encode())
        cfg = TestnetExecutionConfig(
            # Preparation signs, so the FULL gate (enabled=True) is the
            # honest state to test (DSS-WO-ENG-005 §2).
            enabled=True,
            rpc_url="https://sepolia.example",
            keystore_path=os.path.join(tmp, "keystore.json"),
            passphrase_path=os.path.join(tmp, "pass"),
            address_path=os.path.join(tmp, "address"),
            tx_log_path=os.path.join(tmp, "txlog.jsonl"),
        )
        return cfg, acct

    def _mock_w3(self):
        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth.chain_id = SEPOLIA_CHAIN_ID
        w3.eth.block_number = 100
        w3.eth.get_transaction_count.return_value = 7
        w3.eth.get_block.return_value = {"baseFeePerGas": 10**9}
        w3.to_wei.side_effect = lambda v, u: int(v * 10**9) if u == "gwei" else v
        return w3

    def test_prepare_without_execution_gate_never_decrypts_or_signs(self):
        """DSS-WO-ENG-005 §2: with the gate disabled, Flashbots
        preparation must refuse BEFORE the wallet is decrypted and before
        the relay transport is built."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, acct = self._provisioned(tmp)
            disabled = TestnetExecutionConfig(
                enabled=False,
                rpc_url=cfg.rpc_url,
                keystore_path=cfg.keystore_path,
                passphrase_path=cfg.passphrase_path,
                address_path=cfg.address_path,
                tx_log_path=cfg.tx_log_path,
            )
            relay = MagicMock()
            with patch(
                "executor.testnet_gate.load_agent_wallet",
                side_effect=AssertionError("wallet must never be decrypted"),
            ):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    prepare_flashbots_bundle(
                        [{"to": acct.address, "value": SELF_TRANSFER_VALUE_WEI}],
                        purpose="must never prepare",
                        config=disabled,
                        web3_factory=lambda url: self._mock_w3(),
                        relay_factory=lambda url, auth, relay_url: relay,
                    )
            self.assertIn("DISABLED", str(ctx.exception))
            relay.assert_not_called()

    def test_prepare_signs_bound_wallet_and_never_touches_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, acct = self._provisioned(tmp)
            w3 = self._mock_w3()
            relay = MagicMock()
            # The session constructor re-verifies the transport chain
            # binding, so the mock relay must report the expected chain.
            relay.eth.chain_id = SEPOLIA_CHAIN_ID
            session, signed, target_block = prepare_flashbots_bundle(
                [{"to": acct.address, "value": SELF_TRANSFER_VALUE_WEI}],
                purpose="offline prepare check",
                config=cfg,
                web3_factory=lambda url: w3,
                relay_factory=lambda url, auth, relay_url: relay,
            )
            self.assertEqual(target_block, 101)
            self.assertEqual(len(signed), 1)
            self.assertIsInstance(signed[0], bytes)
            self.assertTrue(signed[0])
            relay.assert_not_called()  # no transport interaction in prepare
            # The session is bound to the verified wallet identity through
            # the capability (DSS-WO-ENG-005 §3).
            self.assertEqual(
                session._prepared.sender.lower(), acct.address.lower()
            )
            self.assertEqual(
                session._prepared.fingerprint,
                _payload_fingerprint([bytes(signed[0])], target_block),
            )

    def test_prepare_refuses_policy_violation_before_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, acct = self._provisioned(tmp)
            stranger = "0x" + "bb" * 20
            relay = MagicMock()
            with self.assertRaises(TestnetSafetyError) as ctx:
                prepare_flashbots_bundle(
                    [{"to": stranger, "value": 1}],
                    purpose="must never prepare",
                    config=cfg,
                    web3_factory=lambda url: self._mock_w3(),
                    relay_factory=lambda url, auth, relay_url: relay,
                )
            self.assertIn("no arbitrary EOA transfers", str(ctx.exception))
            relay.assert_not_called()

    def test_prepare_refuses_allowlisted_but_unexpected_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, acct = self._provisioned(tmp)
            cfg = TestnetExecutionConfig(
                enabled=True,
                rpc_url=cfg.rpc_url,
                keystore_path=cfg.keystore_path,
                passphrase_path=cfg.passphrase_path,
                address_path=cfg.address_path,
                tx_log_path=cfg.tx_log_path,
                expected_chain_id=SEPOLIA_CHAIN_ID,
                # controlled test allowlist; production stays Sepolia-only
                allowlist={SEPOLIA_CHAIN_ID: "Ethereum Sepolia", 17000: "Holesky"},
            )
            w3 = self._mock_w3()
            w3.eth.chain_id = 17000
            relay = MagicMock()
            with self.assertRaises(TestnetSafetyError) as ctx:
                prepare_flashbots_bundle(
                    [{"to": acct.address, "value": 0}],
                    purpose="must never prepare",
                    config=cfg,
                    web3_factory=lambda url: w3,
                    relay_factory=lambda url, auth, relay_url: relay,
                )
            self.assertIn("RPC chain mismatch", str(ctx.exception))
            relay.assert_not_called()

    def test_prepare_refuses_over_cap_rpc_suggested_fee(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, acct = self._provisioned(tmp)
            w3 = self._mock_w3()
            w3.eth.get_block.return_value = {"baseFeePerGas": 400 * 10**9}
            relay = MagicMock()
            with self.assertRaises(TestnetSafetyError) as ctx:
                prepare_flashbots_bundle(
                    [{"to": acct.address, "value": 0}],
                    purpose="must never prepare",
                    config=cfg,
                    web3_factory=lambda url: w3,
                    relay_factory=lambda url, auth, relay_url: relay,
                )
            self.assertIn("exceeds the testnet cap", str(ctx.exception))
            relay.assert_not_called()

    def test_zero_value_self_transfer_is_within_fee_caps(self):
        """The planned initial operation (0-value self-transfer, 21,000 gas
        at the cap price) must fit inside the monetary fee cap."""
        self.assertLess(21000 * MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
                        MAX_TESTNET_TX_FEE_WEI)

    def test_fee_ceiling_is_one_transaction_maximum(self):
        """DSS-WO-ENG-005 §6: the 0.002 native ceiling does NOT absorb a
        second full worst-case retry — two worst-case transactions would
        total 0.0021 native and exceed it. Documented here as an executed
        calculation, not a claim."""
        worst_case = 21000 * MAX_TESTNET_MAX_FEE_PER_GAS_WEI
        self.assertEqual(worst_case, 105 * 10**13)   # 0.00105 native
        self.assertGreater(2 * worst_case, MAX_TESTNET_TX_FEE_WEI)


class TestFlashbotsSepoliaSimulation(unittest.TestCase):
    """Phase 1 — relay connectivity and bundle simulation (no submission).

    Flashbots preparation SIGNS, so this phase requires the same explicit
    testnet execution gate as submission (DSS-WO-ENG-005 §2); without the
    gate the production preparation path would refuse before decrypting
    the wallet."""

    def test_flashbots_sepolia_bundle_simulation(self):
        reason = _skip_reason()
        if reason:
            self.skipTest(reason)
        if os.environ.get("DSS_TESTNET_EXEC") != "1":
            self.skipTest(
                "Flashbots preparation signs, so the testnet execution gate "
                "DSS_TESTNET_EXEC=1 is required even for simulation-only "
                "runs (DSS-WO-ENG-005 §2)"
            )

        from web3 import Web3, HTTPProvider

        cfg = TestnetExecutionConfig.from_env()
        recorded = _read_recorded_address(cfg)

        # Read-only funding check (public RPC, no wallet).
        probe = Web3(HTTPProvider(os.environ["DSS_TESTNET_RPC_URL"]))
        if not probe.is_connected():
            self.skipTest("testnet RPC is not reachable")
        balance = probe.eth.get_balance(recorded)
        if balance == 0:
            self.skipTest(
                "DSS agent wallet is unfunded on Sepolia — awaiting "
                "Principal funding; no simulation possible"
            )

        # Unified gate path: execution gate, identity binding, chain
        # verification, policy, signing — then the read-only relay
        # simulation of the EXACT prepared payload (capability-bound).
        session, signed, target_block = prepare_flashbots_bundle(
            [{"to": recorded, "value": SELF_TRANSFER_VALUE_WEI}],
            purpose="Flashbots Sepolia relay simulation check (0-value self-transfer)",
            config=cfg,
        )
        simulation = session.simulate()
        self.assertTrue(simulation.get("results"), "simulation returned no results")


class TestFlashbotsSepoliaSubmission(unittest.TestCase):
    """Phase 2 — submission. Requires the dedicated testnet execution gate.

    This phase is separate from simulation and additionally gated behind
    DSS_TESTNET_EXEC=1 (checked inside the gate's submit(), never trusted
    from the test). The submission itself is REFUSED by production code
    unless a successful simulation of the exact payload and target block
    was recorded first (FlashbotsBundleSession). Before any state-changing
    submission the Coordinator must have reviewed the transaction plan;
    the gate flag encodes that authorization.
    """

    def test_flashbots_sepolia_self_transfer_submission(self):
        reason = _skip_reason()
        if reason:
            self.skipTest(reason)
        if os.environ.get("DSS_TESTNET_EXEC") != "1":
            self.skipTest(
                "submission requires the testnet execution gate "
                "DSS_TESTNET_EXEC=1 (simulation-only run)"
            )

        cfg = TestnetExecutionConfig.from_env()
        recorded = _read_recorded_address(cfg)

        from web3 import Web3, HTTPProvider
        probe = Web3(HTTPProvider(os.environ["DSS_TESTNET_RPC_URL"]))
        balance = probe.eth.get_balance(recorded)
        if balance == 0:
            self.skipTest("DSS agent wallet is unfunded on Sepolia")

        session, signed, target_block = prepare_flashbots_bundle(
            [{"to": recorded, "value": SELF_TRANSFER_VALUE_WEI}],
            purpose="Flashbots Sepolia 0-value self-transfer via relay",
            config=cfg,
        )
        # Simulation first — submit() fails closed without it (production
        # enforcement, not a test convention). The session takes no
        # payload: it submits exactly the prepared, capability-bound
        # payload for the prepared target block.
        session.simulate()
        result = session.submit()
        self.assertIsNotNone(result["relay_result"], "relay returned no result")


if __name__ == "__main__":
    print("This module is an opt-in pytest integration test.")
    print("Set DSS_TESTNET_FLASHBOTS_LIVE=1 and DSS_TESTNET_RPC_URL, and")
    print("provide the DSS agent keystore in the DSS secret store, to")
    print("enable it. Submission additionally requires DSS_TESTNET_EXEC=1.")
    print()
    print("Never point it at mainnet. The wallet cannot be redirected by")
    print("any environment variable.")
