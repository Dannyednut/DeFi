"""
tests/test_flashbots_integration.py
====================================
OPT-IN Flashbots Sepolia integration test — DSS agent wallet only.

DSS-WO-ENG-003 (Workstream B): the original version of this file executed
at IMPORT time — it loaded PRIVATE_KEY from .env, built a signer, dialed a
live RPC, signed a transaction, simulated a bundle and BROADCAST it via
eth_sendPrivateTransaction. Collecting the file was enough to move money.
That behaviour is removed permanently.

DSS-WO-ENG-004 (§2): wallet loading hardened. The ENG-003 rewrite still
had three defects: (a) a passphrase environment override that bypassed the
approved secret-store custody mechanism, (b) a keystore-path override that
allowed alternative wallets, and (c) the loader returned raw decrypted key
BYTES (no .address, no signing ability — the live phases could never have
worked). All three are fixed:

1. Importing or collecting this module performs NO key loading, NO network
   activity and NO signing. Only stdlib imports run at module level.
2. Everything is disabled by default. All tests skip visibly with their
   reason unless every required testnet-only flag and credential is
   present (see _skip_reason()).
3. The wallet used is the DSS agent testnet wallet ONLY, loaded from the
   DSS secure store (encrypted keystore + passphrase file under the agent
   secret directory). The passphrase is decoded EXACTLY like the central
   gate's keystore format (executor/testnet_gate.py on the testnet-gate
   branch): the passphrase file holds the ASCII hex encoding of the
   passphrase bytes. Decrypted key bytes are converted into a proper
   eth_account LocalAccount with Account.from_key, and the derived address
   is BOUND to the recorded public address file BEFORE any signing. There
   is deliberately NO environment override for the passphrase or the
   keystore path, and no code path touches .env or any Principal-owned
   credential (enforced by a source-level pin test below).
4. Before any live operation the secret files are checked for presence,
   file type (no symlinks) and restrictive permissions (0600 files, 0700
   directory); problems surface as visible skip reasons.
5. The RPC-reported chain ID is verified BEFORE signing and AGAIN before
   submission. Ethereum mainnet (chain 1) is always rejected; any chain
   not explicitly allowlisted fails closed. A controlled allowlist can be
   injected explicitly by tests to exercise the mismatch branch — the
   production allowlist constant stays Sepolia-only.
6. Simulation (eth_callBundle) and submission (eth_sendBundle) are
   separate operations. Submission additionally requires the testnet
   execution gate DSS_TESTNET_EXEC=1. Mandatory simulation-before-
   submission is enforced in PRODUCTION code by the unified gate
   (FlashbotsBundleSession, executor/testnet_gate.py on the testnet-gate
   branch, DSS-WO-ENG-004 §4); this file performs the same ordering as
   procedure, but that production enforcement is the authoritative one.
7. The test transaction is a 0-value self-transfer from the agent wallet
   to itself: no third party can receive anything, and the only possible
   cost is gas on testnet ETH.
8. CI never signs, broadcasts, deploys or submits: CI provides none of
   the required flags or credentials, so the live tests skip.

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
    DSS_TESTNET_EXEC=1                  submission gate (simulation-only
                                        without it)

There is deliberately NO environment variable for the keystore path, the
passphrase or any alternative wallet (DSS-WO-ENG-004 §2 reqs 5–6): the
recorded DSS agent identity cannot be bypassed.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Testnet allowlist for DSS agent operations. Mainnet (1) is deliberately
# absent and is hard-rejected by _verify_chain_id regardless of this map.
# Tests may inject a controlled allowlist explicitly via the allowlist
# parameter; the production map below is never env-configurable.
AGENT_TESTNET_CHAINS = {
    11155111: "Ethereum Sepolia",
}

FLASHBOTS_SEPOLIA_RELAY = "https://relay-sepolia.flashbots.net"
SEPOLIA_CHAIN_ID = 11155111

# The DSS secret store (mirrors executor/testnet_gate.py on the
# testnet-gate branch; the two must stay consistent until the branches
# reconcile — the gate copy is authoritative).
DSS_SECRET_STORE = "/home/z/my-project/.secrets"
KEYSTORE_PATH = os.path.join(DSS_SECRET_STORE, "dss_testnet_wallet.keystore.json")
PASSPHRASE_PATH = os.path.join(DSS_SECRET_STORE, "dss_testnet_wallet.pass")
ADDRESS_PATH = os.path.join(DSS_SECRET_STORE, "dss_testnet_wallet.address")

# The test transaction moves zero value. Gas on Sepolia ETH is the only
# possible expenditure, and only from the DSS agent wallet.
SELF_TRANSFER_VALUE_WEI = 0

# Secret files must carry no group/world bits; the directory must not be
# accessible to other users (same policy as the central gate).
SECRET_MODE_MASK = 0o077


# ── Wallet custody (mirrors the central gate's keystore format) ──────────────

def _decode_passphrase_file(path: str) -> bytes:
    """Decode the keystore passphrase file — EXACTLY the central gate's
    keystore format: the file holds the ASCII hex encoding of the
    passphrase bytes. No environment override, no raw-text fallback."""
    with open(path, "rb") as f:
        raw = f.read().strip()
    try:
        return bytes.fromhex(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            "agent keystore passphrase file is not valid hex-encoded "
            "passphrase data (central keystore format)"
        ) from exc


def _check_restricted_file(path: str, label: str) -> None:
    """Presence + file type + restrictive permissions, fail closed."""
    if os.path.islink(path):
        raise RuntimeError(f"agent {label} must be a regular file, not a symlink: {path}")
    if not os.path.isfile(path):
        raise RuntimeError(f"agent {label} missing or not a regular file: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & SECRET_MODE_MASK:
        raise RuntimeError(
            f"agent {label} permissions too open ({oct(mode)}): group/world "
            "access must be removed (0600 expected)"
        )


def _check_secret_dir(path: str, label: str) -> None:
    if os.path.islink(path):
        raise RuntimeError(f"agent {label} directory must not be a symlink: {path}")
    if not os.path.isdir(path):
        raise RuntimeError(f"agent {label} directory missing: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & SECRET_MODE_MASK:
        raise RuntimeError(
            f"agent {label} directory accessible to other users ({oct(mode)}): "
            "0700 expected"
        )


def _secret_store_problems(
    keystore_path: str = KEYSTORE_PATH,
    passphrase_path: str = PASSPHRASE_PATH,
    address_path: str = ADDRESS_PATH,
) -> list:
    """Return a list of custody problems with the secret store (empty when
    ready). Never reads secret contents — presence, type and permission
    checks only, mirroring executor/testnet_gate.verify_secret_store."""
    problems = []
    for label, path in (("keystore", keystore_path), ("passphrase", passphrase_path)):
        try:
            _check_restricted_file(path, label)
            _check_secret_dir(os.path.dirname(path) or ".", label)
        except RuntimeError as exc:
            problems.append(str(exc))
    if os.path.islink(address_path) or not os.path.isfile(address_path):
        problems.append(
            f"agent recorded-address file missing, a symlink or not a "
            f"regular file: {address_path}"
        )
    return problems


def load_wallet_from_files(
    keystore_path: str, passphrase_path: str, address_path: str
):
    """Load a v3 keystore into a proper eth_account LocalAccount and BIND
    it to the recorded public address file.

    Used by the live path (fixed DSS secret-store paths, no overrides) and
    by offline tests (throwaway keys in temporary directories). The
    recorded-address binding is UNCONDITIONAL: no path configuration can
    make an alternative wallet pass as the recorded identity.

    Raises RuntimeError on any custody problem (missing/loose/symlinked
    files, bad hex passphrase, non-v3 keystore, address mismatch). The
    key bytes and passphrase are dropped before returning; nothing is
    ever printed or logged.
    """
    from eth_account import Account
    from eth_account.signers.local import LocalAccount  # noqa: F401 (contract)

    problems = _secret_store_problems(keystore_path, passphrase_path, address_path)
    if problems:
        raise RuntimeError("; ".join(problems))

    with open(keystore_path, "r") as f:
        keystore = json.load(f)
    if (
        not isinstance(keystore, dict)
        or int(keystore.get("version", 0)) != 3
        or not isinstance(keystore.get("crypto"), dict)
    ):
        raise RuntimeError(
            "agent keystore is not a web3 secret-storage v3 JSON keystore"
        )

    passphrase = _decode_passphrase_file(passphrase_path)
    key_bytes = Account.decrypt(keystore, passphrase)
    try:
        account = Account.from_key(key_bytes)  # proper LocalAccount
    finally:
        del key_bytes, passphrase

    with open(address_path, "r") as f:
        recorded = f.read().strip()
    if account.address.lower() != recorded.lower():
        raise RuntimeError(
            "agent wallet identity mismatch: keystore does not match the "
            "recorded agent address — refusing to sign"
        )
    return account


def _load_agent_wallet():
    """The live path: ONLY the DSS agent wallet, ONLY from the fixed DSS
    secret-store paths. There are deliberately no environment overrides
    (DSS-WO-ENG-004 §2 reqs 5–6) and no fallback to .env or config keys.
    Returns an in-memory LocalAccount; the private key is never printed,
    logged, serialized or written anywhere."""
    return load_wallet_from_files(KEYSTORE_PATH, PASSPHRASE_PATH, ADDRESS_PATH)


def _skip_reason() -> str | None:
    """Return a human-readable skip reason, or None if fully provisioned.
    Touches only local files and environment flags — no network, no keys."""
    if os.environ.get("DSS_TESTNET_FLASHBOTS_LIVE") != "1":
        return (
            "opt-in live testnet test: set DSS_TESTNET_FLASHBOTS_LIVE=1 "
            "(never set in CI)"
        )
    problems = _secret_store_problems()
    if problems:
        return "DSS agent secret store failed custody check: " + "; ".join(problems)
    if not os.environ.get("DSS_TESTNET_RPC_URL"):
        return "DSS_TESTNET_RPC_URL not configured"
    return None


def _verify_chain_id(w3, expected: int = SEPOLIA_CHAIN_ID, allowlist: dict | None = None) -> int:
    """Verify the RPC-reported chain ID against the DSS testnet allowlist.

    Called immediately before signing and again before submission.
    Mainnet and unknown chains fail closed. `allowlist` may be injected
    explicitly by offline tests to exercise the allowlisted-but-unexpected
    branch; production uses the Sepolia-only map.
    """
    chains = AGENT_TESTNET_CHAINS if allowlist is None else allowlist
    actual = int(w3.eth.chain_id)
    if actual == 1:
        raise RuntimeError(
            "DSS testnet execution path refuses Ethereum mainnet (chain ID 1)"
        )
    if actual not in chains:
        raise RuntimeError(
            f"chain ID {actual} is not on the DSS testnet allowlist "
            f"({sorted(chains)}) — failing closed"
        )
    if actual != expected:
        raise RuntimeError(
            f"RPC chain mismatch: expected {expected}, RPC reports {actual}"
        )
    return actual


def _connect_and_preflight():
    """Shared live setup: wallet (custody-checked + identity-bound), RPC,
    chain check, Flashbots middleware. No signing happens here."""
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
        with self.assertRaises(RuntimeError) as ctx:
            _verify_chain_id(self._w3_reporting(1))
        self.assertIn("refuses Ethereum mainnet", str(ctx.exception))

    def test_mainnet_rejected_even_on_injected_allowlist(self):
        with self.assertRaises(RuntimeError) as ctx:
            _verify_chain_id(
                self._w3_reporting(1),
                allowlist={11155111: "Ethereum Sepolia", 17000: "Holesky"},
            )
        self.assertIn("refuses Ethereum mainnet", str(ctx.exception))

    def test_unknown_chain_fails_closed(self):
        for chain_id in (5, 56, 137, 8453, 999999):
            with self.subTest(chain_id=chain_id):
                with self.assertRaises(RuntimeError) as ctx:
                    _verify_chain_id(self._w3_reporting(chain_id))
                self.assertIn("not on the DSS testnet allowlist", str(ctx.exception))

    def test_allowlisted_testnet_passes(self):
        self.assertEqual(_verify_chain_id(self._w3_reporting(11155111)), 11155111)

    def test_allowlisted_but_unexpected_chain_fails_closed(self):
        """ENG-004 §6 pattern: with a controlled two-chain allowlist, an
        allowlisted RPC chain that is not the expected one hits the real
        mismatch branch. The production map is never modified."""
        two_chains = {11155111: "Ethereum Sepolia", 17000: "Holesky"}
        with self.assertRaises(RuntimeError) as ctx:
            _verify_chain_id(
                self._w3_reporting(17000), expected=11155111, allowlist=two_chains
            )
        self.assertIn("RPC chain mismatch", str(ctx.exception))
        # The production allowlist stays untouched.
        self.assertEqual(AGENT_TESTNET_CHAINS, {11155111: "Ethereum Sepolia"})


class TestWalletCustodyOffline(unittest.TestCase):
    """ENG-004 §2: offline tests exercising the ACTUAL wallet-loading logic
    with throwaway test keys and temporary files. The live DSS wallet is
    never used here."""

    def _store(self, tmp) -> str:
        """Create a throwaway wallet (offline) in the layout of the DSS
        secret store; returns the derived address."""
        from eth_account import Account

        acct = Account.create()
        keystore = Account.encrypt(acct.key, b"throwaway-passphrase")

        def w(path, data, mode=0o600):
            with open(path, "wb") as f:
                f.write(data)
            os.chmod(path, mode)

        w(os.path.join(tmp, "keystore.json"), json.dumps(keystore).encode())
        # hex-encoded passphrase — the central gate's keystore format
        w(os.path.join(tmp, "pass"), b"throwaway-passphrase".hex().encode())
        w(os.path.join(tmp, "address"), (acct.address + "\n").encode())
        return acct.address

    def test_roundtrip_returns_local_account_bound_to_recorded_address(self):
        from eth_account.signers.local import LocalAccount

        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp)
            account = load_wallet_from_files(
                os.path.join(tmp, "keystore.json"),
                os.path.join(tmp, "pass"),
                os.path.join(tmp, "address"),
            )
            self.assertIsInstance(account, LocalAccount)
            # Signing ability comes with the LocalAccount interface.
            self.assertTrue(hasattr(account, "sign_transaction"))
            with open(os.path.join(tmp, "address")) as f:
                recorded = f.read().strip()
            self.assertEqual(account.address.lower(), recorded.lower())

    def test_identity_mismatch_refused(self):
        from eth_account import Account

        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp)
            impostor = Account.create().address
            with open(os.path.join(tmp, "address"), "w") as f:
                f.write(impostor + "\n")
            os.chmod(os.path.join(tmp, "address"), 0o600)
            with self.assertRaises(RuntimeError) as ctx:
                load_wallet_from_files(
                    os.path.join(tmp, "keystore.json"),
                    os.path.join(tmp, "pass"),
                    os.path.join(tmp, "address"),
                )
            self.assertIn("identity mismatch", str(ctx.exception))

    def test_symlinked_keystore_refused(self):
        with tempfile.TemporaryDirectory() as real_dir:
            self._store(real_dir)
            with tempfile.TemporaryDirectory() as tmp:
                os.symlink(
                    os.path.join(real_dir, "keystore.json"),
                    os.path.join(tmp, "keystore.json"),
                )
                with self.assertRaises(RuntimeError) as ctx:
                    load_wallet_from_files(
                        os.path.join(tmp, "keystore.json"),
                        os.path.join(real_dir, "pass"),
                        os.path.join(real_dir, "address"),
                    )
                self.assertIn("symlink", str(ctx.exception))

    def test_loose_permissions_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp)
            os.chmod(os.path.join(tmp, "keystore.json"), 0o644)
            with self.assertRaises(RuntimeError) as ctx:
                load_wallet_from_files(
                    os.path.join(tmp, "keystore.json"),
                    os.path.join(tmp, "pass"),
                    os.path.join(tmp, "address"),
                )
            self.assertIn("permissions too open", str(ctx.exception))

    def test_world_accessible_directory_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp)
            os.chmod(tmp, 0o755)
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    load_wallet_from_files(
                        os.path.join(tmp, "keystore.json"),
                        os.path.join(tmp, "pass"),
                        os.path.join(tmp, "address"),
                    )
                self.assertIn("accessible to other users", str(ctx.exception))
            finally:
                os.chmod(tmp, 0o700)

    def test_missing_passphrase_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp)
            os.remove(os.path.join(tmp, "pass"))
            with self.assertRaises(RuntimeError):
                load_wallet_from_files(
                    os.path.join(tmp, "keystore.json"),
                    os.path.join(tmp, "pass"),
                    os.path.join(tmp, "address"),
                )

    def test_passphrase_must_be_hex_encoded(self):
        """Pins the central gate's keystore format: a raw (non-hex)
        passphrase file must be refused, not silently accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp)
            with open(os.path.join(tmp, "pass"), "wb") as f:
                f.write(b"raw-passphrase-not-hex")
            with self.assertRaises(RuntimeError) as ctx:
                load_wallet_from_files(
                    os.path.join(tmp, "keystore.json"),
                    os.path.join(tmp, "pass"),
                    os.path.join(tmp, "address"),
                )
            self.assertIn("hex-encoded", str(ctx.exception))

    def test_skip_reason_surfaces_custody_problems(self):
        problems = _secret_store_problems(
            keystore_path="/nonexistent/keystore.json",
            passphrase_path="/nonexistent/pass",
            address_path="/nonexistent/address",
        )
        self.assertTrue(problems)
        self.assertTrue(any("missing or not a regular file" in p for p in problems))

    def test_source_pins_no_override_and_no_legacy_key_path(self):
        """ENG-004 §2 reqs 5–6: the overrides and the legacy key path must
        not exist in this file's source. Literals are built by
        concatenation so this source does not contain them verbatim."""
        with open(__file__, "r") as f:
            src = f.read()
        forbidden = (
            "DSS_TESTNET_KEYSTORE" + "_PASSPHRASE",
            'os.environ.get("DSS_TESTNET' + '_KEYSTORE")',
            'os.get' + 'env("PRIVATE' + '_KEY")',
            'os.environ.get("PRIVATE' + '_KEY")',
            "load_dot" + "env",
            "send_" + "private_transaction",
        )
        for item in forbidden:
            self.assertNotIn(item, src)


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
    submission the Coordinator must have reviewed the transaction plan; the
    gate flag encodes that authorization. NOTE (DSS-WO-ENG-004 §4): the
    AUTHORITATIVE simulation-before-submission enforcement is production
    code on the unified gate (FlashbotsBundleSession in
    executor/testnet_gate.py, testnet-gate branch); this file mirrors the
    ordering as procedure until the branches reconcile.
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
    print("Set DSS_TESTNET_FLASHBOTS_LIVE=1 and DSS_TESTNET_RPC_URL, and")
    print("provide the DSS agent keystore in the DSS secret store, to")
    print("enable it. Submission additionally requires DSS_TESTNET_EXEC=1.")
    print()
    print("Never point it at mainnet. The wallet cannot be redirected by")
    print("any environment variable.")
