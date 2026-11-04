"""
tests/test_testnet_gate.py
==========================
Offline, deterministic tests for the DSS testnet-only execution boundary
(executor/testnet_gate.py, DSS-WO-ENG-003 D; unified per DSS-WO-ENG-004).

Every safety invariant of the gate is pinned here:

  * gate disabled by default and fail-closed on missing configuration;
  * mainnet (1) ALWAYS rejected, unknown chains rejected, allowlisted
    testnets pass, allowlisted-but-unexpected RPC chain rejected BEFORE
    signing (ENG-004 §6: exercised through an explicitly injected test
    allowlist — the production allowlist stays Sepolia-only and can never
    come from the environment), and a chain identity change between
    signing and submission prevents sending;
  * transaction policy: no deployments, no arbitrary EOA transfers, no
    unverified contract targets, hard value/gas caps, monetary fee
    exposure caps for EIP-1559 and legacy transactions (missing,
    negative, malformed and over-cap fee inputs rejected), no unlimited
    token approvals, chainId binding;
  * wallet custody: the single wallet-loading path verifies secret-file
    presence/types/permissions, hex-decodes the passphrase, builds a
    proper eth_account LocalAccount and binds it to the recorded address;
    any mismatch, symlink or loose permission fails closed (throwaway
    keys only — never the live DSS wallet);
  * Flashbots bundle policy: every transaction of a bundle passes the
    same policy; one bad transaction rejects the bundle;
  * simulation-before-submission is enforced by PRODUCTION code
    (FlashbotsBundleSession): failed, missing, stale or payload-mismatched
    simulations prevent submission before any relay submission call, the
    chain identity is re-verified immediately before submission, and the
    authorization is consumed by the submission;
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
    TestnetExecutionConfig,
    TestnetSafetyError,
    check_chain_id,
    evaluate_bundle_simulation,
    load_agent_wallet,
    record_testnet_tx,
    sign_and_submit_testnet_tx,
    validate_bundle,
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


class TestFlashbotsSessionEnforcement(unittest.TestCase):
    """ENG-004 §4: submission is impossible without a successful simulation
    of the exact payload for the exact target block — enforced in the
    production orchestration, exercised here with a mock relay."""

    def _raws(self, n=1):
        return [os.urandom(120) for _ in range(n)]  # arbitrary signed bytes

    def _session(self, tmp, relay, *, enabled=True, allowlist=None):
        cfg = _bundle_cfg(tmp, enabled=enabled, allowlist=allowlist)
        return FlashbotsBundleSession(
            relay, sender="0x" + "aa" * 20, config=cfg, chain_id=SEPOLIA,
        ), cfg

    def test_submit_without_simulation_refused_before_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            session, _ = self._session(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit(self._raws(), 100)
            self.assertIn("no successful simulation", str(ctx.exception))
            self.assertFalse(relay.send_calls)

    def test_failed_simulation_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(sim_result={"error": {"message": "execution reverted"}})
            session, _ = self._session(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate(self._raws(), 100)
            self.assertIn("simulation failed", str(ctx.exception))
            with self.assertRaises(TestnetSafetyError):
                session.submit(self._raws(), 100)
            self.assertFalse(relay.send_calls)

    def test_reverting_transaction_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = _ok_simulation()
            sim["results"][0]["error"] = "execution reverted"
            relay = _RelayStub(sim_result=sim)
            session, _ = self._session(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate(self._raws(), 100)
            self.assertIn("tx[0] would fail", str(ctx.exception))
            self.assertFalse(relay.send_calls)

    def test_first_revert_response_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = _ok_simulation()
            sim["firstRevert"] = "0x" + "ee" * 32
            relay = _RelayStub(sim_result=sim)
            session, _ = self._session(tmp, relay)
            with self.assertRaises(TestnetSafetyError):
                session.simulate(self._raws(), 100)
            self.assertFalse(relay.send_calls)

    def test_empty_simulation_results_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(sim_result={"bundleHash": "0x1", "results": []})
            session, _ = self._session(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate(self._raws(), 100)
            self.assertIn("no per-transaction results", str(ctx.exception))

    def test_missing_simulation_response_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(sim_result=None)
            session, _ = self._session(tmp, relay)
            with self.assertRaises(TestnetSafetyError):
                session.simulate(self._raws(), 100)
            self.assertFalse(relay.send_calls)

    def test_result_count_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(sim_result=_ok_simulation(n_txs=1))
            session, _ = self._session(tmp, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate(self._raws(n=2), 100)
            self.assertIn("malformed response", str(ctx.exception))

    def test_stale_simulation_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, _ = self._session(tmp, relay)
            raws = self._raws()
            session.simulate(raws, 100)  # authorized at head 99
            relay.advance(1)             # head becomes 100 → target reached
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit(raws, 100)
            self.assertIn("stale submission refused", str(ctx.exception))
            self.assertFalse(relay.send_calls)

    def test_payload_mismatch_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            session, _ = self._session(tmp, relay)
            simulated = self._raws()
            session.simulate(simulated, 100)
            tampered = [bytes(simulated[0]) + b"\x01"]  # different payload
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit(tampered, 100)
            self.assertIn("no successful simulation", str(ctx.exception))
            self.assertFalse(relay.send_calls)

    def test_target_block_mismatch_prevents_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            session, _ = self._session(tmp, relay)
            raws = self._raws()
            session.simulate(raws, 100)
            with self.assertRaises(TestnetSafetyError):
                session.submit(raws, 101)  # different target block
            self.assertFalse(relay.send_calls)

    def test_chain_identity_change_between_sim_and_submit_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Allowlisted chains both; expected Sepolia. RPC reports Sepolia
            # for the simulation, then Holesky for the submission.
            relay = _RelayStub(chain_values=(SEPOLIA, HOLESKY))
            session, _ = self._session(tmp, relay, allowlist=TWO_CHAIN_ALLOWLIST)
            raws = self._raws()
            session.simulate(raws, 100)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit(raws, 100)
            self.assertIn("RPC chain mismatch", str(ctx.exception))
            self.assertFalse(relay.send_calls)

    def test_submission_requires_execution_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub()
            session, _ = self._session(tmp, relay, enabled=False)
            raws = self._raws()
            session.simulate(raws, 100)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit(raws, 100)
            self.assertIn("DSS_TESTNET_EXEC=1", str(ctx.exception))
            self.assertFalse(relay.send_calls)

    def test_happy_path_simulate_then_submit_audits_and_consumes_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, cfg = self._session(tmp, relay)
            raws = self._raws()
            sim = session.simulate(raws, 100)
            self.assertEqual(len(relay.simulate_calls), 1)
            self.assertEqual(relay.simulate_calls[0][1], 100)
            out = session.submit(raws, 100)
            self.assertEqual(len(relay.send_calls), 1)
            # The submitted payload must be exactly the simulated payload.
            sent_bundle, sent_block = relay.send_calls[0]
            self.assertEqual(sent_block, 100)
            self.assertEqual(
                [dict(t)["signed_transaction"] for t in sent_bundle],
                raws,
            )
            self.assertEqual(out["audit"]["outcome"], "submitted_to_relay")
            with open(cfg.tx_log_path) as f:
                outcomes = [json.loads(line)["outcome"] for line in f if line.strip()]
            self.assertIn("simulated", outcomes)
            self.assertIn("submitted_to_relay", outcomes)
            # The authorization is consumed: a second submission is refused.
            with self.assertRaises(TestnetSafetyError):
                session.submit(raws, 100)
            self.assertEqual(len(relay.send_calls), 1)

    def test_submission_cannot_run_with_future_target_under_disabled_gate(self):
        """Even a perfectly simulated payload cannot reach the relay while
        the execution gate is disabled."""
        with tempfile.TemporaryDirectory() as tmp:
            relay = _RelayStub(head=99)
            session, _ = self._session(tmp, relay, enabled=False)
            raws = self._raws()
            session.simulate(raws, 100)
            with self.assertRaises(TestnetSafetyError):
                session.submit(raws, 100)
            self.assertFalse(relay.send_calls)


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
