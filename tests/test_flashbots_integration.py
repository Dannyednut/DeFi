"""
tests/test_flashbots_integration.py
====================================
OPT-IN Flashbots Sepolia integration test — DSS agent wallet only, through
the R4 single execution path (DSS-WO-ENG-015 R4).

History and safety contract:

1. The ORIGINAL version of this file executed at IMPORT time — it loaded
   PRIVATE_KEY from .env, built a signer, dialed a live RPC, signed a
   transaction, simulated a bundle and BROADCAST it via
   eth_sendPrivateTransaction. Collecting the file was enough to move
   money. That script was removed in DSS-WO-ENG-003 (Workstream B) and
   rewritten in DSS-WO-ENG-004/005; DSS-WO-ENG-015 R4 rewrites it once
   more against the retained architecture (Builder → Signer → Relay).
   Importing or collecting this module performs NO key loading, NO
   network activity and NO signing. Only stdlib + already-installed
   package imports run at module level.

2. Everything is disabled by default. Live tests skip visibly with their
   reason unless every required flag and credential is present (see
   _skip_reason()). CI provides none of them, so live tests never run in
   CI.

3. SINGLE wallet path: the wallet is loaded EXCLUSIVELY by
   executor.signer.load_wallet from the DSS secret store (encrypted v3
   keystore + hex-encoded passphrase file + recorded address binding).
   This file has no key-handling code of its own. There is no code path
   to any other wallet, and the engine never generates or funds one.

4. Authorization is the MASTER EXECUTION SWITCH (DSS-WO-ENG-015 R4):
   the retired DSS_TESTNET_EXEC testnet gate no longer exists. Signing
   requires EXECUTE_ONCHAIN=true in the REAL process environment; the
   signer verifies the RPC-reported chain against the expected chain
   before and immediately before signing, and the relay re-verifies the
   chain immediately before broadcasting and proves the payload was
   signed by the recorded agent identity.

5. The live test transaction is a 0-value self-transfer from the agent
   wallet to itself: no third party can receive anything, and the only
   possible cost is gas on testnet ETH, bounded by the chain fee policy
   (execution_profile.FEE_POLICIES[11155111]).

6. Bundle simulation additionally requires the code-controlled Flashbots
   registry (config.FLASHBOTS_RPCS) to carry the Sepolia entry: without
   it the relay module fail-closes ("no Flashbots relay configured") —
   enabling that entry is a reviewed config change, not a test hack.

Gating environment variables (agent runtime only, never set in CI):
    DSS_TESTNET_FLASHBOTS_LIVE=1   opt in to live testnet interaction
    EXECUTE_ONCHAIN=true           the master execution switch
    DSS_SECRET_STORE=<dir>         the DSS secret-store directory
    DSS_TESTNET_RPC_URL=<url>      Sepolia RPC endpoint
"""
import contextlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from eth_account import Account

from config import FLASHBOTS_RPCS, execution_enabled
from executor.relay import FLASHBOTS_CHAINS
from executor.signer import (
    ExecutionRefused,
    check_chain_identity,
    load_wallet,
    recorded_agent_address,
    sign_transaction,
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
    if not execution_enabled():
        return "master execution switch is disabled (EXECUTE_ONCHAIN)"
    try:
        verify_secret_store()
    except ExecutionRefused as exc:
        return f"DSS agent secret store failed custody check: {exc}"
    if not os.environ.get("DSS_TESTNET_RPC_URL"):
        return "DSS_TESTNET_RPC_URL not configured"
    return None


# ── offline: skip-reason visibility ──────────────────────────────────────────

class TestSkipReasonVisibility(unittest.TestCase):
    def test_default_run_skips_with_visible_secret_free_reason(self):
        import re
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSS_TESTNET_FLASHBOTS_LIVE", None)
            reason = _skip_reason()
        self.assertIsNotNone(reason)
        self.assertIn("DSS_TESTNET_FLASHBOTS_LIVE", reason)
        # The reason must not leak any secret-looking material.
        self.assertIsNone(re.search(r"[0-9a-fA-F]{64,}", reason))

    def test_ci_never_provisions_live_interaction(self):
        # The CI environment (no flags) can never reach a live phase.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSS_TESTNET_FLASHBOTS_LIVE", None)
            self.assertIsNotNone(_skip_reason())


# ── offline: wallet custody and identity binding (throwaway store) ──────────

class TestWalletCustodyOffline(unittest.TestCase):
    """Offline pins for the single wallet path — throwaway keys only."""

    def _throwaway_store(self, key: Account) -> str:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="flashbots-it-"))
        store = tmp / "secrets"
        store.mkdir(mode=0o700)
        keystore = Account.encrypt(key.key, b"throwaway-passphrase")
        p = store / "dss_wallet.keystore.json"
        p.write_text(json.dumps(keystore))
        os.chmod(p, 0o600)
        p = store / "dss_wallet.pass"
        p.write_text(b"throwaway-passphrase".hex())
        os.chmod(p, 0o600)
        p = store / "dss_wallet.address"
        p.write_text(key.address)
        os.chmod(p, 0o600)
        return str(store)

    def test_load_wallet_binds_the_recorded_identity(self):
        key = Account.create()
        with patch.dict(os.environ, {
            "DSS_SECRET_STORE": self._throwaway_store(key),
        }):
            account = load_wallet()
            self.assertEqual(account.address.lower(), key.address.lower())
            self.assertEqual(
                recorded_agent_address().lower(), key.address.lower(),
            )

    def test_identity_mismatch_refuses_to_return_the_account(self):
        key = Account.create()
        impostor = Account.create()
        store = self._throwaway_store(key)
        # Record the WRONG public identity.
        addr_file = pathlib.Path(store) / "dss_wallet.address"
        addr_file.write_text(impostor.address)
        os.chmod(addr_file, 0o600)
        with patch.dict(os.environ, {"DSS_SECRET_STORE": store}):
            with self.assertRaises(ExecutionRefused) as ctx:
                load_wallet()
            self.assertIn("identity mismatch", str(ctx.exception))

    def test_group_readable_keystore_fails_custody(self):
        key = Account.create()
        store = self._throwaway_store(key)
        ks = pathlib.Path(store) / "dss_wallet.keystore.json"
        os.chmod(ks, 0o640)
        with patch.dict(os.environ, {"DSS_SECRET_STORE": store}):
            with self.assertRaises(ExecutionRefused):
                verify_secret_store()

    def test_symlinked_keystore_fails_custody(self):
        key = Account.create()
        store = pathlib.Path(self._throwaway_store(key))
        real = store / "dss_wallet.keystore.json"
        link = store / "link.keystore.json"
        real.rename(link)
        real.write_text(link.read_text())
        os.chmod(real, 0o600)
        os.remove(real)
        os.symlink(link, real)
        with patch.dict(os.environ, {"DSS_SECRET_STORE": str(store)}):
            with self.assertRaises(ExecutionRefused):
                verify_secret_store()

    def test_unset_secret_store_fails_closed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSS_SECRET_STORE", None)
            with self.assertRaises(ExecutionRefused) as ctx:
                verify_secret_store()
            self.assertIn("DSS_SECRET_STORE", str(ctx.exception))


# ── live (opt-in, never in CI) ───────────────────────────────────────────────

class TestLiveFlashbotsSepolia(unittest.TestCase):
    """Live Sepolia interaction through the R4 path. SKIPS unless every
    gating flag is present; CI can never reach these bodies."""

    def setUp(self):
        reason = _skip_reason()
        if reason:
            self.skipTest(reason)
        from web3 import Web3, HTTPProvider

        self.w3 = Web3(HTTPProvider(os.environ["DSS_TESTNET_RPC_URL"]))
        # Chain identity FIRST — the same production check the signer runs.
        check_chain_identity(self.w3, SEPOLIA_CHAIN_ID)
        self.agent = load_wallet()

    def _self_transfer_tx(self) -> dict:
        """0-value self-transfer — nothing can leave the agent wallet."""
        addr = self.w3.to_checksum_address(self.agent.address)
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", self.w3.eth.gas_price)
        return {
            "to": addr,
            "value": SELF_TRANSFER_VALUE_WEI,
            "gas": 21_000,
            "maxFeePerGas": int(base_fee) * 2 + 10**9,
            "maxPriorityFeePerGas": 10**9,
            "nonce": self.w3.eth.get_transaction_count(addr, "pending"),
            "chainId": SEPOLIA_CHAIN_ID,
        }

    def test_flashbots_sepolia_bundle_simulation(self):
        if SEPOLIA_CHAIN_ID not in FLASHBOTS_CHAINS:
            self.skipTest(
                "config.FLASHBOTS_RPCS has no Sepolia entry — enabling it is "
                "a reviewed code change (the relay module fail-closes without it)"
            )
        from executor.relay import simulate_bundle

        raw = sign_transaction(self.w3, self._self_transfer_tx(),
                               chain_id=SEPOLIA_CHAIN_ID)
        head = int(self.w3.eth.block_number)
        sim = simulate_bundle(
            self.w3,
            [{"signed_transaction": raw}],
            head + 1,
        )
        self.assertIn("results", sim)

    def test_flashbots_sepolia_direct_submission(self):
        raw = sign_transaction(self.w3, self._self_transfer_tx(),
                               chain_id=SEPOLIA_CHAIN_ID)
        result = relay_transaction(
            self.w3, raw, expected_chain_id=SEPOLIA_CHAIN_ID,
        )
        # Either the submission succeeded or it failed closed — never a
        # half-state. This assertion documents the expected value object.
        self.assertIsInstance(result.success, bool)
        self.assertIn(result.method, ("direct", "refused"))


if __name__ == "__main__":
    unittest.main()
