"""
tests/test_multicall3_discovery.py
===================================
Regression coverage for the batched token-pair discovery encoder path.

DSS-WO-ENG-003 (A4) production defect: utils/multicall3.py used
UNISWAP_V2_FACTORY_ABI and UNISWAP_V3_FACTORY_ABI in _encode_get_pair /
_encode_get_pool without importing them from config. Every call to
multicall3_discover_token_pairs therefore raised
NameError("name 'UNISWAP_V2_FACTORY_ABI' is not defined"), which
registry.discover_from_swap's except-branch reduced to candidates=[] —
silently disabling V2/V3 pool discovery from mempool swap paths.

These tests fail-closed check the fixed import surface and the
candidate-shape contract that registry.discover_from_swap depends on.
"""
import unittest
from unittest.mock import MagicMock, patch

from eth_abi import encode as eth_abi_encode
from web3 import Web3

from config import UNISWAP_V2_FACTORY_ABI, UNISWAP_V3_FACTORY_ABI
from utils.multicall3 import (
    _encode_get_pair,
    _encode_get_pool,
    multicall3_discover_token_pairs,
)

FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"  # UniswapV2 factory
T0 = "0x" + "1" * 40
T1 = "0x" + "2" * 40
POOL = "0x" + "3" * 40


class TestPairLookupEncoders(unittest.TestCase):
    """The encoders must build call data without NameError (regression)."""

    def test_encode_get_pair_uses_v2_factory_abi(self):
        sentinel = b"ENCODED_GET_PAIR"
        w3 = MagicMock()
        # Production receives a real Web3; mirror its checksumming behaviour.
        w3.to_checksum_address = Web3.to_checksum_address
        with patch("utils.multicall3.encode_call", return_value=sentinel) as enc:
            data = _encode_get_pair(w3, FACTORY, T0, T1)
        self.assertEqual(data, sentinel)
        contract = w3.eth.contract.call_args
        self.assertEqual(contract.kwargs["abi"], UNISWAP_V2_FACTORY_ABI)
        # getPair is invoked with checksummed, sorted-role arguments
        args = enc.call_args[0]
        self.assertEqual(args[1], "getPair")
        self.assertEqual(
            args[2],
            [Web3.to_checksum_address(T0), Web3.to_checksum_address(T1)],
        )

    def test_encode_get_pool_uses_v3_factory_abi(self):
        sentinel = b"ENCODED_GET_POOL"
        w3 = MagicMock()
        w3.to_checksum_address = Web3.to_checksum_address
        with patch("utils.multicall3.encode_call", return_value=sentinel) as enc:
            data = _encode_get_pool(w3, FACTORY, T0, T1, 3000)
        self.assertEqual(data, sentinel)
        contract = w3.eth.contract.call_args
        self.assertEqual(contract.kwargs["abi"], UNISWAP_V3_FACTORY_ABI)
        args = enc.call_args[0]
        self.assertEqual(args[1], "getPool")
        self.assertEqual(args[2][2], 3000)


class TestDiscoverTokenPairs(unittest.TestCase):
    """End-to-end candidate extraction with the Multicall3 response mocked."""

    def test_discover_returns_candidate_dicts(self):
        w3 = MagicMock()
        encoded_pool = eth_abi_encode(["address"], [Web3.to_checksum_address(POOL)])
        # One entry per constructed call; every lookup "succeeds".
        mc = w3.eth.contract.return_value
        mc.functions.aggregate3.return_value.call.return_value = [
            (True, encoded_pool)
        ] * 8  # >= number of built calls across configured V2/V3 dexes

        with patch("utils.multicall3.encode_call", return_value=b"\x00" * 4):
            found = multicall3_discover_token_pairs(w3, [(T0, T1)])

        self.assertTrue(found, "batched discovery returned no candidates")
        by_dex = {c["dex"].name: c for c in found}
        self.assertIn("UniswapV2", by_dex)
        cand = by_dex["UniswapV2"]
        self.assertEqual(cand["pool_address"], POOL)
        self.assertEqual(cand["token0"], T0)
        self.assertEqual(cand["token1"], T1)
        # Candidate dict contract consumed by registry.discover_from_swap
        self.assertTrue(hasattr(cand["dex"], "name"))
        self.assertTrue(hasattr(cand["dex"], "version"))
        self.assertTrue(hasattr(cand["dex"], "protocol"))

    def test_discover_empty_pairs_is_noop(self):
        w3 = MagicMock()
        self.assertEqual(multicall3_discover_token_pairs(w3, []), [])
        w3.eth.contract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
