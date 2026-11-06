"""
tests/test_relay.py
====================
Unit tests for executor/relay.py — receipt handling only.

DSS-WO-ENG-015: the relay module NO LONGER submits transactions. The legacy
broadcast helpers were removed so that no code path outside the unified
execution authorization boundary's gate orchestrations
(``executor/testnet_gate.py``, reached through ``executor/authorization.py``)
can sign or broadcast a raw transaction:

  - ``relay_transaction`` / ``_relay_direct`` / ``_relay_flashbots`` are gone;
    direct-RPC submission and Flashbots bundle submission live exclusively in
    the gate orchestrations (chain-verified, policy-checked, wallet-bound,
    audited, fail-closed);
  - only read-only receipt handling (``wait_for_receipt``) and the
    ``RelayResult`` record remain here.

Behaviour pinned here:

  - the module exposes NO submission callable any more;
  - the module source contains no raw-broadcast call
    (``eth_sendRawTransaction`` / ``send_bundle``);
  - ``wait_for_receipt`` returns the receipt dict on success and None on
    timeout/failure (read-only — it never writes);

All tests are fully offline: ``w3`` is a MagicMock; no credentials, no
signing, no network activity.
"""
import inspect
import unittest
from unittest.mock import MagicMock
from web3.exceptions import TransactionNotFound

from executor.relay import RelayResult, wait_for_receipt


class TestRelayModuleHasNoSubmissionPath(unittest.TestCase):
    """DSS-WO-ENG-015: the relay module must not offer any way to broadcast
    a raw transaction — submission authority lives exclusively in the
    authorization boundary's gate orchestrations."""

    def test_removed_submission_helpers_no_longer_exist(self):
        import executor.relay as relay_module

        for name in ("relay_transaction", "_relay_direct", "_relay_flashbots"):
            self.assertFalse(
                hasattr(relay_module, name),
                f"executor.relay.{name} must not exist: submission moved to "
                "the unified execution authorization boundary",
            )

    def test_module_source_contains_no_broadcast_call(self):
        import executor.relay as relay_module

        source = inspect.getsource(relay_module)
        for forbidden in ("send_raw_transaction", "send_bundle",
                          "eth_sendRawTransaction", "eth_sendBundle"):
            self.assertNotIn(
                forbidden, source,
                f"executor.relay must not reference {forbidden}: broadcasting "
                "is exclusive to the gate orchestrations",
            )


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


if __name__ == "__main__":
    unittest.main()
