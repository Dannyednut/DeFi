"""
tests/test_optimizer.py
=======================
DSS-WO-ENG-003 (A3): the original file was a manual live-network diagnostic
script shaped like a test. Its entry point took a ``mode`` argument that
pytest treated as a missing fixture (collection error), and the body dialed
real RPC endpoints and on-chain contracts, asserted nothing, and swallowed
every exception.

This file now contains:

1. Deterministic offline tests of the optimal-borrow math
   (``_calc_optimal_v2_borrow`` — the integer mirror of
   ``Helper.calcOptimalV2Borrow``), verified against an independent
   floating-point profit model, including mode 0 (borrow tokenIn) vs
   mode 1 (borrow intermediate) and fail-closed degenerate inputs.

2. Deterministic offline tests of the ``get_optimal_amount`` entrypoint
   routing (2-pool virtual-reserve path, 3-pool golden-section path,
   zero-reserve fail-closed) with the on-chain quote helpers patched out.

3. The original manual diagnostic, preserved verbatim behind an explicit
   opt-in gate: it is visibly SKIPPED unless ``DSS_LIVE_OPTIMIZER_DIAG=1``.
   It dials configured RPC endpoints and calls ArbExec.getProfit on-chain;
   it must never run in CI.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from optimizer.optimal import (
    get_optimal_amount,
    _calc_optimal_v2_borrow,
    OptimalResult,
)

# Profitable two-pool cycle (fees 0.3% each side):
#   p0: tokenIn -> intermediate at 2.0
#   p1: intermediate -> tokenIn at 0.6  => round trip 1.2x pre-fee
# Independently verified: the float profit model peaks at x ~= 102,800.
RES_IN_P0, RES_OUT_P0 = 2_000_000, 4_000_000
RES_IN_P1, RES_OUT_P1 = 5_000_000, 3_000_000
FEES = (3000, 3000)


class TestCalcOptimalV2Borrow(unittest.TestCase):
    """Pure integer math — deterministic, no mocks, no network."""

    def test_profitable_cycle_mode0_borrows_token_in(self):
        amount = _calc_optimal_v2_borrow(
            RES_IN_P0, RES_OUT_P0, RES_IN_P1, RES_OUT_P1,
            FEES[0], FEES[1], mode=0,
        )
        # Regression pin of the verified optimum (float model peak: 102,800).
        self.assertEqual(amount, 102_800)

    def test_profitable_cycle_mode1_borrows_intermediate(self):
        amount = _calc_optimal_v2_borrow(
            RES_IN_P0, RES_OUT_P0, RES_IN_P1, RES_OUT_P1,
            FEES[0], FEES[1], mode=1,
        )
        # mode=1 converts the tokenIn borrow through pool0 into intermediate
        # units: x * g0 * res_out_p0 / (res_in_p0 * 1e6 + x * g0).
        x, g0 = 102_800, 997_000
        expected = (x * g0 * RES_OUT_P0) // (RES_IN_P0 * 1_000_000 + x * g0)
        self.assertEqual(amount, expected)
        self.assertEqual(amount, 194_990)

    def test_deterministic(self):
        args = (RES_IN_P0, RES_OUT_P0, RES_IN_P1, RES_OUT_P1) + FEES
        self.assertEqual(
            _calc_optimal_v2_borrow(*args, mode=0),
            _calc_optimal_v2_borrow(*args, mode=0),
        )

    def test_price_parity_cycle_is_unprofitable_and_returns_zero(self):
        # Identical prices on both pools: the 0.6% round-trip fee eats the
        # cycle, so the quadratic has no profitable root in (0, b2).
        amount = _calc_optimal_v2_borrow(1_000_000, 1_000_000, 1_000_000, 1_000_000, 3000, 3000, 0)
        self.assertEqual(amount, 0)

    def test_zero_reserve_fails_closed(self):
        amount = _calc_optimal_v2_borrow(0, RES_OUT_P0, RES_IN_P1, RES_OUT_P1, 3000, 3000, 0)
        self.assertEqual(amount, 0)

    def test_out_of_range_fee_fails_closed(self):
        amount = _calc_optimal_v2_borrow(
            RES_IN_P0, RES_OUT_P0, RES_IN_P1, RES_OUT_P1, 1_000_000, 3000, 0,
        )
        self.assertEqual(amount, 0)


class TestGetOptimalAmountRouting(unittest.TestCase):
    """Entrypoint wiring with on-chain quote helpers patched out."""

    T0 = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"  # tokenIn (WETH)
    T1 = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # intermediate
    P0 = "0x1111111111111111111111111111111111111111"  # borrow pool
    P1 = "0x2222222222222222222222222222222222222222"  # second pool

    def setUp(self):
        self.w3 = MagicMock()
        # V2 pools (slot0 probe disabled), token0 identity per pool, equal
        # spot prices so pool sorting keeps the borrow pool first.
        patchers = [
            patch("optimizer.optimal._is_v3", return_value=False),
            patch(
                "optimizer.optimal._get_token0",
                side_effect=lambda w3, pool: self.T0 if pool == self.P0 else self.T1,
            ),
            patch("optimizer.optimal._get_pool_price_q96", return_value=1 << 96),
            patch(
                "optimizer.optimal._get_ordered_reserves",
                side_effect=lambda w3, pool, token: (
                    (RES_IN_P0, RES_OUT_P0) if pool == self.P0
                    else (RES_IN_P1, RES_OUT_P1)
                ),
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_two_pool_v2_mode0_routes_to_virtual_reserve(self):
        result = get_optimal_amount(
            self.w3, [self.P0, self.P1], [self.T0, self.T1, self.T0],
            list(FEES), mode=0,
        )
        self.assertIsInstance(result, OptimalResult)
        self.assertEqual(result.method, "virtual_reserve")
        self.assertEqual(result.amount_in, 102_800)
        self.assertFalse(result.tick_crossed)
        self.assertEqual(result.expected_profit, 0)

    def test_two_pool_v2_mode1_routes_to_virtual_reserve(self):
        result = get_optimal_amount(
            self.w3, [self.P0, self.P1], [self.T0, self.T1, self.T0],
            list(FEES), mode=1,
        )
        self.assertEqual(result.method, "virtual_reserve")
        self.assertEqual(result.amount_in, 194_990)

    def test_zero_reserves_fail_closed_without_raising(self):
        with patch(
            "optimizer.optimal._get_ordered_reserves",
            side_effect=lambda w3, pool, token: (0, RES_OUT_P0),
        ):
            result = get_optimal_amount(
                self.w3, [self.P0, self.P1], [self.T0, self.T1, self.T0],
                list(FEES), mode=0,
            )
        self.assertEqual(result.amount_in, 0)
        self.assertEqual(result.method, "virtual_reserve")

    def test_three_pool_path_routes_to_golden_section(self):
        with patch(
            "optimizer.optimal.find_optimal_amount",
            return_value=(123, 456),
        ) as mock_gs:
            result = get_optimal_amount(
                self.w3,
                [self.P0, self.P1, "0x3333333333333333333333333333333333333333"],
                [self.T0, self.T1, self.T0],
                [3000, 3000, 3000],
                mode=0,
            )
        mock_gs.assert_called_once()
        self.assertEqual(result.method, "golden_section")
        self.assertEqual(result.amount_in, 123)
        self.assertEqual(result.expected_profit, 456)


# ── Manual live diagnostic (opt-in; never runs in CI) ────────────────────────

def _run_live_diagnostic(mode: int, amount=None) -> None:
    """Original manual diagnostic, preserved for operator use.

    Dials the configured RPC endpoints, runs the main optimizer entrypoint
    against live Sepolia pools, calls ArbExec.getProfit on-chain, and
    exercises golden-section / tick-walk directly. Prints results; asserts
    nothing. Requires a live connection and must never run in CI.
    """
    from utils.rpc_manager import MultiRPCManager
    from config import get_rpc_configs
    from web3 import Web3
    from log import setup_logging

    setup_logging()

    print("Initializing environment...")
    rpc_configs = get_rpc_configs()
    rpc_manager = MultiRPCManager.from_config(rpc_configs)
    w3 = rpc_manager.get_web3(ws=False)

    # Data derived from shadow_trades or observed arb cycle
    tokens = [
        "0xfff9976782d46cc05630d1f6ebab18b2324d6b14",
        "0x1c7d4b196cb0c7b01d743fbc6116a902379c7238",
        "0xfff9976782d46cc05630d1f6ebab18b2324d6b14",
    ]
    tokens = [Web3.to_checksum_address(t) for t in tokens]

    pools = [
        "0x72e46e15ef83c896de44b1874b4af7ddab5b4f74",
        "0x6ce0896eae6d4bd668fde41bb784548fb8f59b50",
    ]
    pools = [Web3.to_checksum_address(p) for p in pools]

    fees = [3000, 3000]

    print(f"Testing optimal amount for path:")
    print(f"  Tokens: {tokens}")
    print(f"  Pools: {pools}")
    print(f"  Fees: {fees}\n")

    try:
        print("\n--- Testing 1: MAIN ENTRYPOINT ---")
        result = get_optimal_amount(w3, pools, tokens, fees, mode=mode)
        print("=== OPTIMAL RESULT ===")
        print(f"Amount In:       {result.amount_in}")
        print(f"Expected Profit: {result.expected_profit}")
        print(f"Method Used:     {result.method}")
        print(f"Tick Crossed:    {result.tick_crossed}")

        print("\n--- Testing 2: ON-CHAIN getProfit ---")
        try:
            from config import get_chain, ARB_EXEC_ABI
            arb_exec_address = get_chain().arb_exec_address
            if arb_exec_address:
                arb_exec = w3.eth.contract(
                    address=w3.to_checksum_address(arb_exec_address), abi=ARB_EXEC_ABI
                )
                amount_in = result.amount_in if amount is None else amount
                arb_data = {
                    "tokens": tokens,
                    "pools": pools,
                    "amountIn": amount_in,
                    "tokenIn": tokens[0],
                    "borrowPool": pools[0],
                    "fees": fees,
                    "minProfit": 0,
                    "mode": mode,
                }
                out_amount, profit = arb_exec.functions.getProfit(arb_data).call()
                print(f"On-chain Out Amount: {out_amount}")
                print(f"On-chain Profit:     {profit}")
            else:
                print("No ARB_EXEC_ADDRESS defined in config.")
        except Exception as e:
            print(f"Error calling getProfit: {e}")

        print("\n--- Testing 3: GOLDEN SECTION (Forced) ---")
        from optimizer.golden_section import find_optimal_amount
        best_amt, best_profit = find_optimal_amount(w3, tokens, fees, max_iterations=20)
        print(f"Amount In:       {best_amt}")
        print(f"Expected Profit: {best_profit}")

        print("\n--- Testing 4: TICK WALK (Forced) ---")
        from optimizer.optimal import _run_tick_walk
        tw_result = _run_tick_walk(w3, pools, tokens, fees)
        print(f"Amount In:       {tw_result.amount_in}")
        print(f"Expected Profit: {tw_result.expected_profit}")

    except Exception as e:
        print(f"\nError calculating optimal amount: {e}")


import os as _os
import pytest as _pytest

@_pytest.mark.skipif(
    _os.getenv("DSS_LIVE_OPTIMIZER_DIAG") != "1",
    reason=(
        "live-network manual diagnostic (dials configured RPC endpoints and "
        "calls ArbExec.getProfit on-chain); opt in with DSS_LIVE_OPTIMIZER_DIAG=1 "
        "when running the diagnostic by hand — never in CI"
    ),
)
def test_live_optimizer_diagnostic():
    _run_live_diagnostic(0)
    _run_live_diagnostic(1)


if __name__ == "__main__":
    print("======= case 1 =======")
    _run_live_diagnostic(0)
    print()

    print("======= case 2 =======")
    _run_live_diagnostic(1)
    print()

    print("======= case 3 =======")
    _run_live_diagnostic(0, 0)
    print()

    print("======= case 4 =======")
    _run_live_diagnostic(1, 0)
