"""
optimizer/
==========
Off-chain optimal borrow amount calculation for arbitrage paths.

Public API:
    from optimizer import get_optimal_amount, OptimalResult

Example:
    result = get_optimal_amount(
        w3      = w3,
        pools   = ["0xPoolA", "0xPoolB"],     # sorted: borrow pool first
        tokens  = ["0xWETH", "0xUSDC", "0xWETH"],
        fees    = [3000, 500],
        mode    = 0,
        tick_cross_threshold=0.005,           # 0.5% divergence triggers tick_walk
    )

    if result.amount_in > 0:
        # Pass result.amount_in as arb.amountIn to ArbExec.execute()
        print(f"method={result.method} amount={result.amount_in} profit={result.expected_profit}")
"""
from .optimal import get_optimal_amount, _get_amount_out, OptimalResult, DEFAULT_TICK_CROSS_THRESHOLD

__all__ = [
    "get_optimal_amount",
    "_get_amount_out",
    "OptimalResult",
    "DEFAULT_TICK_CROSS_THRESHOLD",
]
