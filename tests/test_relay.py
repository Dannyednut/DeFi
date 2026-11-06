"""
tests/test_relay.py
====================
Unit tests for executor/relay.py — transaction relay functionality.

DSS-WO-ENG-003: rewritten for the current fail-closed relay interface.

The legacy interface these tests used to pin no longer exists and is
deliberately NOT restored:

  - ``_relay_flashbots(w3, raw_tx, chain_id, target_block=..., relay_signer=...)``
    became ``_relay_flashbots(w3, raw_tx, target_block, target_tx=b"")``;
    relay signing identity is provisioned once via the Flashbots middleware
    (executor/executor.py), not per-call.
  - ImportError/empty-result/exception public-mempool fallbacks were removed:
    an atomic backrun that cannot be submitted privately must fail closed,
    never leak to the public relay.
  - Unknown chains no longer fall back to the mainnet Flashbots RPC: chain
    eligibility is decided by ChainMEVPolicy (execution_profile.PROFILES
    private_submission), and FLASHBOTS_RPCS is filtered against it.

Behaviour pinned here:

  - private submission is the only path when use_flashbots=True;
  - every private-path failure is fail-closed (no public fallback);
  - a stale target block is refused, never silently advanced;
  - a bundle that misses its target block cancels and reports failure
    (regression: this path used to fall through and return None);
  - unsupported chains refuse private submission and use direct submission
    only when explicitly requested (use_flashbots=False).

All tests are fully offline: ``w3`` is a MagicMock; no credentials, no
signing, no network activity.
"""
import unittest
from unittest.mock import MagicMock, patch
from web3.exceptions import TransactionNotFound

from executor.relay import (
    _relay_flashbots,
    _relay_direct,
    relay_transaction,
    RelayResult,
    FLASHBOTS_CHAINS,
)


RAW_TX = bytes.fromhex("f86c" + "00" * 44)  # Minimal valid-looking raw tx
TARGET_TX = bytes.fromhex("f86d" + "11" * 44)


class TestRelayFlashbots(unittest.TestCase):
    """Test cases for _relay_flashbots() — current positional interface.

    Signature: _relay_flashbots(w3, raw_tx, target_block, target_tx=b"").
    The Flashbots middleware is assumed to be attached to ``w3`` upstream;
    the function itself performs no package imports and no signer handling.
    """

    def setUp(self):
        self.mock_w3 = MagicMock()
        self.mock_w3.eth.block_number = 12345678

    def _successful_bundle_result(self):
        mock_result = MagicMock()
        mock_result.bundle_hash.return_value.hex.return_value = "0x" + "ef" * 32
        self.mock_w3.flashbots.send_bundle.return_value = mock_result
        return mock_result

    def test_successful_submission(self):
        """A single private tx is submitted as a one-entry bundle."""
        self._successful_bundle_result()

        result = _relay_flashbots(self.mock_w3, RAW_TX, target_block=12345679)

        self.mock_w3.flashbots.send_bundle.assert_called_once_with(
            [{"signed_transaction": RAW_TX}], target_block_number=12345679
        )
        self.assertTrue(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertEqual(result.tx_hash, "0x" + "ef" * 32)
        self.assertEqual(result.error, "")

    def test_bundle_includes_trigger_transaction_first(self):
        """An atomic backrun bundles the trigger tx in front of the backrun."""
        self._successful_bundle_result()

        _relay_flashbots(
            self.mock_w3, RAW_TX, target_block=12345679, target_tx=TARGET_TX
        )

        self.mock_w3.flashbots.send_bundle.assert_called_once_with(
            [{"signed_transaction": TARGET_TX}, {"signed_transaction": RAW_TX}],
            target_block_number=12345679,
        )

    def test_empty_result_fails_closed_without_public_fallback(self):
        """A rejected private submission must NOT leak to the public mempool."""
        self.mock_w3.flashbots.send_bundle.return_value = None

        with patch("executor.relay._relay_direct") as mock_direct:
            result = _relay_flashbots(self.mock_w3, RAW_TX, target_block=12345679)

        self.assertFalse(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertIn("private bundle rejected", result.error)
        mock_direct.assert_not_called()

    def test_exception_fails_closed_without_public_fallback(self):
        """A relay exception must fail closed, never fall back publicly."""
        self.mock_w3.flashbots.send_bundle.side_effect = Exception("Connection refused")

        with patch("executor.relay._relay_direct") as mock_direct:
            result = _relay_flashbots(self.mock_w3, RAW_TX, target_block=12345679)

        self.assertFalse(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertIn("Connection refused", result.error)
        mock_direct.assert_not_called()

    def test_missing_flashbots_provider_fails_closed(self):
        """Without attached Flashbots middleware the relay fails closed.

        This is the current-world equivalent of the retired import-error
        fallback test: an unavailable private path must not degrade into a
        public submission.
        """
        w3_without_flashbots = MagicMock(spec=["eth"])
        w3_without_flashbots.eth.block_number = 12345678

        with patch("executor.relay._relay_direct") as mock_direct:
            result = _relay_flashbots(
                w3_without_flashbots, RAW_TX, target_block=12345679
            )

        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertEqual(result.method, "flashbots")
        mock_direct.assert_not_called()

    def test_stale_target_block_fails_closed(self):
        """A target block at or behind head is refused before submission."""
        self._successful_bundle_result()

        result = _relay_flashbots(self.mock_w3, RAW_TX, target_block=12345678)

        self.assertFalse(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertIn("target block is stale", result.error)
        self.mock_w3.flashbots.send_bundle.assert_not_called()

    def test_result_without_bundle_hash_reports_pending(self):
        """A result object lacking bundle_hash is still tracked as pending."""
        mock_result = MagicMock(spec=["wait", "receipts"])
        self.mock_w3.flashbots.send_bundle.return_value = mock_result

        result = _relay_flashbots(self.mock_w3, RAW_TX, target_block=12345679)

        self.assertTrue(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertEqual(result.tx_hash, "pending")

    def test_bundle_not_included_cancels_and_fails_closed(self):
        """Regression: a missed bundle cancels and returns a failure result.

        The previous implementation fell through the TransactionNotFound
        branch and returned None, crashing callers that read RelayResult
        attributes (executor.py does ``result.success`` unconditionally).
        """
        mock_result = self._successful_bundle_result()
        mock_result.receipts.side_effect = TransactionNotFound("not mined")

        result = _relay_flashbots(self.mock_w3, RAW_TX, target_block=12345679)

        self.mock_w3.flashbots.cancel_bundles.assert_called_once_with("0x" + "ef" * 32)
        self.assertIsNotNone(result, "relay must never return None")
        self.assertFalse(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertIn("bundle not included", result.error)


class TestRelayTransaction(unittest.TestCase):
    """Test cases for relay_transaction() — current interface.

    Signature: relay_transaction(w3, raw_tx, target_tx=b"", target_block=None,
    use_flashbots=False). Chain eligibility is decided by ChainMEVPolicy,
    derived from execution_profile.PROFILES.
    """

    def setUp(self):
        self.mock_w3 = MagicMock()
        self.mock_w3.eth.chain_id = 1
        self.mock_w3.eth.block_number = 12345678
        self.raw_tx = RAW_TX

    @patch("executor.relay._relay_direct")
    @patch("executor.relay._relay_flashbots")
    def test_relay_transaction_uses_flashbots_for_supported_chains(self, mock_fb, mock_direct):
        """Chains with private_submission support use the bundle relay."""
        mock_fb.return_value = RelayResult(
            success=True, tx_hash="0xflash", method="flashbots"
        )

        # Guard against a silent no-op if configuration ever empties this set.
        self.assertTrue(FLASHBOTS_CHAINS, "no Flashbots-eligible chains configured")

        for chain_id in sorted(FLASHBOTS_CHAINS):
            with self.subTest(chain_id=chain_id):
                self.mock_w3.eth.chain_id = chain_id
                mock_fb.reset_mock()
                mock_direct.reset_mock()

                result = relay_transaction(
                    self.mock_w3,
                    self.raw_tx,
                    target_block=12345679,
                    use_flashbots=True,
                )

                mock_fb.assert_called_once()
                mock_direct.assert_not_called()
                self.assertEqual(result.method, "flashbots")
                self.assertTrue(result.success)

    @patch("executor.relay._relay_flashbots")
    @patch("executor.relay._relay_direct")
    def test_relay_transaction_uses_direct_for_unsupported_chains(self, mock_direct, mock_fb):
        """Chains without private submission support submit directly by default."""
        mock_direct.return_value = RelayResult(
            success=True, tx_hash="0xdirect", method="direct"
        )

        unsupported_chains = [56, 137, 42161, 324]  # BSC, Polygon, Arbitrum, zkSync

        for chain_id in unsupported_chains:
            with self.subTest(chain_id=chain_id):
                self.mock_w3.eth.chain_id = chain_id
                mock_direct.reset_mock()
                mock_fb.reset_mock()

                result = relay_transaction(
                    self.mock_w3, self.raw_tx, target_block=12345679
                )

                mock_direct.assert_called_once_with(self.mock_w3, self.raw_tx)
                mock_fb.assert_not_called()
                self.assertEqual(result.method, "direct")
                self.assertTrue(result.success)

    @patch("executor.relay._relay_flashbots")
    @patch("executor.relay._relay_direct")
    def test_relay_transaction_refuses_flashbots_on_unsupported_chain(self, mock_direct, mock_fb):
        """Explicit use_flashbots on an ineligible chain fails closed.

        The retired behaviour sent unknown chains to the mainnet Flashbots
        RPC; the current behaviour refuses the submission instead.
        """
        self.mock_w3.eth.chain_id = 56

        result = relay_transaction(
            self.mock_w3, self.raw_tx, target_block=12345679, use_flashbots=True
        )

        self.assertFalse(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertIn("private atomic backrun not enabled", result.error)
        mock_fb.assert_not_called()
        mock_direct.assert_not_called()

    @patch("executor.relay._relay_direct")
    @patch("executor.relay._relay_flashbots")
    def test_relay_transaction_auto_targets_next_block(self, mock_fb, mock_direct):
        """Without an explicit target the next block is targeted."""
        mock_fb.return_value = RelayResult(
            success=True, tx_hash="0xflash", method="flashbots"
        )
        self.mock_w3.eth.chain_id = 1

        relay_transaction(self.mock_w3, self.raw_tx, use_flashbots=True)

        mock_fb.assert_called_once_with(
            self.mock_w3, self.raw_tx, 12345679, b""
        )
        mock_direct.assert_not_called()

    @patch("executor.relay._relay_flashbots")
    @patch("executor.relay._relay_direct")
    def test_relay_transaction_refuses_stale_target_block(self, mock_fb, mock_direct):
        """A stale explicit target is refused, never silently advanced."""
        self.mock_w3.eth.chain_id = 1
        self.mock_w3.eth.block_number = 12345678

        result = relay_transaction(
            self.mock_w3, self.raw_tx, target_block=12345678, use_flashbots=True
        )

        self.assertFalse(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertIn("target block is stale", result.error)
        mock_fb.assert_not_called()
        mock_direct.assert_not_called()


class TestRelayDirect(unittest.TestCase):
    """Test cases for _relay_direct() function."""

    def setUp(self):
        self.mock_w3 = MagicMock()
        self.raw_tx = RAW_TX

    def test_direct_relay_success(self):
        """Test successful direct relay submission."""
        expected_hash_bytes = bytes.fromhex("ab" * 32)
        self.mock_w3.eth.send_raw_transaction.return_value = expected_hash_bytes

        result = _relay_direct(self.mock_w3, self.raw_tx)

        self.assertTrue(result.success)
        self.assertEqual(result.method, "direct")
        # tx_hash should be the hex representation
        self.assertEqual(result.tx_hash, expected_hash_bytes.hex())

    def test_direct_relay_success_hex_string(self):
        """Test successful direct relay when RPC returns hex string."""
        expected_hash = "0x" + "ab" * 32
        self.mock_w3.eth.send_raw_transaction.return_value = expected_hash

        result = _relay_direct(self.mock_w3, self.raw_tx)

        self.assertTrue(result.success)
        self.assertEqual(result.method, "direct")
        self.assertEqual(result.tx_hash, expected_hash)

    def test_direct_relay_failure(self):
        """Test failed direct relay submission."""
        self.mock_w3.eth.send_raw_transaction.side_effect = Exception("RPC error")

        result = _relay_direct(self.mock_w3, self.raw_tx)

        self.assertFalse(result.success)
        self.assertEqual(result.method, "direct")
        self.assertEqual(result.tx_hash, "")
        self.assertIn("RPC error", result.error)


if __name__ == "__main__":
    unittest.main()
