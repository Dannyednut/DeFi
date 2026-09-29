"""
tests/test_relay.py
====================
Unit tests for executor/relay.py — transaction relay functionality.
"""
import unittest
from unittest.mock import MagicMock, patch, Mock
import sys
from types import ModuleType

from executor.relay import (
    _relay_flashbots,
    _relay_direct,
    relay_transaction,
    RelayResult,
    FLASHBOTS_CHAINS,
    FLASHBOTS_RPCS,
)


class TestRelayFlashbots(unittest.TestCase):
    """Test cases for _relay_flashbots() function."""

    def setUp(self):
        """Set up common test fixtures."""
        self.mock_w3 = MagicMock()
        self.mock_w3.eth.block_number = 12345678
        self.raw_tx = bytes.fromhex("f86c" + "00" * 44)  # Minimal valid-looking raw tx
        self.test_private_key = "0x" + "ab" * 32  # 64 hex chars
        self.test_relay_signer = "0x" + "cd" * 32  # Different key for relay signing

        # Create mock flashbots module
        self.mock_flashbot_module = MagicMock()
        self.mock_account_module = MagicMock()
        
        # Create mock signer
        self.mock_signer = MagicMock()
        self.mock_account_module.from_key.return_value = self.mock_signer
        
        # Create mock FlashbotsWeb3
        self.mock_w3_flashbots = MagicMock()
        self.mock_flashbot_module.flashbot.return_value = self.mock_w3_flashbots

    def _setup_flashbots_mocks(self):
        """Helper to set up flashbots module mocks in sys.modules."""
        # Create fake flashbots module
        fake_flashbots = ModuleType("flashbots")
        fake_flashbots.FlashbotsWeb3 = MagicMock()
        fake_flashbots.flashbot = self.mock_flashbot_module.flashbot
        
        # Create fake eth_account module
        fake_eth_account = ModuleType("eth_account")
        fake_eth_account.Account = self.mock_account_module
        
        # Patch sys.modules
        modules_patch = {
            "flashbots": fake_flashbots,
            "eth_account": fake_eth_account,
        }
        return modules_patch

    @patch("executor.relay.log")
    @patch("executor.relay._relay_direct")
    @patch("config.PRIVATE_KEY", "0x" + "ab" * 32)
    def test_flashbots_import_error_fallback(self, mock_direct_relay, mock_log):
        """Test fallback to direct relay when flashbots package not installed."""
        mock_direct_relay.return_value = RelayResult(
            success=True, tx_hash="0x1234", method="direct"
        )

        # Just remove flashbots from sys.modules to trigger ImportError
        original_flashbots = sys.modules.pop("flashbots", None)
        
        result = _relay_flashbots(
            self.mock_w3, self.raw_tx, 1, target_block=12345679, relay_signer=None
        )
        
        # Restore flashbots module if it was present
        if original_flashbots:
            sys.modules["flashbots"] = original_flashbots

        # Should fallback to direct relay
        mock_direct_relay.assert_called_once_with(self.mock_w3, self.raw_tx)
        self.assertTrue(result.success)
        self.assertEqual(result.method, "direct")

    @patch("executor.relay.log")
    @patch("executor.relay._relay_direct")
    @patch("config.PRIVATE_KEY", "0x" + "ab" * 32)
    def test_flashbots_successful_submission(self, mock_direct_relay, mock_log):
        """Test successful Flashbots bundle submission."""
        modules_patch = self._setup_flashbots_mocks()
        
        # Mock bundle result with bundle_hash
        mock_result = MagicMock()
        mock_result.bundle_hash.return_value.hex.return_value = "0x" + "ef" * 32
        self.mock_w3_flashbots.flashbots.send_bundle.return_value = mock_result

        with patch.dict("sys.modules", modules_patch):
            result = _relay_flashbots(
                self.mock_w3, self.raw_tx, 1, target_block=12345679, relay_signer=None
            )

        # Verify Flashbots was called correctly
        self.mock_account_module.from_key.assert_called_once_with(self.test_private_key)
        self.mock_flashbot_module.flashbot.assert_called_once_with(
            self.mock_w3, self.mock_signer, FLASHBOTS_RPCS[1]
        )

        # Verify bundle was sent
        expected_bundle = [{"signed_transaction": self.raw_tx}]
        self.mock_w3_flashbots.flashbots.send_bundle.assert_called_once_with(
            expected_bundle, target_block_number=12345679
        )

        # Verify result
        self.assertTrue(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertTrue(result.tx_hash.startswith("0x"))

        # Direct relay should NOT be called
        mock_direct_relay.assert_not_called()

    @patch("executor.relay.log")
    @patch("executor.relay._relay_direct")
    @patch("config.PRIVATE_KEY", "0x" + "ab" * 32)
    def test_flashbots_with_custom_relay_signer(self, mock_direct_relay, mock_log):
        """Test Flashbots submission with custom relay signer."""
        modules_patch = self._setup_flashbots_mocks()
        
        mock_result = MagicMock()
        mock_result.bundle_hash.return_value.hex.return_value = "0x" + "ef" * 32
        self.mock_w3_flashbots.flashbots.send_bundle.return_value = mock_result

        with patch.dict("sys.modules", modules_patch):
            result = _relay_flashbots(
                self.mock_w3,
                self.raw_tx,
                11155111,  # Sepolia
                target_block=12345679,
                relay_signer=self.test_relay_signer,
            )

        # Should use custom relay signer, not PRIVATE_KEY
        self.mock_account_module.from_key.assert_called_once_with(self.test_relay_signer)

        # Verify correct RPC endpoint for Sepolia
        self.mock_flashbot_module.flashbot.assert_called_once_with(
            self.mock_w3, self.mock_signer, FLASHBOTS_RPCS[11155111]
        )

        self.assertTrue(result.success)

    @patch("executor.relay.log")
    @patch("executor.relay._relay_direct")
    @patch("config.PRIVATE_KEY", "0x" + "ab" * 32)
    def test_flashbots_empty_result_fallback(self, mock_direct_relay, mock_log):
        """Test fallback to direct relay when Flashbots returns empty result."""
        modules_patch = self._setup_flashbots_mocks()
        
        # Flashbots returns None/empty result
        self.mock_w3_flashbots.flashbots.send_bundle.return_value = None

        mock_direct_relay.return_value = RelayResult(
            success=True, tx_hash="0x5678", method="direct"
        )

        with patch.dict("sys.modules", modules_patch):
            result = _relay_flashbots(
                self.mock_w3, self.raw_tx, 1, target_block=12345679, relay_signer=None
            )

        # Should fallback to direct relay when result is empty
        # Note: w3 is wrapped by flashbot(), so we use ANY for w3 comparison
        from unittest.mock import ANY
        mock_direct_relay.assert_called_once_with(ANY, self.raw_tx)
        self.assertTrue(result.success)
        self.assertEqual(result.method, "direct")

    @patch("executor.relay.log")
    @patch("executor.relay._relay_direct")
    @patch("config.PRIVATE_KEY", "0x" + "ab" * 32)
    def test_flashbots_exception_fallback(self, mock_direct_relay, mock_log):
        """Test fallback to direct relay when Flashbots raises exception."""
        modules_patch = self._setup_flashbots_mocks()
        
        # Simulate exception during Flashbots setup
        self.mock_flashbot_module.flashbot.side_effect = Exception("Connection refused")

        mock_direct_relay.return_value = RelayResult(
            success=True, tx_hash="0x9abc", method="direct"
        )

        with patch.dict("sys.modules", modules_patch):
            result = _relay_flashbots(
                self.mock_w3, self.raw_tx, 1, target_block=12345679, relay_signer=None
            )

        # Should fallback to direct relay on exception
        mock_direct_relay.assert_called_once_with(self.mock_w3, self.raw_tx)
        self.assertTrue(result.success)
        self.assertEqual(result.method, "direct")

    @patch("executor.relay.log")
    @patch("executor.relay._relay_direct")
    @patch("config.PRIVATE_KEY", "0x" + "ab" * 32)
    def test_flashbots_auto_target_block(self, mock_direct_relay, mock_log):
        """Test that target_block defaults to current block + 1 when None."""
        modules_patch = self._setup_flashbots_mocks()
        
        # Set block_number on the mock that will be used (mock_w3_flashbots)
        self.mock_w3_flashbots.eth.block_number = 12345678
        
        mock_result = MagicMock()
        mock_result.bundle_hash.return_value.hex.return_value = "0x" + "ef" * 32
        self.mock_w3_flashbots.flashbots.send_bundle.return_value = mock_result

        # target_block is None - should auto-calculate
        with patch.dict("sys.modules", modules_patch):
            result = _relay_flashbots(
                self.mock_w3, self.raw_tx, 1, target_block=None, relay_signer=None
            )

        # Should use current block + 1
        expected_block = 12345678 + 1
        self.mock_w3_flashbots.flashbots.send_bundle.assert_called_once_with(
            [{"signed_transaction": self.raw_tx}], target_block_number=expected_block
        )

    @patch("executor.relay.log")
    @patch("executor.relay._relay_direct")
    @patch("config.PRIVATE_KEY", "0x" + "ab" * 32)
    def test_flashbots_result_without_bundle_hash(self, mock_direct_relay, mock_log):
        """Test handling result without bundle_hash attribute."""
        modules_patch = self._setup_flashbots_mocks()
        
        # Result without bundle_hash attribute
        mock_result = MagicMock(spec=[])
        self.mock_w3_flashbots.flashbots.send_bundle.return_value = mock_result

        with patch.dict("sys.modules", modules_patch):
            result = _relay_flashbots(
                self.mock_w3, self.raw_tx, 1, target_block=12345679, relay_signer=None
            )

        self.assertTrue(result.success)
        self.assertEqual(result.method, "flashbots")
        self.assertEqual(result.tx_hash, "pending")

    @patch("executor.relay.log")
    @patch("executor.relay._relay_direct")
    @patch("config.PRIVATE_KEY", "0x" + "ab" * 32)
    def test_flashbots_chain_fallback_to_mainnet(self, mock_direct_relay, mock_log):
        """Test that unknown chain_id falls back to mainnet Flashbots RPC."""
        modules_patch = self._setup_flashbots_mocks()
        
        mock_result = MagicMock()
        mock_result.bundle_hash.return_value.hex.return_value = "0x" + "ef" * 32
        self.mock_w3_flashbots.flashbots.send_bundle.return_value = mock_result

        # Use unknown chain_id
        unknown_chain_id = 999999

        with patch.dict("sys.modules", modules_patch):
            result = _relay_flashbots(
                self.mock_w3, self.raw_tx, unknown_chain_id, target_block=12345679, relay_signer=None
            )

        # Should fallback to mainnet RPC
        self.mock_flashbot_module.flashbot.assert_called_once_with(
            self.mock_w3, self.mock_signer, FLASHBOTS_RPCS[1]
        )

        self.assertTrue(result.success)


class TestRelayTransaction(unittest.TestCase):
    """Test cases for relay_transaction() function."""

    def setUp(self):
        self.mock_w3 = MagicMock()
        self.raw_tx = bytes.fromhex("f86c" + "00" * 44)

    @patch("executor.relay._relay_flashbots")
    def test_relay_transaction_uses_flashbots_for_supported_chains(self, mock_flashbots):
        """Test that supported chains use Flashbots."""
        mock_flashbots.return_value = RelayResult(
            success=True, tx_hash="0xflash", method="flashbots"
        )

        for chain_id in FLASHBOTS_CHAINS:
            with self.subTest(chain_id=chain_id):
                mock_flashbots.reset_mock()
                result = relay_transaction(
                    self.mock_w3, self.raw_tx, chain_id, target_block=100, relay_signer=None
                )

                mock_flashbots.assert_called_once()
                self.assertEqual(result.method, "flashbots")

    @patch("executor.relay._relay_direct")
    def test_relay_transaction_uses_direct_for_unsupported_chains(self, mock_direct):
        """Test that unsupported chains use direct relay."""
        mock_direct.return_value = RelayResult(
            success=True, tx_hash="0xdirect", method="direct"
        )

        unsupported_chains = [56, 137, 42161, 324]  # BSC, Polygon, Arbitrum, zkSync

        for chain_id in unsupported_chains:
            with self.subTest(chain_id=chain_id):
                mock_direct.reset_mock()
                result = relay_transaction(
                    self.mock_w3, self.raw_tx, chain_id, target_block=100, relay_signer=None
                )

                mock_direct.assert_called_once()
                self.assertEqual(result.method, "direct")


class TestRelayDirect(unittest.TestCase):
    """Test cases for _relay_direct() function."""

    def setUp(self):
        self.mock_w3 = MagicMock()
        self.raw_tx = bytes.fromhex("f86c" + "00" * 44)

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
