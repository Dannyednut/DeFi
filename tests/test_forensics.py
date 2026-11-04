import unittest
from unittest.mock import MagicMock, patch
from mempool.outcome import OutcomeResolver
from mempool.simulator import PendingOpportunity
from decimal import Decimal

class TestOutcomeResolverForensics(unittest.TestCase):
    def setUp(self):
        self.w3 = MagicMock()
        self.graph = MagicMock()
        self.resolver = OutcomeResolver(self.w3, self.graph)

    @patch('time.time', return_value=1234567890.0)
    def test_fetch_forensics_mined(self, mock_time):
        # Setup mock tx and receipt
        tx_hash = "0x" + "a"*64
        self.w3.eth.get_transaction.return_value = {
            "from": "0xEOA",
            "to": "0xContract",
            "maxPriorityFeePerGas": 2 * 10**9, # 2 Gwei
            "maxFeePerGas": 50 * 10**9,
            "gasPrice": 40 * 10**9
        }
        self.w3.eth.get_transaction_receipt.return_value = {
            "gasUsed": 100000,
            "effectiveGasPrice": 40 * 10**9
        }
        self.w3.eth.get_block.return_value = {
            "baseFeePerGas": 30 * 10**9
        }

        # Create a pending opportunity
        opp = PendingOpportunity(
            trigger_tx_hash=tx_hash,
            trigger_router="0xRouter",
            trigger_dex="UniswapV2",
            pending_seen_at=1234567000.0,
            affected_pool="0xPool",
            token_in="0xTokenA",
            token_out="0xTokenB",
            simulated_amount_in=1000,
            reserve_in_before=10000,
            reserve_out_before=10000,
            reserve_in_after=11000,
            reserve_out_after=9091,
            price_impact_pct=10.0,
            opp_type="DEX-Spread",
            arb_tokens=["0xTokenA", "0xTokenB"],
            arb_pools=["0xPool1", "0xPool2"],
            arb_dexes=["UniswapV2", "SushiswapV2"],
            estimated_profit_ratio=1.01,
            estimated_profit_usd=20.0
        )
        opp.mined_block = 123
        opp.tx_outcome = "mined"

        self.resolver._fetch_forensics(opp)

        self.assertEqual(opp.winner_eoa, "0xeoa")
        self.assertEqual(opp.winner_contract, "0xcontract")
        self.assertEqual(opp.gas_used, 100000)
        # Tip calculation (EIP-1559 in the test case)
        # min(maxPriority, maxFee - baseFee) = min(2, 50 - 30) = 2 Gwei
        self.assertAlmostEqual(opp.tip_gwei, 2.0)
        # total_fee = 100000 * 40 Gwei = 0.004 ETH
        self.assertAlmostEqual(opp.total_fee_eth, 0.004)

if __name__ == "__main__":
    unittest.main()
