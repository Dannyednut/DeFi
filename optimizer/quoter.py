"""
optimizer/quoter.py
===================
Thin wrapper around Uniswap QuoterV2.quoteExactInput.

Uses eth_call so no gas is consumed and no transaction is sent.
The Quoter simulates the exact swap path specified — it does NOT auto-route.
"""
from __future__ import annotations

from web3 import Web3

from .abi import QUOTER_V2_ABI, pack_path
from config import get_chain
from log import get_logger

log = get_logger("optimizer.quoter")

chain = get_chain()
quoter_v2 = chain.quoter_v3


def quote_exact_input(
    w3: Web3,
    tokens: list[str],
    fees: list[int],
    amount_in: int,
    quoter_address: str = quoter_v2,
) -> int | None:
    """
    Simulate an exact-input multi-hop swap through the specified pools.

    The path is constructed from `tokens` and `fees` — this routes through
    exactly those pools in that order, not through any auto-router.

    Args:
        w3:              Web3 instance.
        tokens:          Token addresses in hop order [t0, t1, ..., tN].
        fees:            Fee tiers per hop [f0, f1, ..., fN-1].
        amount_in:       Exact input amount in token0 base units.
        quoter_address:  QuoterV2 contract address (defaults to mainnet).

    Returns:
        amountOut as int, or None if the call reverts (e.g. no liquidity).
    """
    if amount_in <= 0:
        return None

    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(quoter_address),
        abi=QUOTER_V2_ABI,
    )
    path = pack_path(tokens, fees)

    try:
        result = quoter.functions.quoteExactInput(path, amount_in).call()
        return result[0]  # amountOut
    except Exception as e:
        log.debug(f"quoteExactInput failed for amount {amount_in}: {e}")
        return None


def quote_profit(
    w3: Web3,
    tokens: list[str],
    fees: list[int],
    amount_in: int,
    quoter_address: str = quoter_v2,
) -> int:
    """
    Returns profit = amountOut - amountIn, or 0 if not profitable / call fails.
    Convenience wrapper used by golden_section.py.
    """
    out = quote_exact_input(w3, tokens, fees, amount_in, quoter_address)
    if out is None:
        return 0
    return max(0, out - amount_in)
