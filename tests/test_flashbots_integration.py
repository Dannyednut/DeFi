"""
tests/test_flashbots_integration.py
====================================
OPT-IN Flashbots Sepolia integration test — DSS agent wallet only.

DSS-WO-ENG-003 (Workstream B): the original version of this file executed
at IMPORT time — it loaded PRIVATE_KEY from .env, built a signer, dialed a
live RPC, signed a transaction, simulated a bundle and BROADCAST it via
eth_sendPrivateTransaction. Collecting the file was enough to move money.
That behaviour is removed permanently.

Safety contract of this rewrite:

1. Importing or collecting this module performs NO key loading, NO network
   activity and NO signing. Only stdlib imports run at module level.
2. Everything is disabled by default. Both tests skip visibly with their
   reason unless every required testnet-only flag and credential is
   present (see _skip_reason()).
3. The wallet used is the DSS agent testnet wallet, loaded exclusively
   from the DSS secure store (encrypted keystore + passphrase file under
   the agent secret directory). The legacy .env PRIVATE_KEY and every
   Principal-owned credential are never read by this module — there is no
   code path that touches os.environ["PRIVATE_KEY"], config.PRIVATE_KEY,
   or any .env file.
4. The RPC-reported chain ID is verified BEFORE signing and AGAIN before
   submission. Ethereum mainnet (chain 1) is always rejected; any chain
   not explicitly allowlisted fails closed.
5. Simulation (eth_callBundle) and submission (eth_sendBundle) are
   separate operations. Submission additionally requires the testnet
   execution gate DSS_TESTNET_EXEC=1.
6. The test transaction is a 0-value self-transfer from the agent wallet
   to itself: no third party can receive anything, and the only possible
   cost is gas on testnet ETH.
7. CI never signs, broadcasts, deploys or submits: CI provides none of
   the required flags or credentials, so both tests skip.

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
    DSS_TESTNET_EXEC=1                  submission gate (simulation-only without it)
    DSS_TESTNET_KEYSTORE=<path>         override keystore location (default:
                                        ~/.secrets/dss_testnet_wallet.keystore.json)
    DSS_TESTNET_KEYSTORE_PASSPHRASE     override passphrase source (default:
                                        ~/.secrets/dss_testnet_wallet.pass file)
"""
import json
import os
import unittest

# Testnet allowlist for DSS agent operations. Mainnet (1) is deliberately
# absent and is hard-rejected by _verify_chain_id regardless of this map.
AGENT_TESTNET_CHAINS = {
    11155111: "Ethereum Sepolia",
}

FLASHBOTS_SEPOLIA_RELAY = "https://relay-sepolia.flashbots.net"
SEPOLIA_CHAIN_ID = 11155111

# The test transaction moves zero value. Gas on Sepolia ETH is the only
# possible expenditure, and only from the DSS agent wallet.
SELF_TRANSFER_VALUE_WEI = 0


def _keystore_path() -> str:
    return os.environ.get(
        "DSS_TESTNET_KEYSTORE",
        os.path.expanduser("~/.secrets/dss_testnet_wallet.keystore.json"),
    )


def _keystore_passphrase() -> bytes:
    """Passphrase for the agent keystore. File-based by default so the
    passphrase is never part of any process listing or shell history."""
    env_pass = os.environ.get("DSS_TESTNET_KEYSTORE_PASSPHRASE")
    if env_pass:
        return env_pass.encode()
    pass_path = os.path.expanduser("~/.secrets/dss_testnet_wallet.pass")
    if os.path.isfile(pass_path):
        with open(pass_path, "rb") as f:
            return f.read().strip()
    raise FileNotFoundError(
        "agent keystore passphrase not found (~/.secrets/dss_testnet_wallet.pass "
        "or DSS_TESTNET_KEYSTORE_PASSPHRASE)"
    )


def _skip_reason() -> str | None:
    """Return a human-readable skip reason, or None if fully provisioned."""
    if os.environ.get("DSS_TESTNET_FLASHBOTS_LIVE") != "1":
        return (
            "opt-in live testnet test: set DSS_TESTNET_FLASHBOTS_LIVE=1 "
            "(never set in CI)"
        )
    if not os.path.isfile(_keystore_path()):
        return "DSS agent testnet wallet keystore not found"
    if not os.environ.get("DSS_TESTNET_RPC_URL"):
        return "DSS_TESTNET_RPC_URL not configured"
    return None


def _load_agent_wallet():
    """Decrypt the DSS agent wallet from the secure store.

    Returns an eth_account LocalAccount held in memory only. The private
    key is never printed, logged, serialized or written anywhere by this
    module. There is deliberately NO fallback to .env or config keys.
    """
    from eth_account import Account

    with open(_keystore_path(), "r") as f:
        keystore = json.load(f)
    return Account.decrypt(keystore, _keystore_passphrase())


def _verify_chain_id(w3, expected: int = SEPOLIA_CHAIN_ID) -> int:
    """Verify the RPC-reported chain ID against the DSS testnet allowlist.

    Called immediately before signing and again before submission.
    Mainnet and unknown chains fail closed.
    """
    actual = int(w3.eth.chain_id)
    if actual == 1:
        raise RuntimeError(
            "DSS testnet execution path refuses Ethereum mainnet (chain ID 1)"
        )
    if actual not in AGENT_TESTNET_CHAINS:
        raise RuntimeError(
            f"chain ID {actual} is not on the DSS testnet allowlist "
            f"({sorted(AGENT_TESTNET_CHAINS)}) — failing closed"
        )
    if actual != expected:
        raise RuntimeError(
            f"RPC chain mismatch: expected {AGENT_TESTNET_CHAINS[expected]} "
            f"({expected}), RPC reports {actual}"
        )
    return actual


def _connect_and_preflight():
    """Shared live setup: wallet, RPC, Flashbots middleware, chain check."""
    from web3 import Web3, HTTPProvider
    from flashbots import flashbot

    account = _load_agent_wallet()
    w3 = Web3(HTTPProvider(os.environ["DSS_TESTNET_RPC_URL"]))
    if not w3.is_connected():
        raise RuntimeError("testnet RPC is not reachable")

    # Chain identity is verified BEFORE any signing material is used.
    _verify_chain_id(w3)

    # Auth signer for relay request signatures is the agent wallet itself.
    flashbot(w3, account, FLASHBOTS_SEPOLIA_RELAY)
    return w3, account


def _build_self_transfer(w3, account) -> dict:
    """0-value EIP-1559 self-transfer from the agent wallet to itself."""
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
    priority_fee = w3.to_wei(1, "gwei")
    max_fee = int(base_fee * 1.5) + priority_fee
    return {
        "from": account.address,
        "to": account.address,
        "value": SELF_TRANSFER_VALUE_WEI,
        "gas": 21000,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": SEPOLIA_CHAIN_ID,
    }


class TestChainIdGate(unittest.TestCase):
    """Offline tests of the chain-ID gate itself (no network, no keys).

    Pins the fail-closed contract: mainnet is always rejected, unknown
    chains are rejected, and only explicit allowlisted testnets pass.
    """

    def _w3_reporting(self, chain_id: int):
        w3 = unittest.mock.MagicMock()
        w3.eth.chain_id = chain_id
        return w3

    def test_mainnet_is_always_rejected(self):
        import unittest.mock
        with self.assertRaises(RuntimeError) as ctx:
            _verify_chain_id(self._w3_reporting(1))
        self.assertIn("refuses Ethereum mainnet", str(ctx.exception))

    def test_unknown_chain_fails_closed(self):
        import unittest.mock
        for chain_id in (5, 56, 137, 8453, 999999):
            with self.subTest(chain_id=chain_id):
                with self.assertRaises(RuntimeError) as ctx:
                    _verify_chain_id(self._w3_reporting(chain_id))
                self.assertIn("not on the DSS testnet allowlist", str(ctx.exception))

    def test_allowlisted_testnet_passes(self):
        import unittest.mock
        self.assertEqual(_verify_chain_id(self._w3_reporting(11155111)), 11155111)

    def test_expected_mismatch_fails_closed(self):
        import unittest.mock
        # RPC reports an allowlisted chain that differs from the expected
        # one (two-chain allowlist scenario; with the single-chain Sepolia
        # allowlist this branch is pure defence-in-depth).
        fake_allowlist = {11155111: "Ethereum Sepolia", 17000: "Holesky"}
        with unittest.mock.patch.dict(
            "tests.test_flashbots_integration.AGENT_TESTNET_CHAINS",
            fake_allowlist,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _verify_chain_id(
                    self._w3_reporting(17000), expected=11155111
                )
        self.assertIn("RPC chain mismatch", str(ctx.exception))


class TestFlashbotsSepoliaSimulation(unittest.TestCase):
    """Phase 1 — relay connectivity and bundle simulation (no submission)."""

    def test_flashbots_sepolia_bundle_simulation(self):
        reason = _skip_reason()
        if reason:
            self.skipTest(reason)

        w3, account = _connect_and_preflight()

        balance = w3.eth.get_balance(account.address)
        if balance == 0:
            self.skipTest(
                "DSS agent wallet is unfunded on Sepolia — awaiting "
                "Principal funding; no simulation possible"
            )

        # Re-verify chain identity immediately before signing.
        self.assertEqual(_verify_chain_id(w3), SEPOLIA_CHAIN_ID)

        tx = _build_self_transfer(w3, account)
        signed = account.sign_transaction(tx)
        # eth-account <0.13 exposes .rawTransaction; newer versions .raw_transaction
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        bundle = [{"signed_transaction": raw}]

        target_block = w3.eth.block_number + 1
        simulation = w3.flashbots.simulate(bundle, target_block)
        error = simulation.get("error") if hasattr(simulation, "get") else None
        self.assertIsNone(
            error, f"Flashbots Sepolia bundle simulation failed: {error}"
        )


class TestFlashbotsSepoliaSubmission(unittest.TestCase):
    """Phase 2 — submission. Requires the dedicated testnet execution gate.

    This phase is separate from simulation and gated behind DSS_TESTNET_EXEC=1
    in addition to every Phase-1 requirement. Before any state-changing
    submission the Coordinator must have reviewed the transaction plan
    (DSS-WO-ENG-003 §8); the gate flag encodes that authorization.
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

        w3, account = _connect_and_preflight()

        balance = w3.eth.get_balance(account.address)
        if balance == 0:
            self.skipTest("DSS agent wallet is unfunded on Sepolia")

        # Chain ID verified at connect, before signing, and once more
        # immediately before submission (fail-closed between the two).
        self.assertEqual(_verify_chain_id(w3), SEPOLIA_CHAIN_ID)

        tx = _build_self_transfer(w3, account)
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction

        # Re-verify AFTER signing, immediately before submission.
        self.assertEqual(_verify_chain_id(w3), SEPOLIA_CHAIN_ID)

        target_block = w3.eth.block_number + 1
        result = w3.flashbots.send_bundle(
            [{"signed_transaction": raw}],
            target_block_number=target_block,
        )
        self.assertIsNotNone(result, "relay returned no submission result")


if __name__ == "__main__":
    print("This module is an opt-in pytest integration test.")
    print("Set DSS_TESTNET_FLASHBOTS_LIVE=1, DSS_TESTNET_RPC_URL and provide")
    print("the DSS agent keystore to enable it. Submission additionally")
    print("requires DSS_TESTNET_EXEC=1.")
    print()
    print("Never point it at mainnet. Never replace the agent wallet with")
    print("a .env key.")
