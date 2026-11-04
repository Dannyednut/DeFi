"""
tests/test_testnet_gate.py
==========================
Offline, deterministic tests for the DSS testnet-only execution boundary
(executor/testnet_gate.py, DSS-WO-ENG-003 D; unified per DSS-WO-ENG-004;
capability-gated per DSS-WO-ENG-005).

Every safety invariant of the gate is pinned here:

  * gate disabled by default and fail-closed on missing configuration;
  * mainnet (1) ALWAYS rejected, unknown chains rejected, allowlisted
    testnets pass, allowlisted-but-unexpected RPC chain rejected BEFORE
    signing (ENG-004 §6: exercised through an explicitly injected test
    allowlist — the production allowlist stays Sepolia-only and can never
    come from the environment), and a chain identity change between
    signing and submission prevents sending;
  * EVERY operation that decrypts the wallet and signs — direct RPC and
    Flashbots preparation alike — first passes the SAME explicit testnet
    execution gate (ENG-005 §2): with the gate disabled, Flashbots
    preparation never decrypts the wallet and never signs;
  * Flashbots submission authority is an unforgeable capability
    (ENG-005 §3): only prepare_flashbots_bundle can mint a
    PreparedFlashbotsBundle; directly constructed, forged or subclassed
    session/capability objects are refused; simulate()/submit() take no
    payload, so an arbitrary bundle can never gain submission authority;
    every binding (wallet identity via signer recovery, chain ID,
    validated intent, exact signed payload fingerprint, target block) is
    re-verified at construction and before every relay interaction;
  * transaction policy: no deployments, no arbitrary EOA transfers, no
    unverified contract targets, hard value/gas caps, monetary fee
    exposure caps for EIP-1559 and legacy transactions (missing,
    negative, malformed and over-cap fee inputs rejected), no unlimited
    token approvals, chainId binding, and an AGGREGATE bundle-level fee
    cap enforced before wallet decryption (ENG-005 §5);
  * wallet custody: the single wallet-loading path verifies secret-file
    presence/types/permissions, hex-decodes the passphrase, builds a
    proper eth_account LocalAccount and binds it to the recorded address;
    any mismatch, symlink or loose permission fails closed (throwaway
    keys only — never the live DSS wallet);
  * Flashbots bundle policy: every transaction of a bundle passes the
    same policy; one bad transaction rejects the bundle;
  * simulation-before-submission is enforced by PRODUCTION code
    (FlashbotsBundleSession): failed, missing, stale or
    payload-mismatched simulations prevent submission before any relay
    submission call, the chain identity is re-verified immediately before
    submission, and the one-use authorization is consumed BEFORE the
    relay call so a timeout cannot authorize unlimited retries;
  * every simulation and submission — success, failure, refusal or
    uncertain outcome — is audited without leaking secrets (ENG-005 §4);
  * EXECUTE_ONCHAIN (the production guard) stays false and untouched;
  * the audit log records the required fields and can never contain
    credentials;
  * the sign-and-submit orchestration verifies chain identity before
    signing AND before submission — exercised end-to-end with a mocked
    Web3 and a throwaway keystore; nothing touches a network.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account
from eth_account.signers.local import LocalAccount

from executor.testnet_gate import (
    ALLOWED_TESTNET_CHAINS,
    DEFAULT_KEYSTORE_PATH,
    MAINNET_CHAIN_ID,
    MAX_TOKEN_APPROVAL_AMOUNT,
    MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
    MAX_TESTNET_MAX_PRIORITY_FEE_WEI,
    MAX_TESTNET_TX_FEE_WEI,
    TX_LOG_FIELDS,
    FlashbotsBundleSession,
    PreparedFlashbotsBundle,
    TestnetExecutionConfig,
    TestnetSafetyError,
    _payload_fingerprint,
    check_chain_id,
    evaluate_bundle_simulation,
    load_agent_wallet,
    prepare_flashbots_bundle,
    record_testnet_tx,
    sign_and_submit_testnet_tx,
    validate_bundle,
    validate_bundle_fee_exposure,
    validate_fee_exposure,
    validate_transaction,
    verify_secret_store,
)

SEPOLIA = 11155111
HOLESKY = 17000
TWO_CHAIN_ALLOWLIST = {SEPOLIA: "Ethereum Sepolia", HOLESKY: "Holesky"}


def _restricted_write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _tmp_keystore(tmpdir: str, address: str = None):
    """Create a throwaway encrypted keystore (offline, throwaway key)."""
    acct = Account.create()
    # The THROWAWAY account signs in tests; the recorded address must match
    # it, so we return the real one instead of any parameter.
    keystore = Account.encrypt(acct.key, b"test-passphrase")
    _restricted_write(os.path.join(tmpdir, "keystore.json"), json.dumps(keystore).encode())
    _restricted_write(os.path.join(tmpdir, "pass"), b"test-passphrase".hex().encode())
    _restricted_write(os.path.join(tmpdir, "address"), (acct.address + "\n").encode())
    return {"account": acct, "dir": tmpdir}


class TestGateDefaults(unittest.TestCase):
    def test_gate_disabled_by_default(self):
        cfg = TestnetExecutionConfig.from_env(env={})
        self.assertFalse(cfg.enabled)
        with self.assertRaises(TestnetSafetyError) as ctx:
            cfg.require_ready()
        self.assertIn("DISABLED", str(ctx.exception))

    def test_enabled_without_credentials_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = TestnetExecutionConfig.from_env(env={
                "DSS_TESTNET_EXEC": "1",
                "DSS_TESTNET_RPC_URL": "https://sepolia.example",
            })
            cfg = TestnetExecutionConfig(
                enabled=True,
                rpc_url=cfg.rpc_url,
                keystore_path=os.path.join(tmp, "missing.json"),
                passphrase_path=os.path.join(tmp, "missing.pass"),
                address_path=os.path.join(tmp, "missing.addr"),
            )
            with self.assertRaises(TestnetSafetyError) as ctx:
                cfg.require_ready()
            self.assertIn("missing or not a regular file", str(ctx.exception))

    def test_enabled_with_unknown_chain_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_keystore(tmp)
            cfg = TestnetExecutionConfig(
                enabled=True,
                rpc_url="https://x.example",
                keystore_path=os.path.join(tmp, "keystore.json"),
                passphrase_path=os.path.join(tmp, "pass"),
                address_path=os.path.join(tmp, "address"),
                expected_chain_id=8453,  # Base — mainnet L2, not allowlisted
            )
            with self.assertRaises(TestnetSafetyError):
                cfg.require_ready()

    def test_from_env_cannot_inject_allowlist_or_relay(self):
        """The environment can configure only presence-type inputs; the
        allowlist (and relay) are reviewed code constants, never settings."""
        cfg = TestnetExecutionConfig.from_env(env={
            "DSS_TESTNET_EXEC": "1",
            "DSS_TESTNET_RPC_URL": "https://sepolia.example",
            "DSS_TESTNET_ALLOWLIST": "1,2,3",
        })
        self.assertIsNone(cfg.allowlist)
        self.assertIs(cfg.effective_allowlist(), ALLOWED_TESTNET_CHAINS)
        self.assertEqual(cfg.relay_url, "https://relay-sepolia.flashbots.net")

    def test_production_guard_untouched(self):
        """EXECUTE_ONCHAIN must remain the separate, unchanged production
        guard — the testnet boundary must not weaken or read it."""
        import config
        self.assertFalse(config.EXECUTE_ONCHAIN)
        # The gate module does not import or consult EXECUTE_ONCHAIN.
        import executor.testnet_gate as gate
        import inspect
        src = inspect.getsource(gate)
        self.assertNotIn("EXECUTE_ONCHAIN", src.replace(
            "EXECUTE_ONCHAIN (the production execution guard) is untouched and",
            "").replace(
            "stays false. This boundary is a SEPARATE gate that only ever permits",
            ""))


class TestChainIdGate(unittest.TestCase):
    def test_mainnet_always_rejected(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            check_chain_id(1)
        self.assertIn("refuses Ethereum mainnet", str(ctx.exception))

    def test_mainnet_rejected_even_on_injected_allowlist(self):
        """Mainnet can never be smuggled in through a test allowlist."""
        with self.assertRaises(TestnetSafetyError) as ctx:
            check_chain_id(1, allowlist=TWO_CHAIN_ALLOWLIST)
        self.assertIn("refuses Ethereum mainnet", str(ctx.exception))

    def test_unknown_chains_rejected(self):
        for chain_id in (5, 10, 56, 137, 17000, 8453, 42161, 999999):
            with self.subTest(chain_id=chain_id):
                with self.assertRaises(TestnetSafetyError):
                    check_chain_id(chain_id)

    def test_allowlisted_sepolia_passes(self):
        self.assertEqual(check_chain_id(11155111), 11155111)

    def test_mismatch_rejected(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            check_chain_id(11155111, expected=999999)
        self.assertIn("mismatch", str(ctx.exception))

    def test_injected_allowlist_unexpected_chain_rejected(self):
        """ENG-004 §6: with a controlled two-chain allowlist, an allowlisted
        RPC chain that is not the expected one hits the real mismatch
        branch."""
        with self.assertRaises(TestnetSafetyError) as ctx:
            check_chain_id(HOLESKY, expected=SEPOLIA, allowlist=TWO_CHAIN_ALLOWLIST)
        self.assertIn("RPC chain mismatch", str(ctx.exception))

    def test_injected_allowlist_expected_chain_accepted(self):
        self.assertEqual(
            check_chain_id(HOLESKY, expected=HOLESKY, allowlist=TWO_CHAIN_ALLOWLIST),
            HOLESKY,
        )

    def test_allowlist_contains_sepolia_only(self):
        """Initially Sepolia only; any addition requires a documented
        network-specific review (policy constant pinned by test)."""
        self.assertEqual(ALLOWED_TESTNET_CHAINS, {11155111: "Ethereum Sepolia"})
        self.assertNotIn(MAINNET_CHAIN_ID, ALLOWED_TESTNET_CHAINS)


class TestTransactionPolicy(unittest.TestCase):
    SENDER = "0x" + "aa" * 20
    STRANGER = "0x" + "bb" * 20

    def base_tx(self, **over):
        tx = {
            "to": self.SENDER,       # self-transfer by default
            "value": 0,
            "gas": 21000,
            "maxFeePerGas": 2 * 10**9,          # 2 gwei — within caps
            "maxPriorityFeePerGas": 10**9,      # 1 gwei — within caps
            "data": b"",
            "chainId": 11155111,
        }
        tx.update(over)
        return tx

    def test_self_transfer_allowed(self):
        out = validate_transaction(self.base_tx(), sender=self.SENDER, chain_id=11155111)
        self.assertEqual(out["to"], self.SENDER)

    def test_arbitrary_eoa_transfer_rejected(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                self.base_tx(to=self.STRANGER, value=1),
                sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("no arbitrary EOA transfers", str(ctx.exception))

    def test_unverified_contract_target_rejected(self):
        contract = "0x" + "cc" * 20
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                self.base_tx(to=contract),
                sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("unverified contract targets", str(ctx.exception))

    def test_known_target_allowed(self):
        contract = "0x" + "cc" * 20
        targets = {11155111: {contract: "reviewed test deployment"}}
        out = validate_transaction(
            self.base_tx(to=contract),
            sender=self.SENDER, chain_id=11155111, known_targets=targets,
        )
        self.assertEqual(out["to"], contract)

    def test_deployment_rejected(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                self.base_tx(to=None, data=b"\x60\x80"),
                sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("deployment rejected", str(ctx.exception))

    def test_value_cap_enforced(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                self.base_tx(value=MAX_TOKEN_APPROVAL_AMOUNT),
                sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("cap", str(ctx.exception))

    def test_gas_cap_enforced(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                self.base_tx(gas=1_000_000),
                sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("gas limit", str(ctx.exception))

    def test_unlimited_approval_rejected(self):
        unlimited = (2**256 - 1).to_bytes(32, "big")
        data = bytes.fromhex("095ea7b3") + b"\x00" * 32 + unlimited  # padded address word + amount
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                self.base_tx(to="0x" + "cc" * 20, data=data),
                sender=self.SENDER, chain_id=11155111,
                known_targets={11155111: {"0x" + "cc" * 20: "token"}},
            )
        self.assertIn("approval rejected", str(ctx.exception))

    def test_bounded_approval_allowed(self):
        bounded = (10**15).to_bytes(32, "big")
        data = bytes.fromhex("095ea7b3") + b"\x00" * 32 + bounded
        out = validate_transaction(
            self.base_tx(to="0x" + "cc" * 20, data=data),
            sender=self.SENDER, chain_id=11155111,
            known_targets={11155111: {"0x" + "cc" * 20: "token"}},
        )
        self.assertEqual(out["to"], "0x" + "cc" * 20)

    def test_chain_id_binding_enforced(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                self.base_tx(chainId=1), sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("chainId", str(ctx.exception))


class TestFeeExposurePolicy(unittest.TestCase):
    """ENG-004 §5: a gas-unit cap is not a monetary cap."""

    def test_1559_within_caps_passes_and_returns_expenditure(self):
        tx = {"gas": 21000, "maxFeePerGas": 50 * 10**9,
              "maxPriorityFeePerGas": 2 * 10**9}
        expenditure = validate_fee_exposure(tx)
        self.assertEqual(expenditure, 21000 * 50 * 10**9)
        self.assertLessEqual(expenditure, MAX_TESTNET_TX_FEE_WEI)

    def test_1559_total_expenditure_over_cap_rejected(self):
        # 500,000 gas (under the gas-unit cap) at 50 gwei = 2.5e15 wei —
        # above the 2e15 wei monetary cap. Fee exposure must fail.
        tx = {"gas": 500_000, "maxFeePerGas": 50 * 10**9,
              "maxPriorityFeePerGas": 2 * 10**9}
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_fee_exposure(tx)
        self.assertIn("not a monetary cap", str(ctx.exception))

    def test_1559_price_over_cap_rejected(self):
        tx = {"gas": 21000, "maxFeePerGas": 50 * 10**9 + 1}
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_fee_exposure(tx)
        self.assertIn("exceeds the testnet cap", str(ctx.exception))

    def test_1559_priority_over_cap_rejected(self):
        tx = {"gas": 21000, "maxFeePerGas": 50 * 10**9,
              "maxPriorityFeePerGas": 2 * 10**9 + 1}
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_fee_exposure(tx)
        self.assertIn("maxPriorityFeePerGas", str(ctx.exception))

    def test_1559_priority_above_max_fee_rejected(self):
        tx = {"gas": 21000, "maxFeePerGas": 10**9,
              "maxPriorityFeePerGas": 2 * 10**9}
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_fee_exposure(tx)
        self.assertIn("malformed fee inputs", str(ctx.exception))

    def test_legacy_within_cap_passes(self):
        tx = {"gas": 21000, "gasPrice": 30 * 10**9}
        self.assertEqual(validate_fee_exposure(tx), 21000 * 30 * 10**9)

    def test_legacy_over_cap_rejected(self):
        tx = {"gas": 21000, "gasPrice": 51 * 10**9}
        with self.assertRaises(TestnetSafetyError):
            validate_fee_exposure(tx)

    def test_missing_fee_input_rejected(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_fee_exposure({"gas": 21000})
        self.assertIn("missing fee input", str(ctx.exception))

    def test_mixed_1559_and_legacy_rejected(self):
        tx = {"gas": 21000, "maxFeePerGas": 10**9, "gasPrice": 10**9}
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_fee_exposure(tx)
        self.assertIn("malformed fee inputs", str(ctx.exception))

    def test_negative_fee_rejected(self):
        for tx in ({"gas": 21000, "maxFeePerGas": -1},
                   {"gas": 21000, "gasPrice": -5}):
            with self.subTest(tx=tx):
                with self.assertRaises(TestnetSafetyError):
                    validate_fee_exposure(tx)

    def test_malformed_fee_types_rejected(self):
        for value in (True, 1.5, "10", None):
            with self.subTest(value=value):
                tx = {"gas": 21000, "maxFeePerGas": value}
                if value is None:
                    tx = {"gas": 21000, "gasPrice": value}
                with self.assertRaises(TestnetSafetyError):
                    validate_fee_exposure(tx)

    def test_policy_rejects_over_cap_fee_in_validate_transaction(self):
        sender = "0x" + "aa" * 20
        tx = {"to": sender, "value": 0, "gas": 21000,
              "maxFeePerGas": 51 * 10**9, "data": b"", "chainId": 11155111}
        with self.assertRaises(TestnetSafetyError):
            validate_transaction(tx, sender=sender, chain_id=11155111)


class TestBundlePolicy(unittest.TestCase):
    SENDER = "0x" + "aa" * 20

    def tx(self, **over):
        tx = {"to": self.SENDER, "value": 0, "gas": 21000,
              "maxFeePerGas": 2 * 10**9, "maxPriorityFeePerGas": 10**9,
              "data": b"", "chainId": 11155111}
        tx.update(over)
        return tx

    def test_valid_bundle_passes(self):
        out = validate_bundle([self.tx(), self.tx()], sender=self.SENDER, chain_id=11155111)
        self.assertEqual(len(out), 2)

    def test_one_bad_transaction_rejects_whole_bundle(self):
        stranger = "0x" + "bb" * 20
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_bundle(
                [self.tx(), self.tx(to=stranger, value=1)],
                sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("bundle tx[1] rejected", str(ctx.exception))
        self.assertIn("no arbitrary EOA transfers", str(ctx.exception))

    def test_fee_exposure_applies_per_bundle_transaction(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_bundle(
                [self.tx(), self.tx(gas=500_000, maxFeePerGas=50 * 10**9)],
                sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("bundle tx[1] rejected", str(ctx.exception))


# ── Wallet custody (throwaway keys only — never the live DSS wallet) ─────────

class TestWalletCustody(unittest.TestCase):
    def _store(self, tmp):
        info = _tmp_keystore(tmp)
        return (
            TestnetExecutionConfig(
                enabled=True,
                rpc_url="https://sepolia.example",
                keystore_path=os.path.join(tmp, "keystore.json"),
                passphrase_path=os.path.join(tmp, "pass"),
                address_path=os.path.join(tmp, "address"),
            ),
            info["account"],
        )

    def test_roundtrip_returns_local_account_bound_to_recorded_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, acct = self._store(tmp)
            account = load_agent_wallet(cfg)
            self.assertIsInstance(account, LocalAccount)
            self.assertEqual(account.address.lower(), acct.address.lower())

    def test_identity_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._store(tmp)
            impostor = Account.create().address
            _restricted_write(os.path.join(tmp, "address"), (impostor + "\n").encode())
            with self.assertRaises(TestnetSafetyError) as ctx:
                load_agent_wallet(cfg)
            self.assertIn("identity mismatch", str(ctx.exception))

    def test_symlinked_keystore_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_dir = tempfile.mkdtemp()
            _tmp_keystore(real_dir)
            os.symlink(
                os.path.join(real_dir, "keystore.json"),
                os.path.join(tmp, "keystore.json"),
            )
            _restricted_write(os.path.join(tmp, "pass"), b"78746573742d70617373706872617365")
            cfg = TestnetExecutionConfig(
                enabled=True, rpc_url="https://sepolia.example",
                keystore_path=os.path.join(tmp, "keystore.json"),
                passphrase_path=os.path.join(tmp, "pass"),
                address_path=os.path.join(real_dir, "address"),
            )
            with self.assertRaises(TestnetSafetyError) as ctx:
                verify_secret_store(cfg)
            self.assertIn("symlink", str(ctx.exception))

    def test_loose_keystore_permissions_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._store(tmp)
            os.chmod(cfg.keystore_path, 0o644)
            with self.assertRaises(TestnetSafetyError) as ctx:
                verify_secret_store(cfg)
            self.assertIn("permissions too open", str(ctx.exception))

    def test_world_accessible_directory_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._store(tmp)
            os.chmod(tmp, 0o755)
            try:
                with self.assertRaises(TestnetSafetyError) as ctx:
                    verify_secret_store(cfg)
                self.assertIn("accessible to other users", str(ctx.exception))
            finally:
                os.chmod(tmp, 0o700)

    def test_missing_passphrase_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._store(tmp)
            os.remove(cfg.passphrase_path)
            with self.assertRaises(TestnetSafetyError):
                verify_secret_store(cfg)

    def test_non_hex_passphrase_refused(self):
        from executor.testnet_gate import decode_passphrase_file
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pass")
            _restricted_write(path, b"raw-secret-passphrase")  # not hex
            with self.assertRaises(TestnetSafetyError) as ctx:
                decode_passphrase_file(path)
            self.assertIn("hex-encoded", str(ctx.exception))

    def test_non_v3_keystore_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._store(tmp)
            _restricted_write(cfg.keystore_path, json.dumps({"version": 1}).encode())
            with self.assertRaises(TestnetSafetyError) as ctx:
                load_agent_wallet(cfg)
            self.assertIn("secret-storage v3", str(ctx.exception))


# ── Simulation-before-submission enforcement (production code) ───────────────

def _ok_simulation(n_txs=1, gas_used=21000):
    return {
        "bundleHash": "0x" + "cd" * 32,
        "coinbaseDiff": "0x1",
        "totalGasUsed": gas_used * n_txs,
        "results": [
            {"txHash": "0x" + "ee" * 32, "gasUsed": gas_used, "value": "0x0"}
            for _ in range(n_txs)
        ],
    }


class _EthStub:
    def __init__(self, chain_values, head):
        self._values = list(chain_values)
        self._i = 0
        self._head = head

    @property
    def chain_id(self):
        value = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return value

    @property
    def block_number(self):
        return self._head


_NO_SIM = object()  # sentinel: "no simulation result configured"


class _RelayStub:
    """Mock Flashbots transport: records calls, never executes anything."""

    def __init__(self, chain_values=(SEPOLIA,), head=99, sim_result=_NO_SIM):
        self.eth = _EthStub(list(chain_values), head)
        self.flashbots = self
        self._sim_result = _ok_simulation() if sim_result is _NO_SIM else sim_result
        self.simulate_calls = []
        self.send_calls = []

    def simulate(self, bundle, block_tag, *args, **kwargs):
        self.simulate_calls.append((bundle, block_tag))
        if isinstance(self._sim_result, Exception):
            raise self._sim_result
        return self._sim_result

    def send_bundle(self, bundle, target_block_number=None, **kwargs):
        self.send_calls.append((bundle, target_block_number))
        return {"bundleHash": "0x" + "77" * 32}

    def advance(self, blocks=1):
        self.eth._head += blocks

    def assert_not_called(self):
        if self.simulate_calls or self.send_calls:
            raise AssertionError(
                "relay must not be contacted, but was: "
                f"simulate={self.simulate_calls} send={self.send_calls}"
            )


def _bundle_cfg(tmp, *, enabled=True, allowlist=None):
    return TestnetExecutionConfig(
        enabled=enabled,
        rpc_url="https://sepolia.example",
        keystore_path=os.path.join(tmp, "keystore.json"),
        passphrase_path=os.path.join(tmp, "pass"),
        address_path=os.path.join(tmp, "address"),
        tx_log_path=os.path.join(tmp, "txlog.jsonl"),
        allowlist=allowlist,
    )


class _PrepareW3:
    """Web3 stub for prepare_flashbots_bundle: never touches a network.
    chain_id advances through the given values on each read (sticking at
    the last value), emulating an RPC whose identity may change."""

    def __init__(self, chain_values=(SEPOLIA,), head=99):
        self._values = list(chain_values)
        self._i = 0
        self._head = head

    def is_connected(self):
        return True

    @property
    def eth(self):
        return self

    @property
    def chain_id(self):
        value = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return value

    @property
    def block_number(self):
        return self._head

    @property
    def gas_price(self):
        return 10**9

    def get_transaction_count(self, addr):
        return 7

    def get_block(self, tag):
        return {"baseFeePerGas": 10**9}

    def to_wei(self, v, u):
        return int(v * 10**9) if u == "gwei" else v


def _audit_outcomes(cfg):
    with open(cfg.tx_log_path) as f:
        return [json.loads(line)["outcome"] for line in f if line.strip()]


def _prepared_bundle(tmp, relay, *, enabled=True, allowlist=None, n_txs=1,
                     fee_price_gwei=2, chain_values=(SEPOLIA,), head=99,
                     purpose="capability session test"):
    """Prepare a REAL bundle through the sanctioned path with a throwaway
    wallet. Returns (session, cfg, signed, target, acct)."""
    info = _tmp_keystore(tmp)
    acct = info["account"]
    cfg = TestnetExecutionConfig(
        enabled=enabled,
        rpc_url="https://sepolia.example",
        keystore_path=os.path.join(tmp, "keystore.json"),
        passphrase_path=os.path.join(tmp, "pass"),
        address_path=os.path.join(tmp, "address"),
        tx_log_path=os.path.join(tmp, "txlog.jsonl"),
        allowlist=allowlist,
    )
    w3 = _PrepareW3(chain_values=chain_values, head=head)
    txs = [
        {
            "to": acct.address, "value": 0, "gas": 21000,
            "maxFeePerGas": fee_price_gwei * 10**9,
            "maxPriorityFeePerGas": 10**9, "data": b"",
        }
        for _ in range(n_txs)
    ]
    session, signed, target = prepare_flashbots_bundle(
        txs, purpose=purpose, config=cfg,
        web3_factory=lambda url: w3,
        relay_factory=lambda url, auth, relay_url: relay,
    )
    return session, cfg, signed, target, acct


class TestFlashbotsCapabilitySession(unittest.TestCase):
    """ENG-005 §2–§4: the sanctioned preparation path, the unforgeable
    capability and the fully-audited simulate/submit lifecycle — exercised
    end-to-end with a throwaway keystore, a stubbed RPC and a mock relay."""

    def _prepare(self, tmp, relay, **kw):
        return _prepared_bundle(tmp, relay, **kw)

    # ── §2: the execution gate governs Flashbots preparation ────────────

    def test_preparation_without_gate_never_decrypts_or_signs(self):
        """With DSS_TESTNET_EXEC disabled, Flashbots preparation must fail
        at the gate BEFORE the wallet is decrypted and before the relay is
        touched (ENG-005 §2 reqs 1–2)."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            with patch(
                "executor.testnet_gate.load_agent_wallet",
                side_effect=AssertionError("wallet must never be decrypted"),
            ):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    self._prepare(tmp, relay, enabled=False)
            self.assertIn("DISABLED", str(ctx.exception))
            relay.assert_not_called()

    def test_preparation_with_gate_proceeds_after_policy_checks(self):
        """With the gate enabled, preparation signs only after the RPC
        chain check, the full transaction policy and the custody checks
        have passed — and the relay is never contacted during prepare."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            session, cfg, signed, target, acct = self._prepare(tmp, relay)
            relay.assert_not_called()
            self.assertEqual(target, 100)
            self.assertEqual(len(signed), 1)
            from eth_account import Account as _A
            self.assertEqual(
                _A.recover_transaction(signed[0]).lower(), acct.address.lower()
            )

    def test_both_transports_use_same_gate_and_wallet_identity(self):
        """Direct RPC and Flashbots preparation pass the SAME gate and sign
        with the SAME recorded wallet identity (ENG-005 §2 req 8)."""
        with tempfile.TemporaryDirectory() as tmp:
            info = _tmp_keystore(tmp)
            acct = info["account"]
            base = dict(
                rpc_url="https://sepolia.example",
                keystore_path=os.path.join(tmp, "keystore.json"),
                passphrase_path=os.path.join(tmp, "pass"),
                address_path=os.path.join(tmp, "address"),
                tx_log_path=os.path.join(tmp, "txlog.jsonl"),
            )
            disabled = TestnetExecutionConfig(enabled=False, **base)
            # Both transports refuse at the gate, before any signing.
            with self.assertRaises(TestnetSafetyError) as direct_err:
                sign_and_submit_testnet_tx(
                    {"to": acct.address, "value": 0, "gas": 21000, "data": b""},
                    purpose="gate parity", config=disabled,
                    web3_factory=lambda url: MagicMock(),
                )
            self.assertIn("DISABLED", str(direct_err.exception))
            relay = _RelayStub()
            with patch(
                "executor.testnet_gate.load_agent_wallet",
                side_effect=AssertionError("wallet must never be decrypted"),
            ):
                with self.assertRaises(TestnetSafetyError) as fb_err:
                    prepare_flashbots_bundle(
                        [{"to": acct.address, "value": 0}],
                        purpose="gate parity", config=disabled,
                        web3_factory=lambda url: _PrepareW3(),
                        relay_factory=lambda url, auth, relay_url: relay,
                    )
            self.assertIn("DISABLED", str(fb_err.exception))
            relay.assert_not_called()

            # With the gate enabled, both transports sign with the same
            # wallet — the one bound to the recorded address.
            enabled = TestnetExecutionConfig(enabled=True, **base)
            sent = []

            class _DirectW3:
                def is_connected(self):
                    return True

                class eth:  # noqa: N801 - stub namespace
                    pass

            w3 = MagicMock()
            w3.is_connected.return_value = True
            w3.eth.chain_id = SEPOLIA
            w3.eth.get_transaction_count.return_value = 7
            w3.eth.get_block.return_value = {"baseFeePerGas": 10**9}
            w3.to_wei.side_effect = lambda v, u: int(v * 10**9) if u == "gwei" else v

            def _send(raw):
                sent.append(bytes(raw))
                return b"\xab" * 32

            w3.eth.send_raw_transaction.side_effect = _send
            w3.eth.wait_for_transaction_receipt.return_value = {
                "status": 1, "gasUsed": 21000,
            }
            sign_and_submit_testnet_tx(
                {"to": acct.address, "value": 0, "gas": 21000, "data": b""},
                purpose="gate parity", config=enabled,
                web3_factory=lambda url: w3,
            )
            from eth_account import Account as _A
            _, fb_signed, fb_target = prepare_flashbots_bundle(
                [{"to": acct.address, "value": 0}],
                purpose="gate parity", config=enabled,
                web3_factory=lambda url: _PrepareW3(),
                relay_factory=lambda url, auth, relay_url: _RelayStub(),
            )
            self.assertEqual(
                _A.recover_transaction(sent[0]).lower(), acct.address.lower()
            )
            self.assertEqual(
                _A.recover_transaction(fb_signed[0]).lower(), acct.address.lower()
            )

    # ── §3: the session cannot be forged outside the preparation path ───

    def test_direct_session_construction_refused(self):
        """Only the sanctioned preparation path may create a session: every
        externally supplied object is refused (ENG-005 §3)."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            # legacy keyword construction no longer exists
            with self.assertRaises(TypeError):
                FlashbotsBundleSession(
                    relay, sender="0x" + "aa" * 20,
                    config=_bundle_cfg(tmp), chain_id=SEPOLIA,
                )
            # no capability at all
            with self.assertRaises((TypeError, TestnetSafetyError)):
                FlashbotsBundleSession(relay)
            # None / duck-typed forgeries
            for forged in (None, object(), _ForgedPrepared()):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    FlashbotsBundleSession(relay, forged)
                self.assertIn("forged", str(ctx.exception))
            # a subclass instance is not the exact capability type
            class _Evil(PreparedFlashbotsBundle):
                def __init__(self):
                    pass
            with self.assertRaises(TestnetSafetyError):
                FlashbotsBundleSession(relay, _Evil())

    def test_capability_minting_requires_authority(self):
        """PreparedFlashbotsBundle refuses external construction: no
        authority token → hard error (ENG-005 §3)."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _bundle_cfg(tmp)
            with self.assertRaises(TypeError):
                # _authority is a required keyword — missing → refused
                PreparedFlashbotsBundle(
                    sender="0x" + "aa" * 20, chain_id=SEPOLIA,
                    validated_txs=[], signed_payload=[], target_block=100,
                    fingerprint="0" * 64, total_fee_exposure_wei=1,
                    purpose="x", config=cfg,
                )
            with self.assertRaises(TestnetSafetyError) as ctx:
                PreparedFlashbotsBundle(
                    _authority=object(),  # wrong token
                    sender="0x" + "aa" * 20, chain_id=SEPOLIA,
                    validated_txs=[], signed_payload=[], target_block=100,
                    fingerprint="0" * 64, total_fee_exposure_wei=1,
                    purpose="x", config=cfg,
                )
            self.assertIn("EXCLUSIVELY", str(ctx.exception))

    def test_bundle_signed_by_different_account_refused(self):
        """A capability whose payload was signed by a DIFFERENT account is
        refused by runtime signer recovery — the wallet-identity binding
        is enforced behavior, not a naming convention (ENG-005 §3).

        White-box note: _mint with the internal authority token is used
        here ONLY to place a deliberately mismatched capability in front
        of the session constructor; production cannot mint capabilities
        outside prepare_flashbots_bundle."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            impostor = Account.create()  # signs, but is NOT the recorded wallet
            recorded = Account.create()  # the "recorded" identity
            tx = {
                "to": recorded.address, "value": 0, "gas": 21000,
                "gasPrice": 10**9, "nonce": 0, "chainId": SEPOLIA,
            }
            raw = bytes(
                getattr(
                    impostor.sign_transaction(tx),
                    "raw_transaction",
                    None,
                )
                or impostor.sign_transaction(tx).rawTransaction
            )
            cfg = _bundle_cfg(tmp)
            prepared = PreparedFlashbotsBundle._mint(
                sender=recorded.address, chain_id=SEPOLIA,
                validated_txs=[tx], signed_payload=[raw], target_block=100,
                fingerprint=_payload_fingerprint([raw], 100),
                total_fee_exposure_wei=21000 * 10**9,
                purpose="impostor test", config=cfg,
            )
            with self.assertRaises(TestnetSafetyError) as ctx:
                FlashbotsBundleSession(relay, prepared)
            self.assertIn("not the verified agent wallet", str(ctx.exception))
            self.assertFalse(relay.send_calls)

    def test_session_takes_no_payload_and_relays_only_the_prepared_one(self):
        """simulate()/submit() accept NO payload: an arbitrary signed bundle
        cannot gain submission authority by being passed to them, and the
        relay receives exactly the prepared payload for the prepared block
        (ENG-005 §3)."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg, signed, target, acct = self._prepare(tmp, relay)
            stranger_signed = Account.create().sign_transaction({
                "to": acct.address, "value": 0, "gas": 21000,
                "gasPrice": 10**9, "nonce": 0, "chainId": SEPOLIA,
            })
            stranger_raw = bytes(
                getattr(stranger_signed, "raw_transaction", None)
                or stranger_signed.rawTransaction
            )
            for call in (
                lambda: session.simulate(stranger_raw, target),
                lambda: session.simulate(signed, target),
                lambda: session.submit(stranger_raw, target),
                lambda: session.submit(signed, target, purpose="x"),
            ):
                with self.assertRaises(TypeError):
                    call()
            session.simulate()
            session.submit()
            self.assertEqual(len(relay.send_calls), 1)
            sent_bundle, sent_block = relay.send_calls[0]
            self.assertEqual(sent_block, target)
            self.assertEqual(
                [dict(t)["signed_transaction"] for t in sent_bundle], signed
            )

    def test_capability_fingerprint_binds_exact_signed_bytes(self):
        """Any alteration to the signed payload (e.g. a changed fee
        envelope produces different bytes) yields a DIFFERENT fingerprint,
        so it can never match the recorded authorization (ENG-005 §3)."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg, signed, target, acct = self._prepare(tmp, relay)
            prepared = session._prepared
            self.assertEqual(
                prepared.fingerprint,
                _payload_fingerprint(prepared.signed_payload, target),
            )
            tampered = (bytes(signed[0]) + b"\x01",)
            self.assertNotEqual(
                _payload_fingerprint(tampered, target), prepared.fingerprint
            )

    # ── §3/§4: simulate → submit lifecycle, audit, one-use ──────────────

    def test_submit_without_simulation_refused_before_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit()
            self.assertIn("no successful simulation", str(ctx.exception))
            self.assertFalse(relay.send_calls)
            self.assertIn("submission_refused", _audit_outcomes(cfg))

    def test_failed_simulation_prevents_submission_and_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(sim_result={"error": {"message": "execution reverted"}})
            session, cfg, signed, _, _ = self._prepare(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate()
            self.assertIn("simulation failed", str(ctx.exception))
            with self.assertRaises(TestnetSafetyError):
                session.submit()
            self.assertFalse(relay.send_calls)
            self.assertIn("simulation_failed", _audit_outcomes(cfg))
            # no secret material in the log
            with open(cfg.tx_log_path) as f:
                log = f.read()
            self.assertNotIn(signed[0].hex(), log)

    def test_reverting_transaction_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = _ok_simulation()
            sim["results"][0]["error"] = "execution reverted"
            relay = _RelayStub(sim_result=sim)
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate()
            self.assertIn("tx[0] would fail", str(ctx.exception))
            self.assertFalse(relay.send_calls)
            self.assertIn("simulation_failed", _audit_outcomes(cfg))

    def test_first_revert_response_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = _ok_simulation()
            sim["firstRevert"] = "0x" + "ee" * 32
            relay = _RelayStub(sim_result=sim)
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            with self.assertRaises(TestnetSafetyError):
                session.simulate()
            self.assertFalse(relay.send_calls)
            self.assertIn("simulation_failed", _audit_outcomes(cfg))

    def test_empty_simulation_results_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(sim_result={"bundleHash": "0x1", "results": []})
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate()
            self.assertIn("no per-transaction results", str(ctx.exception))
            self.assertIn("simulation_failed", _audit_outcomes(cfg))

    def test_missing_simulation_response_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(sim_result=None)
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            with self.assertRaises(TestnetSafetyError):
                session.simulate()
            self.assertFalse(relay.send_calls)
            self.assertIn("simulation_failed", _audit_outcomes(cfg))

    def test_result_count_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(sim_result=_ok_simulation(n_txs=1))
            session, cfg, _, _, _ = self._prepare(tmp, relay, n_txs=2)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate()
            self.assertIn("malformed response", str(ctx.exception))
            self.assertIn("simulation_failed", _audit_outcomes(cfg))

    def test_stale_simulation_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            session.simulate()           # authorized at head 99, target 100
            relay.advance(1)             # head becomes 100 → target reached
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit()
            self.assertIn("stale submission refused", str(ctx.exception))
            self.assertFalse(relay.send_calls)
            self.assertIn("submission_refused", _audit_outcomes(cfg))
            # a stale target cannot even be re-simulated
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate()
            self.assertIn("stale simulation refused", str(ctx.exception))

    def test_chain_identity_change_between_sim_and_submit_refused(self):
        """Allowlisted chains both; expected Sepolia. The relay transport
        reports Sepolia through preparation, session construction and
        simulation, then Holesky at submission — the binding refuses."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(chain_values=(SEPOLIA, SEPOLIA, HOLESKY))
            session, cfg, _, _, _ = self._prepare(
                tmp, relay, allowlist=TWO_CHAIN_ALLOWLIST
            )
            session.simulate()
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit()
            self.assertIn("RPC chain mismatch", str(ctx.exception))
            self.assertFalse(relay.send_calls)
            self.assertIn("submission_refused", _audit_outcomes(cfg))

    def test_submission_requires_execution_gate(self):
        """White-box: the capability is minted through the internal
        authority ONLY to place the session under a disabled gate — a
        state production can never reach because preparation requires the
        gate. submit() must still re-check the flag itself."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            info = _tmp_keystore(tmp)
            acct = info["account"]
            cfg = _bundle_cfg(tmp, enabled=False)
            signed = acct.sign_transaction({
                "to": acct.address, "value": 0, "gas": 21000,
                "gasPrice": 10**9, "nonce": 0, "chainId": SEPOLIA,
            })
            raw = bytes(getattr(signed, "raw_transaction", None) or signed.rawTransaction)
            prepared = PreparedFlashbotsBundle._mint(
                sender=acct.address, chain_id=SEPOLIA,
                validated_txs=[{"to": acct.address, "value": 0}],
                signed_payload=[raw], target_block=100,
                fingerprint=_payload_fingerprint([raw], 100),
                total_fee_exposure_wei=21000 * 10**9,
                purpose="gate recheck", config=cfg,
            )
            session = FlashbotsBundleSession(relay, prepared)
            session.simulate()  # read-only relay op needs no gate
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit()
            self.assertIn("DSS_TESTNET_EXEC=1", str(ctx.exception))
            self.assertFalse(relay.send_calls)
            self.assertIn("submission_refused", _audit_outcomes(cfg))

    def test_happy_path_audits_and_consumes_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg, signed, target, _ = self._prepare(tmp, relay)
            sim = session.simulate()
            self.assertEqual(len(relay.simulate_calls), 1)
            self.assertEqual(relay.simulate_calls[0][1], target)
            out = session.submit()
            self.assertEqual(len(relay.send_calls), 1)
            sent_bundle, sent_block = relay.send_calls[0]
            self.assertEqual(sent_block, target)
            self.assertEqual(
                [dict(t)["signed_transaction"] for t in sent_bundle], signed
            )
            self.assertEqual(out["audit"]["outcome"], "accepted")
            self.assertEqual(out["audit"]["max_fee_exposure_wei"], 21000 * 2 * 10**9)
            outcomes = _audit_outcomes(cfg)
            self.assertIn("simulated", outcomes)
            self.assertIn("accepted", outcomes)
            # One simulation authorizes exactly one submission.
            with self.assertRaises(TestnetSafetyError):
                session.submit()
            self.assertEqual(len(relay.send_calls), 1)
            self.assertIn("submission_refused", _audit_outcomes(cfg))

    def test_relay_timeout_consumes_authorization_and_forces_new_simulation(self):
        """A relay failure after the authorization is consumed must not
        leave a reusable authorization behind: retries require a NEW
        successful simulation (ENG-005 §4)."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            session.simulate()

            class _Timeout(Exception):
                pass

            def _boom(bundle, target_block_number=None, **kw):
                raise _Timeout("relay unreachable")

            relay.send_bundle = _boom
            with self.assertRaises(_Timeout):
                session.submit()
            # outcome uncertain: the bundle may still have landed
            self.assertIn("uncertain", _audit_outcomes(cfg))
            # the authorization was consumed — a bare retry is refused
            del relay.send_bundle  # restore the accepting stub
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit()
            self.assertIn("no successful simulation", str(ctx.exception))
            self.assertEqual(len(relay.send_calls), 0)
            # only a NEW successful simulation re-authorizes submission
            session.simulate()
            session.submit()
            self.assertEqual(len(relay.send_calls), 1)

    def test_relay_rejection_audited(self):
        """An explicit relay error inside a 200-style response is recorded
        as a REJECTION and fails closed (ENG-005 §4)."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            session.simulate()
            relay.send_bundle = lambda bundle, target_block_number=None, **kw: {
                "error": {"message": "bundle target block in the past"}
            }
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit()
            self.assertIn("relay rejected the bundle submission", str(ctx.exception))
            outcomes = _audit_outcomes(cfg)
            self.assertIn("rejected", outcomes)
            with open(cfg.tx_log_path) as f:
                rec = [json.loads(l) for l in f if l.strip()]
            rejected = [r for r in rec if r["outcome"] == "rejected"][-1]
            self.assertIn("bundle target block in the past", rejected["error"])

    def test_relay_result_without_hash_is_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg, _, _, _ = self._prepare(tmp, relay)
            session.simulate()
            relay.send_bundle = lambda bundle, target_block_number=None, **kw: {
                "unknown_shape": True
            }
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit()
            self.assertIn("uncertain", str(ctx.exception))
            self.assertIn("uncertain", _audit_outcomes(cfg))

    def test_full_lifecycle_leaks_no_secret_material(self):
        """Across the whole lifecycle the audit log must never contain the
        signed payload bytes, the private key, or the passphrase (ENG-005
        §4) — including failure paths."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg, signed, _, _ = self._prepare(tmp, relay)
            session.simulate()

            def _boom(bundle, target_block_number=None, **kw):
                # hostile error embedding the signed payload
                raise RuntimeError(f"send failed for 0x{bytes(signed[0]).hex()}")

            relay.send_bundle = _boom
            with self.assertRaises(RuntimeError):
                session.submit()
            with open(cfg.tx_log_path) as f:
                log = f.read()
            self.assertNotIn(signed[0].hex(), log)
            self.assertNotIn("test-passphrase", log)
            for record in (json.loads(l) for l in log.splitlines() if l.strip()):
                self.assertEqual(sorted(record.keys()), sorted(TX_LOG_FIELDS))
                self.assertIn(record["outcome"], (
                    "simulated", "uncertain",
                ))


class _ForgedPrepared:
    """Duck-typed forgery with plausible-looking fields."""

    def __getattr__(self, name):
        if name == "config":
            raise AttributeError("config")
        raise AttributeError(name)


class TestAggregateBundleFeeCap(unittest.TestCase):
    """ENG-005 §5: aggregate bundle-level fee exposure."""

    SENDER = "0x" + "aa" * 20

    def tx(self, fee_gwei=40):
        return {
            "to": self.SENDER, "value": 0, "gas": 21000,
            "maxFeePerGas": fee_gwei * 10**9,
            "maxPriorityFeePerGas": 10**9, "data": b"",
            "chainId": 11155111,
        }

    def test_within_aggregate_cap_passes(self):
        total = validate_bundle_fee_exposure([self.tx(), self.tx()])
        self.assertEqual(total, 2 * 21000 * 40 * 10**9)
        self.assertLessEqual(total, MAX_TESTNET_TX_FEE_WEI)

    def test_over_aggregate_cap_rejected(self):
        # each transaction is individually within the per-tx cap, but the
        # bundle total exceeds the approved budget
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_bundle_fee_exposure(
                [self.tx(fee_gwei=50), self.tx(fee_gwei=50)]
            )
        self.assertIn("aggregate bundle fee exposure", str(ctx.exception))

    def test_validate_bundle_enforces_aggregate_cap(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_bundle(
                [self.tx(fee_gwei=50), self.tx(fee_gwei=50)],
                sender=self.SENDER, chain_id=11155111,
            )
        self.assertIn("aggregate bundle fee exposure", str(ctx.exception))

    def test_per_transaction_cap_still_enforced(self):
        huge = self.tx(fee_gwei=50)
        huge["gas"] = 500_000
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_bundle_fee_exposure([self.tx(fee_gwei=50), huge])
        self.assertIn("bundle tx[1] rejected", str(ctx.exception))

    def test_prepare_refuses_over_limit_bundle_before_wallet_decryption(self):
        """The aggregate check fires BEFORE the wallet is decrypted or
        anything is signed (ENG-005 §5)."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            info = _tmp_keystore(tmp)
            acct = info["account"]
            cfg = TestnetExecutionConfig(
                enabled=True,
                rpc_url="https://sepolia.example",
                keystore_path=os.path.join(tmp, "keystore.json"),
                passphrase_path=os.path.join(tmp, "pass"),
                address_path=os.path.join(tmp, "address"),
                tx_log_path=os.path.join(tmp, "txlog.jsonl"),
            )
            over = [
                {
                    "to": acct.address, "value": 0, "gas": 21000,
                    "maxFeePerGas": 50 * 10**9,
                    "maxPriorityFeePerGas": 10**9, "data": b"",
                }
                for _ in range(2)
            ]
            with patch(
                "executor.testnet_gate.load_agent_wallet",
                side_effect=AssertionError("wallet must never be decrypted"),
            ):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    prepare_flashbots_bundle(
                        over, purpose="over-limit bundle",
                        config=cfg,
                        web3_factory=lambda url: _PrepareW3(),
                        relay_factory=lambda url, auth, relay_url: relay,
                    )
            self.assertIn("aggregate bundle fee exposure", str(ctx.exception))
            relay.assert_not_called()

    def test_prepare_accepts_bundle_within_aggregate_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            session, cfg, signed, target, acct = _prepared_bundle(
                tmp, relay, n_txs=2, fee_price_gwei=40
            )
            self.assertEqual(len(signed), 2)
            self.assertEqual(
                session._prepared.total_fee_exposure_wei,
                2 * 21000 * 40 * 10**9,
            )
            self.assertLessEqual(
                session._prepared.total_fee_exposure_wei, MAX_TESTNET_TX_FEE_WEI
            )

    def test_prepare_refuses_unvalidated_recipient_before_signing(self):
        """A bundle with an unvalidated recipient is refused before the
        wallet is ever decrypted (ENG-005 §3 — the validated intent in the
        capability can only contain policy-compliant transactions)."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            info = _tmp_keystore(tmp)
            acct = info["account"]
            cfg = TestnetExecutionConfig(
                enabled=True,
                rpc_url="https://sepolia.example",
                keystore_path=os.path.join(tmp, "keystore.json"),
                passphrase_path=os.path.join(tmp, "pass"),
                address_path=os.path.join(tmp, "address"),
                tx_log_path=os.path.join(tmp, "txlog.jsonl"),
            )
            stranger = "0x" + "bb" * 20
            with patch(
                "executor.testnet_gate.load_agent_wallet",
                side_effect=AssertionError("wallet must never be decrypted"),
            ):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    prepare_flashbots_bundle(
                        [{"to": stranger, "value": 1}],
                        purpose="unvalidated recipient",
                        config=cfg,
                        web3_factory=lambda url: _PrepareW3(),
                        relay_factory=lambda url, auth, relay_url: relay,
                    )
            self.assertIn("no arbitrary EOA transfers", str(ctx.exception))
            relay.assert_not_called()


class TestAuditLog(unittest.TestCase):
    def test_record_shape_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "txlog.jsonl")
            rec = record_testnet_tx(
                path,
                purpose="relay connectivity check",
                chain_id=11155111,
                target="0x" + "aa" * 20,
                function="transfer",
                estimated_gas=21000,
                actual_gas_used=21000,
                tx_hash="0x" + "ab" * 32,
                outcome="mined",
                max_fee_exposure_wei=21000 * 2 * 10**9,
                private_key="SHOULD-NOT-APPEAR",  # unexpected field is dropped
            )
            self.assertEqual(sorted(rec.keys()), sorted(TX_LOG_FIELDS))
            with open(path) as f:
                stored = json.loads(f.read().strip())
            self.assertEqual(stored["tx_hash"], "0x" + "ab" * 32)
            self.assertNotIn("private_key", stored)
            self.assertNotIn("SHOULD-NOT-APPEAR", json.dumps(stored))


class TestSignAndSubmitOrchestration(unittest.TestCase):
    """Full orchestration with a mocked Web3 — no network, throwaway key."""

    def _provisioned(self, tmp):
        info = _tmp_keystore(tmp)
        return TestnetExecutionConfig(
            enabled=True,
            rpc_url="https://sepolia.example",
            keystore_path=os.path.join(tmp, "keystore.json"),
            passphrase_path=os.path.join(tmp, "pass"),
            address_path=os.path.join(tmp, "address"),
            tx_log_path=os.path.join(tmp, "txlog.jsonl"),
        ), info["account"].address

    def _mock_w3(self):
        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth.chain_id = 11155111
        w3.eth.get_transaction_count.return_value = 7
        w3.eth.get_block.return_value = {"baseFeePerGas": 10**9}
        w3.to_wei.side_effect = lambda v, u: int(v * 10**9) if u == "gwei" else v
        w3.eth.wait_for_transaction_receipt.return_value = {"status": 1, "gasUsed": 21000}
        return w3

    def test_happy_path_records_mined_tx(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, sender = self._provisioned(tmp)
            w3 = self._mock_w3()
            record = sign_and_submit_testnet_tx(
                # The runner never invents recipients: the intent itself must
                # name the agent wallet for a self-transfer.
                {"to": sender, "value": 0, "gas": 21000, "data": b""},
                purpose="self 0-value relay check",
                config=cfg,
                web3_factory=lambda url: w3,
            )
            self.assertEqual(record["outcome"], "mined")
            self.assertEqual(record["chain_id"], 11155111)
            self.assertEqual(record["actual_gas_used"], 21000)
            self.assertEqual(record["target"], sender.lower())
            self.assertTrue(w3.eth.send_raw_transaction.called)

    def test_disabled_gate_blocks_before_any_rpc_or_signing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._provisioned(tmp)
            disabled = TestnetExecutionConfig(
                enabled=False,
                rpc_url=cfg.rpc_url,
                keystore_path=cfg.keystore_path,
                passphrase_path=cfg.passphrase_path,
                address_path=cfg.address_path,
            )
            w3 = MagicMock()
            with self.assertRaises(TestnetSafetyError):
                sign_and_submit_testnet_tx(
                    {"to": None, "value": 0, "gas": 21000, "data": b""},
                    purpose="should never run",
                    config=disabled,
                    web3_factory=lambda url: w3,
                )
            w3.eth.assert_not_called() if hasattr(w3, "eth") else None
            self.assertFalse(w3.method_calls)

    def test_mainnet_rpc_blocks_before_signing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._provisioned(tmp)
            w3 = self._mock_w3()
            w3.eth.chain_id = 1  # RPC reports MAINNET
            with self.assertRaises(TestnetSafetyError) as ctx:
                sign_and_submit_testnet_tx(
                    {"to": None, "value": 0, "gas": 21000, "data": b""},
                    purpose="should never sign",
                    config=cfg,
                    web3_factory=lambda url: w3,
                )
            self.assertIn("refuses Ethereum mainnet", str(ctx.exception))
            # No signing occurred: the send was never reached.
            self.assertFalse(w3.eth.send_raw_transaction.called)

    def test_allowlisted_but_unexpected_rpc_chain_blocks_before_signing(self):
        """ENG-004 §6 (replaces the old ineffective test): the REAL mismatch
        branch. The expected chain IS allowlisted (require_ready passes) and
        the RPC reports a DIFFERENT allowlisted chain — the mismatch must
        fire before wallet signing. Production allowlist is never touched;
        a controlled allowlist is injected explicitly."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._provisioned(tmp)
            cfg = TestnetExecutionConfig(
                enabled=True,
                rpc_url=cfg.rpc_url,
                keystore_path=cfg.keystore_path,
                passphrase_path=cfg.passphrase_path,
                address_path=cfg.address_path,
                tx_log_path=cfg.tx_log_path,
                expected_chain_id=SEPOLIA,           # allowlisted → ready
                allowlist=TWO_CHAIN_ALLOWLIST,       # controlled test injection
            )
            w3 = self._mock_w3()
            w3.eth.chain_id = HOLESKY                # allowlisted, unexpected
            with self.assertRaises(TestnetSafetyError) as ctx:
                sign_and_submit_testnet_tx(
                    {"to": None, "value": 0, "gas": 21000, "data": b""},
                    purpose="should never sign",
                    config=cfg,
                    web3_factory=lambda url: w3,
                )
            self.assertIn("RPC chain mismatch", str(ctx.exception))
            self.assertFalse(w3.eth.send_raw_transaction.called)

    def _flipping_w3(self, chain_values):
        """Web3 stub whose chain_id advances through `chain_values` on each
        read; records raw submissions."""
        class _FlippingW3:
            def __init__(self):
                self._values = list(chain_values)
                self._i = 0
                self.sent = []
                self.submitted = 0
                self.eth = self  # the gate reads w3.eth.chain_id

            def is_connected(self):
                return True

            @property
            def chain_id(self):
                value = self._values[min(self._i, len(self._values) - 1)]
                self._i += 1
                return value

            @property
            def gas_price(self):
                return 10**9

            def get_transaction_count(self, addr):
                return 7

            def get_block(self, tag):
                return {"baseFeePerGas": 10**9}

            def to_wei(self, v, u):
                return int(v * 10**9) if u == "gwei" else v

            def send_raw_transaction(self, raw):
                self.submitted += 1
                self.sent.append(raw)
                return b"\xab" * 32

            def wait_for_transaction_receipt(self, h, timeout=None):
                return {"status": 1, "gasUsed": 21000}

        return _FlippingW3()

    def test_chain_identity_change_between_signing_and_submission_prevents_sending(self):
        """ENG-004 §6: the RPC reports Sepolia up to and including signing,
        then changes identity before submission — the pre-submission check
        must refuse to send."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, sender = self._provisioned(tmp)
            cfg = TestnetExecutionConfig(
                enabled=True,
                rpc_url=cfg.rpc_url,
                keystore_path=cfg.keystore_path,
                passphrase_path=cfg.passphrase_path,
                address_path=cfg.address_path,
                tx_log_path=cfg.tx_log_path,
                expected_chain_id=SEPOLIA,
                allowlist=TWO_CHAIN_ALLOWLIST,  # both chains allowlisted
            )
            w3 = self._flipping_w3([SEPOLIA, SEPOLIA, HOLESKY])
            with self.assertRaises(TestnetSafetyError) as ctx:
                sign_and_submit_testnet_tx(
                    {"to": sender, "value": 0, "gas": 21000, "data": b""},
                    purpose="chain flip must never send",
                    config=cfg,
                    web3_factory=lambda url: w3,
                )
            self.assertIn("RPC chain mismatch", str(ctx.exception))
            self.assertEqual(w3.submitted, 0)  # never sent

    def test_policy_violation_blocks_before_signing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._provisioned(tmp)
            w3 = self._mock_w3()
            stranger = "0x" + "bb" * 20
            with self.assertRaises(TestnetSafetyError) as ctx:
                sign_and_submit_testnet_tx(
                    {"to": stranger, "value": 10**15, "gas": 21000, "data": b""},
                    purpose="arbitrary transfer must never sign",
                    config=cfg,
                    web3_factory=lambda url: w3,
                )
            self.assertIn("no arbitrary EOA transfers", str(ctx.exception))
            self.assertFalse(w3.eth.send_raw_transaction.called)

    def test_over_cap_rpc_suggested_fee_blocks_before_signing(self):
        """If the RPC suggests a fee price above the monetary policy, the
        filled transaction is refused — never silently clipped."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, sender = self._provisioned(tmp)
            w3 = self._mock_w3()
            w3.eth.get_block.return_value = {"baseFeePerGas": 400 * 10**9}
            with self.assertRaises(TestnetSafetyError) as ctx:
                sign_and_submit_testnet_tx(
                    {"to": sender, "value": 0, "gas": 21000, "data": b""},
                    purpose="over-cap fee must never sign",
                    config=cfg,
                    web3_factory=lambda url: w3,
                )
            self.assertIn("exceeds the testnet cap", str(ctx.exception))
            self.assertFalse(w3.eth.send_raw_transaction.called)


if __name__ == "__main__":
    unittest.main()
