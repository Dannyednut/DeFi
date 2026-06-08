"""
optimizer/tick_walk.py
======================
Exact tick-aware optimal arbitrage amount for two V3 pools.

Direct port of the algorithm from:
  https://github.com/t4sk/notes/blob/main/uni-v3/arb.py
  (as described in "Algorithm to Arbitrage Uniswap V3 Pools" by Smart Contract
   Programmer / Cyfrin Updraft)

The algorithm finds the exact profit-maximising input amount (dya) by walking
the tick ranges of both pools simultaneously and computing the closed-form
optimum within each overlapping tick segment.

Pool A: the pool you sell INTO  (price of tokenIn is lower here)
Pool B: the pool you buy FROM   (price of tokenIn is higher here)

Both pools must trade the same token pair.
Input/output:
  dya → pool A → dx → pool B → dyb
  dya: amount of tokenY (quote token) sent into pool A
  dx:  amount of tokenX (base token) received from pool A / sent to pool B
  dyb: amount of tokenY received from pool B

segments format: [(sqrt_price_lo, sqrt_price_hi, liquidity)] sorted ascending
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple

from .tick_data import TickSegment
from log import get_logger

log = get_logger("optimizer.tick_walk")

MIN_DY_DELTA = 1.0   # minimum meaningful profit delta (in float units)


# ── AMM math (mirrors the Python reference implementation) ───────────────────

def _calc_x(L: float, s: float) -> float:
    """Virtual token0 reserve: L / sqrtPrice."""
    assert s >= 0
    return math.inf if s == 0 else L / s


def _calc_y(L: float, s: float) -> float:
    """Virtual token1 reserve: L * sqrtPrice."""
    assert s >= 0
    return L * s


def _calc_dx(L: float, s_lo: float, s_hi: float) -> float:
    """token0 available between s_lo and s_hi."""
    if s_lo >= s_hi:
        return 0.0
    if s_lo == 0:
        return math.inf
    return L * (1 / s_lo - 1 / s_hi)


def _calc_dy(L: float, s_lo: float, s_hi: float) -> float:
    """token1 available between s_lo and s_hi."""
    if s_lo >= s_hi:
        return 0.0
    return L * (s_hi - s_lo)


def _calc_dx_to_s_lo(L: float, s_hi: float, dx: float) -> float:
    return 1 / (dx / L + 1 / s_hi)


def _calc_dy_to_s_hi(L: float, s_lo: float, dy: float) -> float:
    return dy / L + s_lo


def _calc_dx_to_s_lo_from_dy(L: float, s_hi: float, dy: float) -> float:
    return -dy / L + s_hi


def _calc_amt_out(i: float, o: float, di: float, f: float) -> float:
    """Constant-product amountOut given reserves i, o, input di, fee f."""
    di_net = di * (1 - f)
    return o * di_net / (i + di_net)


# ── Closed-form optimum within a single overlapping tick segment ──────────────

def _calc_opt_dy_in(
    xa: float, ya: float,
    xb: float, yb: float,
    fa: float, fb: float,
) -> float:
    """
    Closed-form optimal dya to input into pool A given virtual reserves.

    Derivation: maximise dyb - dya where:
      dx  = getAmountOut(ya, xa, dya, fa)
      dyb = getAmountOut(xb, yb, dx,  fb)
    Set d(dyb-dya)/d(dya) = 0 → quadratic in dya.
    """
    k0 = xa * ya * xb * yb * (1 - fa) * (1 - fb)
    k1 = (xb + xa * (1 - fb)) * (1 - fa)

    a = k1 * k1
    b = 2 * k1 * xb * ya
    c = (xb * ya) ** 2 - k0

    disc = b * b - 4 * a * c
    if disc < 0:
        return 0.0
    return (-b + math.sqrt(disc)) / (2 * a)


def _calc_opt_dya_segment(
    la: float, sa: float,
    lb: float, sb: float,
    fa: float, fb: float,
) -> tuple[float, float, float, float]:
    """
    Compute optimal dya within the current tick segment (sa to next_a, sb to next_b).
    Returns (dya, dyb, sa_after_swap, sb_after_swap).
    Returns (0, 0, sa, sb) if no profitable trade in this segment.
    """
    assert 0 <= sa < sb, f"sa={sa}, sb={sb}"

    xa = _calc_x(la, sa)
    ya = _calc_y(la, sa)
    xb = _calc_x(lb, sb)
    yb = _calc_y(lb, sb)

    dya = _calc_opt_dy_in(xa, ya, xb, yb, fa, fb)
    if dya <= 0:
        return (0.0, 0.0, sa, sb)

    dx  = _calc_amt_out(ya, xa, dya, fa)
    dyb = _calc_amt_out(xb, yb, dx,  fb)

    if abs(dya - dyb) < MIN_DY_DELTA:
        return (0.0, 0.0, sa, sb)

    if dya > dyb:
        # Numerical edge case — no real profit
        return (0.0, 0.0, sa, sb)

    # New prices after the swap
    sa_swap = _calc_dy_to_s_hi(la, sa, dya * (1 - fa))
    sb_swap = _calc_dx_to_s_lo(lb, sb, dx  * (1 - fb))

    if not (sa <= sa_swap <= sb_swap <= sb):
        return (0.0, 0.0, sa, sb)

    return (dya, dyb, sa_swap, sb_swap)


def _calc_dx_to_s_lo(L: float, s_hi: float, dx: float) -> float:
    return 1 / (dx / L + 1 / s_hi)


# ── Segment exhaustion helpers ────────────────────────────────────────────────

def _swap_to_sa_hi(
    xa: float, xb: float,
    la: float, sa_lo: float, sa_hi: float,
    lb: float, sb_lo: float, sb_hi: float,
    fa: float, fb: float,
) -> tuple[float, float, float, float]:
    """Exhaust pool A's current tick range entirely."""
    dya = _calc_dy(la, sa_lo, sa_hi) / (1 - fa)
    sa  = sa_hi
    sb  = _calc_dx_to_s_lo(lb, sb_hi, xa * (1 - fb))
    assert sb_lo <= sb, f"sb_lo={sb_lo} > sb={sb}"
    dyb = _calc_dy(lb, sb, sb_hi)
    return (dya, dyb, sa, sb)


def _swap_to_sb_lo(
    xa: float, xb: float,
    la: float, sa_lo: float, sa_hi: float,
    lb: float, sb_lo: float, sb_hi: float,
    fa: float, fb: float,
) -> tuple[float, float, float, float]:
    """Exhaust pool B's current tick range entirely."""
    dx = min(xa, xb / (1 - fb))
    if dx == xa:
        sa = sa_hi
    else:
        sa = _calc_dx_to_s_lo(lb, sa_lo, dx)  # repurposed: resolves sa given dx
        sa = min(sa, sa_hi)

    dya = _calc_dy(la, sa_lo, sa) / (1 - fa)

    if dx == xa:
        sb  = _calc_dx_to_s_lo(lb, sb_hi, xa * (1 - fb))
        assert sb_lo <= sb
    else:
        sb = sb_lo

    dyb = _calc_dy(lb, sb, sb_hi)
    return (dya, dyb, sa, sb)


def _nxt_up(pool: list[TickSegment], i: int) -> TickSegment | None:
    return pool[i + 1] if i + 1 < len(pool) else None


def _nxt_down(pool: list[TickSegment], i: int) -> TickSegment | None:
    return pool[i - 1] if i - 1 >= 0 else None


# ── Main tick-walking algorithm ───────────────────────────────────────────────

class ArbResult(NamedTuple):
    dya: float        # optimal input into pool A (quote token)
    dyb: float        # output from pool B (quote token) — profit = dyb - dya
    sa:  float        # pool A sqrtPrice after arb
    sb:  float        # pool B sqrtPrice after arb
    profit: float     # dyb - dya


def calc_optimal_tick_walk(
    pool_a_segments: list[TickSegment],
    pool_b_segments: list[TickSegment],
    fa: float,
    fb: float,
) -> ArbResult:
    """
    Find the exact profit-maximising arbitrage amount between two V3 pools.

    Pool A has lower price (you sell tokenY into A, get tokenX out).
    Pool B has higher price (you sell tokenX into B, get tokenY out).
    The caller (optimal.py) is responsible for assigning A vs B correctly
    based on current prices.

    Args:
        pool_a_segments: Tick segments for pool A, sorted ascending by sqrt_price_lo.
        pool_b_segments: Tick segments for pool B, sorted ascending by sqrt_price_lo.
        fa:              Pool A fee as a fraction (e.g. 0.003 for 0.3%).
        fb:              Pool B fee as a fraction.

    Returns:
        ArbResult with optimal dya, resulting dyb, new prices, and profit.
        profit=0 means no arbitrage opportunity.
    """
    if not pool_a_segments or not pool_b_segments:
        return ArbResult(0.0, 0.0, 0.0, 0.0, 0.0)

    # Pool A: ascending segments (price moves up as we sell into it)
    # Pool B: descending segments (price moves down as we buy from it)
    # Start at the innermost (current-price) segments
    a_idx = 0
    b_idx = len(pool_b_segments) - 1

    seg_a = pool_a_segments[a_idx]
    seg_b = pool_b_segments[b_idx]

    sa_lo, sa_hi, la = seg_a.sqrt_price_lo, seg_a.sqrt_price_hi, float(seg_a.liquidity)
    sb_lo, sb_hi, lb = seg_b.sqrt_price_lo, seg_b.sqrt_price_hi, float(seg_b.liquidity)

    sa = sa_lo
    sb = sb_hi

    total_dya = 0.0
    total_dyb = 0.0

    while sa_lo < sb_hi:
        xa = _calc_dx(la, sa_lo, sa_hi)
        xb = _calc_dx(lb, sb_lo, sb_hi)

        if sa_hi <= sb_lo:
            # Non-overlapping ranges
            if xa <= xb:
                # Pool A exhausted first — consume its whole range
                (da, db, sa, sb) = _swap_to_sa_hi(
                    xa, xb, la, sa_lo, sa_hi, lb, sb_lo, sb_hi, fa, fb
                )
                total_dya += da
                total_dyb += db
            else:
                # Pool B exhausted first
                (da, db, sa, sb) = _swap_to_sb_lo(
                    xa, xb, la, sa_lo, sa_hi, lb, sb_lo, sb_hi, fa, fb
                )
                total_dya += da
                total_dyb += db
        else:
            # Overlapping ranges — check closed-form optimum
            (dya_opt, dyb_opt, sa_swap, sb_swap) = _calc_opt_dya_segment(
                la, sa, lb, sb, fa, fb
            )

            if dya_opt == 0:
                break

            if sa_hi < sa_swap:
                # Optimum beyond pool A's tick boundary — exhaust A
                (da, db, sa, sb) = _swap_to_sa_hi(
                    xa, xb, la, sa_lo, sa_hi, lb, sb_lo, sb_hi, fa, fb
                )
                total_dya += da
                total_dyb += db
            elif sb_swap < sb_lo:
                # Optimum beyond pool B's tick boundary — exhaust B
                (da, db, sa, sb) = _swap_to_sb_lo(
                    xa, xb, la, sa_lo, sa_hi, lb, sb_lo, sb_hi, fa, fb
                )
                total_dya += da
                total_dyb += db
            else:
                # Optimum lands within this segment — done
                total_dya += dya_opt
                total_dyb += dyb_opt
                sa = sa_swap
                sb = sb_swap
                break

        assert sa <= sb, f"sa={sa} > sb={sb}"

        # Advance to next tick segment
        if sa == sa_hi:
            nxt = _nxt_up(pool_a_segments, a_idx)
            if nxt is None:
                break
            a_idx += 1
            seg_a = nxt
            sa_lo, sa_hi, la = seg_a.sqrt_price_lo, seg_a.sqrt_price_hi, float(seg_a.liquidity)
        else:
            sa_lo = sa

        if sb == sb_lo:
            nxt = _nxt_down(pool_b_segments, b_idx)
            if nxt is None:
                break
            b_idx -= 1
            seg_b = nxt
            sb_lo, sb_hi, lb = seg_b.sqrt_price_lo, seg_b.sqrt_price_hi, float(seg_b.liquidity)
        else:
            sb_hi = sb

    profit = total_dyb - total_dya
    if profit <= 0:
        return ArbResult(0.0, 0.0, sa, sb, 0.0)

    log.debug(f"tick_walk: dya={total_dya:.4f} dyb={total_dyb:.4f} profit={profit:.4f}")
    return ArbResult(total_dya, total_dyb, sa, sb, profit)
