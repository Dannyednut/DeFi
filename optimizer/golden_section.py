"""
optimizer/golden_section.py
============================
Golden-section search for the profit-maximising input amount across any
multi-hop path (2-pool or N-pool, V2/V3/mixed).

Why golden-section over binary search:
  - Binary search requires a monotone function (finds zero-crossing).
  - Golden-section finds the maximum of a unimodal function.
  - profit(x) = quoteExactInput(x) - x is concave/unimodal for any chain
    of AMM pools (composition of concave swap functions), making golden-
    section the correct algorithm here.

Convergence: ~35 iterations gives 1e-10 relative precision.
Each iteration costs exactly 1 Quoter call (not 2 like ternary search).
"""
from __future__ import annotations

from web3 import Web3

from .quoter import quote_profit
from log import get_logger

log = get_logger("optimizer.golden_section")

# Golden ratio conjugate: φ⁻¹ = (√5 - 1) / 2 ≈ 0.618
_PHI_INV = 0.6180339887498949


def find_optimal_amount(
    w3: Web3,
    tokens: list[str],
    fees: list[int],
    *,
    low: int | None = None,
    high: int | None = None,
    max_iterations: int = 40,
    min_interval: int = 1_000,          # stop when (high-low) < this (wei)
    quoter_address: str | None = None,
) -> tuple[int, int]:
    """
    Find the input amount that maximises profit over the given path.

    The bracket [low, high] defines the search space. If not supplied,
    an adaptive bracket is estimated from 3 probe quotes (see _bracket).

    Args:
        w3:             Web3 instance.
        tokens:         Token addresses in hop order.
        fees:           Fee tiers per hop (pip format).
        low:            Lower bound of search (wei). Auto-estimated if None.
        high:           Upper bound of search (wei). Auto-estimated if None.
        max_iterations: Max Quoter calls (each iteration = 1 call).
        min_interval:   Stop early when bracket width falls below this (wei).
        quoter_address: Override QuoterV2 address (defaults to mainnet).

    Returns:
        (best_amount, best_profit) — both in base units of tokens[0].
        Returns (0, 0) if no profitable amount is found.
    """
    kwargs = {}
    if quoter_address:
        kwargs["quoter_address"] = quoter_address

    # ── Step 1: establish bracket ────────────────────────────────────────────
    if low is None or high is None:
        low, high, seed_best_amt, seed_best_profit = _bracket(w3, tokens, fees, **kwargs)
        if low is None:
            log.debug("golden_section: no profitable bracket found")
            return (0, 0)
    else:
        seed_best_amt    = low
        seed_best_profit = 0

    # ── Step 2: initialise the two interior points ───────────────────────────
    # c1 is at the φ⁻¹ fraction from the right (larger point)
    # c2 is at the φ⁻¹ fraction from the left  (smaller point)
    # Invariant: low < c2 < c1 < high
    span = high - low
    c1 = low + int(span * _PHI_INV)          # ≈ 0.618 from low
    c2 = low + int(span * (1 - _PHI_INV))    # ≈ 0.382 from low

    f1 = quote_profit(w3, tokens, fees, c1, **kwargs)
    f2 = quote_profit(w3, tokens, fees, c2, **kwargs)

    best_amt    = seed_best_amt
    best_profit = seed_best_profit

    def _update_best(amt: int, profit: int) -> None:
        nonlocal best_amt, best_profit
        if profit > best_profit:
            best_profit = profit
            best_amt    = amt

    _update_best(c1, f1)
    _update_best(c2, f2)

    # ── Step 3: iterate ──────────────────────────────────────────────────────
    for i in range(max_iterations):
        if high - low < min_interval:
            break

        if f1 > f2:
            # Peak is in [c2, high] — discard [low, c2)
            low = c2
            c2, f2 = c1, f1
            span = high - low
            c1   = low + int(span * _PHI_INV)
            f1   = quote_profit(w3, tokens, fees, c1, **kwargs)
            _update_best(c1, f1)
        else:
            # Peak is in [low, c1] — discard (c1, high]
            high = c1
            c1, f1 = c2, f2
            span = high - low
            c2   = low + int(span * (1 - _PHI_INV))
            f2   = quote_profit(w3, tokens, fees, c2, **kwargs)
            _update_best(c2, f2)

        log.debug(f"  iter {i+1}: low={low} high={high} best={best_amt} profit={best_profit}")

    log.debug(f"golden_section done: best_amt={best_amt} profit={best_profit}")
    return (best_amt, best_profit)


# ── Bracket estimation ────────────────────────────────────────────────────────

# Probe amounts span ~6 orders of magnitude to catch arbs at any scale.
# These are token-agnostic; the quoter handles decimal differences.
_PROBES = [
    1 * 10**15,    # 0.001 ETH-scale
    1 * 10**17,    # 0.1  ETH-scale
    1 * 10**19,    # 10   ETH-scale
    1 * 10**21,    # 1000 ETH-scale
    1 * 10**23,    # 100k ETH-scale
]


def _bracket(
    w3: Web3,
    tokens: list[str],
    fees: list[int],
    **kwargs,
) -> tuple[int | None, int | None, int, int]:
    """
    Estimate a search bracket by sampling _PROBES and finding a profitable
    interval. Returns (low, high, best_amt, best_profit).
    Returns (None, None, 0, 0) if no probe is profitable.
    """
    profits = [(p, quote_profit(w3, tokens, fees, p, **kwargs)) for p in _PROBES]

    profitable = [(amt, profit) for amt, profit in profits if profit > 0]
    if not profitable:
        return None, None, 0, 0

    best_amt, best_profit = max(profitable, key=lambda x: x[1])

    # Bracket: one probe below and one above the best profitable probe
    best_idx = _PROBES.index(best_amt)
    low  = _PROBES[best_idx - 1] if best_idx > 0               else best_amt // 10
    high = _PROBES[best_idx + 1] if best_idx < len(_PROBES) - 1 else best_amt * 10

    return low, high, best_amt, best_profit
