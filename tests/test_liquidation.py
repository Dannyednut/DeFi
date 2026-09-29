import unittest
from log import get_logger
from unittest.mock import MagicMock, patch
from detectors.specialized import LiquidationScanner

log = get_logger("test.liquidation")

class TestLiquidationScanner(unittest.TestCase):
    def setUp(self):
        self.w3 = MagicMock()
        self.graph = MagicMock()
        self.logger = MagicMock()
        # Mock Aave Pool address
        import config
        config.CHAINS[1].aave_pool = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
        
        self.scanner = LiquidationScanner(self.w3, self.graph, self.logger, 1)

    def test_detect_at_risk_position(self):
        # Mock getUserAccountData call returning HF = 1.02 (Imminent)
        # return: totalCollateral, totalDebt, availableBorrows, liqThreshold, ltv, healthFactor
        # healthFactor = 1.02 * 1e18
        mock_data = [10000, 8000, 2000, 8500, 8000, int(1.02 * 10**18)]
        
        # Mock the Aave contract method
        contract = MagicMock()
        self.scanner._aave = contract
        contract.functions.getUserAccountData.return_value.call.return_value = mock_data
        
        # Add a borrower to watch
        borrower = "0xBorrowerAddress"
        self.scanner.add_watch(borrower)
        
        # Run scan
        opps = self.scanner.on_block(123456)
        
        self.assertTrue(len(opps) > 0)
        opp = opps[0]
        from logger import OppType
        self.assertEqual(opp.opp_type, OppType.LIQUIDATION)
        self.assertEqual(opp.metadata["severity"], "IMMINENT")
        self.assertAlmostEqual(opp.metadata["health_factor"], 1.02)
        log.research(f"Liquidation alert: Borrower {borrower[:10]} | HF: {opp.metadata['health_factor']}")

if __name__ == "__main__":
    unittest.main()
