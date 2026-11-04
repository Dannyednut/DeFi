"""
tests/test_testnet_gate.py
==========================
Offline, deterministic tests for the DSS testnet-only execution boundary
(executor/testnet_gate.py, DSS-WO-ENG-003 Workstream D).

Every safety invariant of the gate is pinned here:

  * gate disabled by default and fail-closed on missing configuration;
  * mainnet (1) ALWAYS rejected, unknown chains rejected, allowlisted
    testnets pass, RPC/expectation mismatch rejected;
  * transaction policy: no deployments, no arbitrary EOA transfers, no
    unverified contract targets, hard value/gas caps, no unlimited token
    approvals, chainId binding;
  * EXECUTE_ONCHAIN (the production guard) stays false and untouched;
  * the audit log records the required fields and can never contain
    credentials;
  * the sign-and-submit orchestration verifies chain identity before
    signing AND before submission, binds the wallet identity, and refuses
    any violation — exercised end-to-end with a mocked Web3 and a
    throwaway keystore; nothing touches a network.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account

from executor.testnet_gate import (
    ALLOWED_TESTNET_CHAINS,
    DEFAULT_KEYSTORE_PATH,
    MAINNET_CHAIN_ID,
    MAX_TOKEN_APPROVAL_AMOUNT,
    TestnetExecutionConfig,
    TestnetSafetyError,
    check_chain_id,
    record_testnet_tx,
    sign_and_submit_testnet_tx,
    validate_transaction,
    TX_LOG_FIELDS,
)


def _tmp_keystore(tmpdir: str, address: str) -> dict:
    """Create a throwaway encrypted keystore (offline, throwaway key)."""
    acct = Account.create()
    # The THROWAWAY account signs in tests; the recorded address must match
    # it, so we return the real one instead of the parameter.
    keystore = Account.encrypt(acct.key, b"test-passphrase")
    with open(os.path.join(tmpdir, "keystore.json"), "w") as f:
        json.dump(keystore, f)
    with open(os.path.join(tmpdir, "pass"), "wb") as f:
        f.write(b"test-passphrase".hex().encode())
    with open(os.path.join(tmpdir, "address"), "w") as f:
        f.write(acct.address + "\n")
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
            self.assertIn("credential missing", str(ctx.exception))

    def test_enabled_with_unknown_chain_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tmp_keystore(tmp, "0x" + "1" * 40)
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
            "    stays false. This boundary is a SEPARATE gate that only ever permits",
            ""))


class TestChainIdGate(unittest.TestCase):
    def test_mainnet_always_rejected(self):
        with self.assertRaises(TestnetSafetyError) as ctx:
            check_chain_id(1)
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
        data = bytes.fromhex("095ea7b3") + b"\x00" * 12 + self.STRANGER.encode()[:0] + unlimited
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
                private_key="SHOULD-NOT-APPEAR",  # unexpected field is dropped
            )
            self.assertEqual(sorted(rec.keys()), sorted(TX_LOG_FIELDS))
            with open(path) as f:
                stored = json.loads(f.read().strip())
            self.assertEqual(stored["tx_hash"], "0x" + "ab" * 32)
            self.assertNotIn("private_key", stored)
            self.assertNotIn("SHOULD-NOT-APPEAR", f.read() if False else json.dumps(stored))


class TestSignAndSubmitOrchestration(unittest.TestCase):
    """Full orchestration with a mocked Web3 — no network, throwaway key."""

    def _provisioned(self, tmp):
        info = _tmp_keystore(tmp, "0x" + "1" * 40)
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

    def test_mismatched_rpc_blocks_before_signing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._provisioned(tmp)
            w3 = self._mock_w3()
            w3.eth.chain_id = 11155111
            cfg = TestnetExecutionConfig(
                enabled=True, rpc_url=cfg.rpc_url,
                keystore_path=cfg.keystore_path,
                passphrase_path=cfg.passphrase_path,
                address_path=cfg.address_path,
                tx_log_path=cfg.tx_log_path,
                expected_chain_id=999999,  # not allowlisted -> require_ready OK? no:
            )
            with self.assertRaises(TestnetSafetyError):
                sign_and_submit_testnet_tx(
                    {"to": None, "value": 0, "gas": 21000, "data": b""},
                    purpose="should never sign",
                    config=cfg,
                    web3_factory=lambda url: w3,
                )
            self.assertFalse(w3.eth.send_raw_transaction.called)

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


if __name__ == "__main__":
    unittest.main()
