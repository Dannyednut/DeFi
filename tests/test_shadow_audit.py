import unittest
from unittest.mock import MagicMock, patch
from mempool.outcome import OutcomeResolver

class TestShadowAudit(unittest.TestCase):
    def setUp(self):
        self.w3 = MagicMock()
        self.graph = MagicMock()
        self.resolver = OutcomeResolver(self.w3, self.graph)

    @patch('mempool.decoder.get_router_addresses')
    def test_shadow_detection(self, mock_get_routers):
        # 1. Setup
        block_number = 12345
        router_addr = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d".lower()
        mock_get_routers.return_value = {router_addr}
        
        # Mock public mempool check: ONLY hash1 was public, hash2 was private
        hash_public = "0x" + "1"*64
        hash_private = "0x" + "2"*64
        
        # Sample set for public mempool
        public_set = {hash_public.lower()}
        def is_public_fn(h):
            return h.lower() in public_set

        # tx3: PRIVATE, NOT a router swap (ignore)
        hash_ignore = "0x" + "3"*64
        
        # txs return just the hex bytes via .hex()
        mock_txs = [
            {"hash": MagicMock(hex=lambda: hash_public[2:]), "to": router_addr},
            {"hash": MagicMock(hex=lambda: hash_private[2:]), "to": router_addr},
            {"hash": MagicMock(hex=lambda: hash_ignore[2:]), "to": "0xSomeoneElse".lower()},
        ]
        
        self.w3.eth.get_block.return_value = {
            "transactions": mock_txs,
            "extraData": b"Flashbots"
        }
        
        # 2. Execution
        self.resolver._log_shadow = MagicMock() # Intercept log call
        self.resolver._audit_shadow_trades(block_number, [], is_public_fn)
        
        # 3. Validation
        # Should only log hash_private
        self.resolver._log_shadow.assert_called_once()
        args = self.resolver._log_shadow.call_args[0]
        self.assertEqual(args[0], hash_private)
        self.assertEqual(args[1], router_addr)
        self.assertEqual(args[2].lower(), "flashbots") 

if __name__ == "__main__":
    unittest.main()
