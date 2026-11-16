"""
tests/test_relay.py
====================
Unit tests for executor/relay.py — the single transaction-submission layer
(DSS-WO-ENG-015 R4).

Behaviour pinned here (all fully offline — throwaway keys and mock
transports; no real wallet, no network, no live signing of any real
asset):

  enforcement at the submission boundary
  - EXECUTE_ONCHAIN disabled → ExecutionRefused, nothing is broadcast;
  - RPC-reported chain mismatch → ExecutionRefused, nothing is broadcast;
  - a payload signed by anything but the recorded agent identity →
    ExecutionRefused (provenance);
  - direct submission succeeds only when the switch is on, the chain
    matches and the payload is the agent's;
  - the chain is re-checked IMMEDIATELY before broadcast: a chain that
    changes between the first read and the broadcast is refused;
  - the chain is ALSO re-checked IMMEDIATELY before a private bundle
    submission (DSS-WO-ENG-016 §3C): a chain that changes between the
    initial verification and send_bundle() prevents the submission;
  - private (Flashbots) submission NEVER falls back to the public
    mempool on failure;
  - private submission respects the chain-specific MEV policy;
  - a stale target block is refused.

  read-only helpers
  - wait_for_receipt returns the receipt dict on success and None on
    timeout/failure.

Offline signing uses a THROWAWAY key generated in tmp (never the agent
wallet, never a funded key, never broadcast anywhere).
"""
import inspect
import json
import os
import stat
import unittest
from unittest.mock import MagicMock, patch
from web3.exceptions import TransactionNotFound

from eth_account import Account

from executor.errors import ExecutionRefused
from executor.relay import RelayResult, wait_for_receipt, relay_transaction

SEPOLIA = 11155111


# ── offline fixtures ─────────────────────────────────────────────────────────

def _provision_throwaway_store(tmp_path, key: Account) -> None:
    """Provision a compliant DSS secret store around a THROWAWAY key."""
    store = tmp_path / "secrets"
    store.mkdir(mode=0o700, exist_ok=True)
    keystore = Account.encrypt(key.key, b"offline-test-passphrase")
    p = store / "dss_wallet.keystore.json"
    p.write_text(json.dumps(keystore))
    os.chmod(p, 0o600)
    p = store / "dss_wallet.pass"
    p.write_text(b"offline-test-passphrase".hex())
    os.chmod(p, 0o600)
    p = store / "dss_wallet.address"
    p.write_text(key.address)
    os.chmod(p, 0o600)


def _signed_raw(key: Account, chain_id: int = SEPOLIA, to=None, value: int = 0) -> bytes:
    """Offline-sign one minimal transaction with a THROWAWAY key."""
    from eth_utils import to_checksum_address
    to = to_checksum_address(to or "0x" + "9a" * 20)
    tx = {
        "to": to,
        "value": value,
        "gas": 21_000,
        "maxFeePerGas": 30 * 10**9,
        "maxPriorityFeePerGas": 1 * 10**9,
        "nonce": 0,
        "chainId": chain_id,
    }
    signed = key.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    return bytes(raw)


class _FakeEth:
    """Deterministic chain-ID sequence + recorded broadcasts."""

    def __init__(self, chain_sequence, head: int = 100):
        self._seq = list(chain_sequence)
        self._i = 0
        self.sent = []
        self.block_number = head

    @property
    def chain_id(self):
        value = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return value

    def send_raw_transaction(self, raw):
        self.sent.append(bytes(raw))
        return b"\xab" * 32


class _FakeW3:
    def __init__(self, chain_sequence):
        self.eth = _FakeEth(chain_sequence)


# ── enforcement at the submission boundary ───────────────────────────────────

class TestRelaySubmissionEnforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("pathlib").Path(
            __import__("tempfile").mkdtemp(prefix="relay-test-")
        )
        self.key = Account.create()
        _provision_throwaway_store(self.tmp, self.key)
        self._env = patch.dict(
            os.environ,
            {"EXECUTE_ONCHAIN": "true", "DSS_SECRET_STORE": str(self.tmp / "secrets")},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_disabled_switch_refuses_without_broadcast(self):
        with patch.dict(os.environ, {"EXECUTE_ONCHAIN": "false"}):
            w3 = _FakeW3([SEPOLIA, SEPOLIA])
            raw = _signed_raw(self.key)
            with self.assertRaises(ExecutionRefused) as ctx:
                relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
            self.assertIn("disabled", str(ctx.exception))
            self.assertEqual(w3.eth.sent, [])

    def test_chain_mismatch_refuses_before_provenance(self):
        w3 = _FakeW3([1])  # reports chain 1, expected Sepolia
        raw = _signed_raw(self.key)
        with self.assertRaises(ExecutionRefused) as ctx:
            relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
        self.assertIn("chain mismatch", str(ctx.exception))
        self.assertEqual(w3.eth.sent, [])

    def test_payload_from_another_signer_is_refused(self):
        impostor = Account.create()
        w3 = _FakeW3([SEPOLIA])
        raw = _signed_raw(impostor)
        with self.assertRaises(ExecutionRefused) as ctx:
            relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
        self.assertIn("approved signing process", str(ctx.exception))
        self.assertEqual(w3.eth.sent, [])

    def test_payload_for_another_chain_is_refused(self):
        w3 = _FakeW3([SEPOLIA])
        raw = _signed_raw(self.key, chain_id=1)  # signed for mainnet
        with self.assertRaises(ExecutionRefused) as ctx:
            relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
        self.assertIn("built for chain", str(ctx.exception))
        self.assertEqual(w3.eth.sent, [])

    def test_direct_submission_success_path(self):
        w3 = _FakeW3([SEPOLIA, SEPOLIA])
        raw = _signed_raw(self.key)
        result = relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
        self.assertTrue(result.success)
        self.assertEqual(result.method, "direct")
        self.assertEqual(len(w3.eth.sent), 1)
        self.assertEqual(w3.eth.sent[0], raw)

    def test_chain_change_before_broadcast_is_refused(self):
        # read 1 = Sepolia (relay entry), read 2 = chain 1 (pre-broadcast
        # recheck) → refused at the transport gate, nothing broadcast.
        w3 = _FakeW3([SEPOLIA, 1])
        raw = _signed_raw(self.key)
        result = relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
        self.assertFalse(result.success)
        self.assertEqual(result.method, "refused")
        self.assertIn("chain mismatch", result.error)
        self.assertEqual(w3.eth.sent, [])


# ── private submission ───────────────────────────────────────────────────────

class TestRelayPrivateSubmission(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("pathlib").Path(
            __import__("tempfile").mkdtemp(prefix="relay-test-")
        )
        self.key = Account.create()
        _provision_throwaway_store(self.tmp, self.key)
        self._env = patch.dict(
            os.environ,
            {"EXECUTE_ONCHAIN": "true", "DSS_SECRET_STORE": str(self.tmp / "secrets")},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _flashbots_w3(self, chain_sequence, send_bundle_result=None):
        w3 = _FakeW3(chain_sequence)
        w3.flashbots = MagicMock()
        w3.flashbots.send_bundle.return_value = send_bundle_result
        return w3

    def test_private_failure_never_falls_back_to_public(self):
        with patch("executor.relay.FLASHBOTS_CHAINS", frozenset({SEPOLIA})):
            w3 = self._flashbots_w3([SEPOLIA], send_bundle_result=None)
            raw = _signed_raw(self.key)
            result = relay_transaction(
                w3, raw, expected_chain_id=SEPOLIA,
                use_flashbots=True, target_block=101,  # fresh target
            )
            self.assertFalse(result.success)
            self.assertEqual(result.method, "flashbots")
            self.assertIn("rejected", result.error)
            # The public mempool must NEVER have been touched.
            self.assertEqual(w3.eth.sent, [])

    def test_private_submission_respects_chain_mev_policy(self):
        # Polygon (137): execution_profile marks private_submission=False →
        # the chain MEV policy refuses atomic backruns regardless of any
        # registry entry.
        with patch("executor.relay.FLASHBOTS_CHAINS", frozenset({137})):
            w3 = self._flashbots_w3([137])
            raw = _signed_raw(self.key, chain_id=137)
            result = relay_transaction(
                w3, raw, expected_chain_id=137,
                use_flashbots=True, target_block=101,
            )
            self.assertFalse(result.success)
            self.assertIn("not enabled for chain 137", result.error)
            self.assertEqual(w3.eth.sent, [])

    def test_private_submission_refused_without_registry_entry(self):
        # FLASHBOTS_RPCS (and thus FLASHBOTS_CHAINS) has no entry for the
        # chain → fail closed before any transport interaction.
        with patch("executor.relay.FLASHBOTS_CHAINS", frozenset()):
            w3 = self._flashbots_w3([SEPOLIA])
            raw = _signed_raw(self.key)
            result = relay_transaction(
                w3, raw, expected_chain_id=SEPOLIA,
                use_flashbots=True, target_block=100,
            )
            self.assertFalse(result.success)
            self.assertIn("no Flashbots relay configured", result.error)
            self.assertEqual(w3.eth.sent, [])

    def test_stale_target_block_is_refused(self):
        with patch("executor.relay.FLASHBOTS_CHAINS", frozenset({SEPOLIA})):
            w3 = self._flashbots_w3([SEPOLIA])
            # head is 100; a target at or below the head is stale.
            raw = _signed_raw(self.key)
            result = relay_transaction(
                w3, raw, expected_chain_id=SEPOLIA,
                use_flashbots=True, target_block=100,
            )
            self.assertFalse(result.success)
            self.assertEqual(result.error, "target block is stale")
            self.assertEqual(w3.eth.sent, [])

    # ── DSS-WO-ENG-016 §3C: final chain-identity verification on the
    # Flashbots route (parity with the direct route) ────────────────────

    def test_chain_change_before_bundle_submission_is_refused(self):
        # The chain changes between the initial verification (read 1 =
        # Sepolia, at the relay entry) and the FINAL pre-bundle recheck
        # (read 2 = chain 1, immediately before send_bundle) → the bundle
        # is never sent and nothing is broadcast.
        with patch("executor.relay.FLASHBOTS_CHAINS", frozenset({SEPOLIA})):
            w3 = self._flashbots_w3([SEPOLIA, 1])
            raw = _signed_raw(self.key)
            result = relay_transaction(
                w3, raw, expected_chain_id=SEPOLIA,
                use_flashbots=True, target_block=101,
                target_tx=b"\x09" * 32,  # atomic-backrun bundle shape
            )
            self.assertFalse(result.success)
            self.assertEqual(result.method, "refused")
            self.assertIn("chain mismatch", result.error)
            w3.flashbots.send_bundle.assert_not_called()
            self.assertEqual(w3.eth.sent, [])

    def test_bundle_submitted_when_chain_identity_stable(self):
        # Control: the chain identity is re-verified and still matches →
        # the bundle IS submitted exactly once, to the fresh target block.
        with patch("executor.relay.FLASHBOTS_CHAINS", frozenset({SEPOLIA})):
            w3 = self._flashbots_w3([SEPOLIA])
            raw = _signed_raw(self.key)
            result = relay_transaction(
                w3, raw, expected_chain_id=SEPOLIA,
                use_flashbots=True, target_block=101,
            )
            w3.flashbots.send_bundle.assert_called_once()
            self.assertEqual(
                w3.flashbots.send_bundle.call_args.kwargs.get(
                    "target_block_number"),
                101,
            )
            self.assertEqual(result.method, "flashbots")
            self.assertEqual(w3.eth.sent, [])


# ── read-only helpers ────────────────────────────────────────────────────────

class TestWaitForReceipt(unittest.TestCase):
    """Read-only receipt handling."""

    def setUp(self):
        self.mock_w3 = MagicMock()

    def test_returns_receipt_dict_on_success(self):
        receipt = {"status": 1, "gasUsed": 21000,
                   "effectiveGasPrice": 1_000_000_000,
                   "blockNumber": 12345679}
        self.mock_w3.eth.wait_for_transaction_receipt.return_value = receipt

        result = wait_for_receipt(self.mock_w3, "0x" + "ab" * 32, timeout=5)

        self.mock_w3.eth.wait_for_transaction_receipt.assert_called_once_with(
            "0x" + "ab" * 32, timeout=5, poll_latency=2.0
        )
        self.assertEqual(result, receipt)

    def test_returns_none_on_transaction_not_found(self):
        self.mock_w3.eth.wait_for_transaction_receipt.side_effect = \
            TransactionNotFound("not mined")

        result = wait_for_receipt(self.mock_w3, "0x" + "ab" * 32, timeout=1)

        self.assertIsNone(result)

    def test_returns_none_on_timeout(self):
        self.mock_w3.eth.wait_for_transaction_receipt.side_effect = \
            TimeoutError("receipt wait timed out")

        result = wait_for_receipt(self.mock_w3, "0x" + "ab" * 32, timeout=1)

        self.assertIsNone(result)


class TestRelayResultRecord(unittest.TestCase):
    """The RelayResult record remains a plain result value object."""

    def test_defaults(self):
        result = RelayResult(success=False, tx_hash="", method="refused")
        self.assertEqual(result.error, "")
        self.assertFalse(result.success)

    def test_method_documented_values(self):
        for method in ("direct", "flashbots", "refused"):
            result = RelayResult(success=True, tx_hash="0x" + "ab" * 32,
                                 method=method)
            self.assertEqual(result.method, method)


class TestRelayErrorContainment(unittest.TestCase):
    """Relay failures must not echo raw signed payload material."""

    def test_direct_failure_sanitizes_error(self):
        tmp = __import__("pathlib").Path(
            __import__("tempfile").mkdtemp(prefix="relay-test-")
        )
        key = Account.create()
        _provision_throwaway_store(tmp, key)
        with patch.dict(
            os.environ,
            {"EXECUTE_ONCHAIN": "true", "DSS_SECRET_STORE": str(tmp / "secrets")},
        ):
            class _ExplodingEth(_FakeEth):
                def send_raw_transaction(self, raw):
                    self.sent.append(bytes(raw))
                    raise RuntimeError(f"provider error for {bytes(raw).hex()}")

            w3 = _FakeW3.__new__(_FakeW3)
            w3.eth = _ExplodingEth([SEPOLIA, SEPOLIA])
            raw = _signed_raw(key)
            result = relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
            self.assertFalse(result.success)
            # The exact payload hex is layer-1 forbidden material — replaced
            # wholesale before any generic rule.
            self.assertNotIn(bytes(raw).hex(), result.error)
            self.assertIn("<redacted>", result.error)


if __name__ == "__main__":
    unittest.main()
