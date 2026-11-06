"""
tests/test_discovery.py
=======================
Unit tests for registry.discover_from_swap() — mempool swap-path pool
discovery.

DSS-WO-ENG-003 (A4): rewritten for the current discovery architecture.
The old test drove a synchronous, sequential factory-getPair flow that no
longer exists: discover_from_swap is async and resolves unknown pairs in
batch through utils.multicall3.multicall3_discover_token_pairs, then
registers each candidate via registry.register_new_pool (V2 pools load
through registry._load_v2_pair). The old test also never awaited the
coroutine ("coroutine was never awaited"), so it exercised nothing.

Mock surface (all offline, deterministic):
  - utils.multicall3.multicall3_discover_token_pairs -> canned candidate
    list (the real batched RPC call is covered separately in
    tests/test_multicall3_discovery.py);
  - registry._load_v2_pair -> pre-built PoolEdge pair with realistic TVL;
  - registry.GeckoTerminal -> offline (TVL lookup raises, so the edge's
    own TVL estimate is kept).
"""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from graph import TokenGraph, PoolEdge
from mempool.decoder import DecodedSwap
from registry import discover_from_swap
from config import get_chain

TOKEN1 = "0x" + "1" * 40
TOKEN2 = "0x" + "2" * 40
POOL_ADDR = "0x" + "3" * 40


def _uniswap_v2_dex():
    dex = next(d for d in get_chain().dexes if d.name == "UniswapV2")
    return dex


def _make_swap() -> DecodedSwap:
    return DecodedSwap(
        tx_hash="0xhash", router="0xRouter", router_name="UniswapV2",
        version=2, function_name="swapExactTokensForTokens",
        token_path=[TOKEN1, TOKEN2], amount_in=1000,
        amount_out_min=900, amount_in_is_exact=True,
        sender="0xSender", gas_price=20 * 10**9,
    )


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.w3 = MagicMock()
        self.graph = TokenGraph()
        self.cache = MagicMock()
        self.dex = _uniswap_v2_dex()

    def _candidate(self):
        """Candidate dict in the exact shape returned by the real
        multicall3_discover_token_pairs."""
        return {
            "pool_address": POOL_ADDR,
            "dex": self.dex,
            "token0": TOKEN1,
            "token1": TOKEN2,
            "fee": None,
        }

    def _register_patches(self, mock_load):
        patchers = [
            patch(
                "utils.multicall3.multicall3_discover_token_pairs",
                return_value=[self._candidate()],
            ),
            patch("registry.GeckoTerminal"),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        # GeckoTerminal is unreachable offline: force the authoritative-TVL
        # lookup to fail so register_new_pool keeps the edge's own TVL.
        from registry import GeckoTerminal  # the patched symbol's class attr
        GeckoTerminal.return_value.pool_tvl_usd.side_effect = Exception("offline")
        edge_a = PoolEdge(POOL_ADDR, "UniswapV2", TOKEN1, TOKEN2, 1000, 1000, 30, 2, tvl_usd=100000)
        edge_b = PoolEdge(POOL_ADDR, "UniswapV2", TOKEN2, TOKEN1, 1000, 1000, 30, 2, tvl_usd=100000)
        mock_load.return_value = (edge_a, edge_b)

    @patch("registry._load_v2_pair")
    def test_discover_v2(self, mock_load):
        """A discovered V2 candidate is registered into the token graph."""
        self._register_patches(mock_load)

        swap = _make_swap()
        asyncio.run(discover_from_swap(self.w3, self.graph, self.cache, swap))

        # Verify the pool was registered in the graph under both directions
        edges = self.graph.get_edges_from(TOKEN1)
        self.assertTrue(
            any(e.pool_address == POOL_ADDR for e in edges),
            f"discovered pool missing from graph edges: {edges}",
        )
        reverse = self.graph.get_edges_from(TOKEN2)
        self.assertTrue(
            any(e.pool_address == POOL_ADDR for e in reverse),
            "discovered pool missing reverse direction",
        )

    @patch("registry._load_v2_pair")
    def test_discover_skips_known_pool(self, mock_load):
        """An already-known pool identity is not re-registered."""
        self._register_patches(mock_load)

        swap = _make_swap()
        asyncio.run(discover_from_swap(self.w3, self.graph, self.cache, swap))
        self.assertTrue(self.graph.has_pool(POOL_ADDR))

        # Second discovery round for the same pool must not duplicate edges
        edges_before = len(self.graph.get_edges_from(TOKEN1))
        asyncio.run(discover_from_swap(self.w3, self.graph, self.cache, swap))
        edges_after = len(self.graph.get_edges_from(TOKEN1))
        self.assertEqual(edges_before, edges_after)

    @patch("registry._load_v2_pair")
    def test_discover_ignores_empty_candidates(self, mock_load):
        """No candidates -> no graph mutation, no exception."""
        self._register_patches(mock_load)
        with patch(
            "utils.multicall3.multicall3_discover_token_pairs",
            return_value=[],
        ):
            asyncio.run(discover_from_swap(self.w3, self.graph, self.cache, _make_swap()))
        self.assertFalse(self.graph.has_pool(POOL_ADDR))
        mock_load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
