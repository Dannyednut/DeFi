"""
optimizer/optimal.py
====================
Top-level dispatch for off-chain optimal borrow amount calculation.

Dispatch logic:
  len(pools) > 2  → golden_section via Quoter
  len(pools) == 2, both V3:
      compute virtual-reserve amount (mirrors on-chain, but with sorted pools)
      quote it via Quoter
      divergence < threshold  → return virtual-reserve amount
      divergence >= threshold → tick_walk (exact, tick-crossing aware)
  len(pools) == 2, V2/V3 or V2/V2 → virtual-reserve amount

KEY INVARIANT: pools must be sorted the same way as on-chain sortPools()
before computing virtual reserves, or the quadratic gives wrong results.
Sort rule: pools[0] = lower price of tokenIn (borrow pool).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from web3 import Web3

from .golden_section import find_optimal_amount
from .quoter import quote_exact_input
from .tick_data import get_tick_segments
from .tick_walk import calc_optimal_tick_walk
from log import get_logger

log = get_logger("optimizer.optimal")

Q96 = 2**96

DEFAULT_TICK_CROSS_THRESHOLD = 0.005  # 0.5% divergence triggers tick_walk


@dataclass
class OptimalResult:
    amount_in: int
    expected_profit: int
    method: str           # "virtual_reserve" | "tick_walk" | "golden_section"
    tick_crossed: bool


def get_optimal_amount(
    w3: Web3,
    pools: list[str],
    tokens: list[str],
    fees: list[int],
    mode: int = 0,
    *,
    tick_cross_threshold: float = DEFAULT_TICK_CROSS_THRESHOLD,
    quoter_address: str | None = None,
    max_gs_iterations: int = 40,
) -> OptimalResult:
    """
    Compute the profit-maximising borrow amount for an arbitrage path.

    pools and tokens must be pre-sorted (borrow pool first), matching the
    order that ArbExec.sortPools() will produce on-chain.

    Args:
        w3:                    Web3 instance.
        pools:                 Pool addresses, sorted (borrow pool first).
        tokens:                Token addresses [t0, t1, ..., tN], len=len(pools)+1.
        fees:                  Fee tiers per pool in pip format (e.g. 3000).
        mode:                  0 = borrow tokenIn, 1 = borrow intermediate.
        tick_cross_threshold:  Divergence fraction above which tick_walk is used.
        quoter_address:        Override QuoterV2 address.
        max_gs_iterations:     Max iterations for golden-section search.

    Returns:
        OptimalResult with amount_in and metadata.
    """
    qs_kwargs: dict = {}
    if quoter_address:
        qs_kwargs["quoter_address"] = quoter_address

    n_pools = len(pools)

    # ── 3+ hop: golden section via Quoter ────────────────────────────────────
    if n_pools > 2:
        log.debug(f"optimal: {n_pools}-hop path → golden_section")
        best_amt, best_profit = find_optimal_amount(
            w3, tokens, fees,
            max_iterations=max_gs_iterations,
            **qs_kwargs,
        )
        return OptimalResult(best_amt, best_profit, "golden_section", False)

    # ── 2-pool path ───────────────────────────────────────────────────────────
    v3_flags = [_is_v3(w3, p) for p in pools]
    both_v3  = all(v3_flags)

    # Sort pools to match on-chain sortPools() before computing reserves
    sorted_pools, sorted_tokens = _sort_pools_python(w3, pools, tokens, fees)

    vr_amount = _calc_virtual_reserve_amount(
        w3, sorted_pools, sorted_tokens, fees, mode
    )

    if not both_v3 or vr_amount == 0:
        log.debug(f"optimal: 2-pool non-V3 path → virtual_reserve amt={vr_amount}")
        return OptimalResult(vr_amount, 0, "virtual_reserve", False)

    # Both V3: detect tick crossing
    tick_crossed, divergence = _detect_tick_crossing(
        w3, sorted_tokens, fees, vr_amount, tick_cross_threshold, **qs_kwargs
    )

    if not tick_crossed:
        log.debug(f"optimal: V3/V3 no tick crossing (div={divergence:.4%}) → amt={vr_amount}")
        return OptimalResult(vr_amount, 0, "virtual_reserve", False)

    log.debug(f"optimal: V3/V3 tick crossing (div={divergence:.4%}) → tick_walk")
    tw = _run_tick_walk(w3, sorted_pools, sorted_tokens, fees)

    if tw.amount_in == 0:
        log.debug("optimal: tick_walk returned 0 → golden_section fallback")
        best_amt, best_profit = find_optimal_amount(
            w3, sorted_tokens, fees,
            max_iterations=max_gs_iterations,
            **qs_kwargs,
        )
        return OptimalResult(best_amt, best_profit, "golden_section", True)

    return tw


# ── Pool sorting (mirrors on-chain sortPools) ─────────────────────────────────

def _sort_pools_python(
    w3: Web3,
    pools: list[str],
    tokens: list[str],
    fees: list[int],
) -> tuple[list[str], list[str]]:
    """
    Sort two pools so pools[0] is the borrow pool (lower price of tokenIn).
    Mirrors the on-chain sortPools() logic.

    Price comparison: price of tokenIn expressed as tokenOut/tokenIn.
    Lower price = cheaper tokenIn = borrow here (we sell tokenIn for less,
    then unwind at higher price in pools[1]).
    """
    if len(pools) != 2:
        return pools, tokens

    try:
        price0 = _get_pool_price(w3, pools[0], tokens[0], fees[0])
        price1 = _get_pool_price(w3, pools[1], tokens[0], fees[0])

        # pools[0] should be the LOWER price pool (borrow pool)
        # If price0 > price1, swap
        if price0 < price1:
            sorted_pools  = [pools[1], pools[0]]
            sorted_tokens = [tokens[0], tokens[1], tokens[2]]  # tokens unchanged (triangular)
        else:
            sorted_pools  = list(pools)
            sorted_tokens = list(tokens)

        return sorted_pools, sorted_tokens

    except Exception as e:
        log.debug(f"_sort_pools_python failed: {e}, using original order")
        return list(pools), list(tokens)


def _get_pool_price(w3: Web3, pool: str, token_in: str, fee: int) -> float:
    """
    Get the marginal price of token_in in the given pool.
    Returns tokenOut/tokenIn as a float (higher = tokenIn is more expensive here).
    Uses virtual reserves: price = resOut / resIn.
    """
    pool     = Web3.to_checksum_address(pool)
    token_in = Web3.to_checksum_address(token_in)

    res_in, res_out = _get_ordered_reserves(w3, pool, token_in)
    if res_in == 0:
        return 0.0
    return res_out / res_in


# ── Virtual reserve calculation ───────────────────────────────────────────────

def _calc_virtual_reserve_amount(
    w3: Web3,
    pools: list[str],
    tokens: list[str],
    fees: list[int],
    mode: int,
) -> int:
    """
    Off-chain mirror of Helper.calcOptimalBorrow.
    pools must already be sorted (borrow pool first).
    tokens[0] = tokenIn (borrow token), tokens[1] = intermediate.
    """
    try:
        res_in_p0, res_out_p0 = _get_ordered_reserves(w3, pools[0], tokens[0])
        res_in_p1, res_out_p1 = _get_ordered_reserves(w3, pools[1], tokens[1])
        return _calc_optimal_v2_borrow(
            res_in_p0, res_out_p0,
            res_in_p1, res_out_p1,
            fees[0], fees[1],
            mode,
        )
    except Exception as e:
        log.debug(f"_calc_virtual_reserve_amount failed: {e}")
        return 0


def _get_ordered_reserves(w3: Web3, pool: str, token_in: str) -> tuple[int, int]:
    """Return (resIn, resOut) ordered by trade direction."""
    pool     = Web3.to_checksum_address(pool)
    token_in = Web3.to_checksum_address(token_in)

    if _is_v3(w3, pool):
        slot0_abi = UNISWAP_V3_SLOT0_ABI_MIN
        liq_abi   = [{"name": "liquidity", "type": "function", "stateMutability": "view",
                      "inputs": [], "outputs": [{"name": "", "type": "uint128"}]}]
        t0_abi    = [{"name": "token0", "type": "function", "stateMutability": "view",
                      "inputs": [], "outputs": [{"name": "", "type": "address"}]}]

        sqrt_price_x96 = w3.eth.contract(address=pool, abi=slot0_abi).functions.slot0().call()[0]
        L              = w3.eth.contract(address=pool, abi=liq_abi).functions.liquidity().call()
        token0         = Web3.to_checksum_address(
            w3.eth.contract(address=pool, abi=t0_abi).functions.token0().call()
        )

        r0 = (L << 96) // sqrt_price_x96 if sqrt_price_x96 else 0
        r1 = (L * sqrt_price_x96) >> 96
    else:
        v2_abi = [
            {"name": "getReserves", "type": "function", "stateMutability": "view",
             "inputs": [], "outputs": [
                 {"name": "reserve0", "type": "uint112"},
                 {"name": "reserve1", "type": "uint112"},
                 {"name": "blockTimestampLast", "type": "uint32"},
             ]},
            {"name": "token0", "type": "function", "stateMutability": "view",
             "inputs": [], "outputs": [{"name": "", "type": "address"}]},
        ]
        p2     = w3.eth.contract(address=pool, abi=v2_abi)
        res    = p2.functions.getReserves().call()
        r0, r1 = res[0], res[1]
        token0 = Web3.to_checksum_address(p2.functions.token0().call())

    return (r0, r1) if token_in == token0 else (r1, r0)


def _calc_optimal_v2_borrow(
    res_in_p0: int, res_out_p0: int,
    res_in_p1: int, res_out_p1: int,
    fee_p0: int, fee_p1: int,
    mode: int,
) -> int:
    """Python mirror of Solidity calcOptimalV2Borrow with per-pool fees."""
    g0 = (1_000_000 - fee_p0) / 1_000_000
    g1 = (1_000_000 - fee_p1) / 1_000_000
    
    a1, b1 = float(res_in_p0),  float(res_out_p0)
    a2, b2 = float(res_in_p1),  float(res_out_p1)
    
    k  = a2 * g1 + b1 * g0 * g1
    qa = k * k
    qb = 2 * k * a1 * a2
    qc = (a1 * a2)**2 - b1 * a1 * a2 * b2 * g0 * g1
    
    disc = qb*qb - 4*qa*qc
    if disc < 0: return 0
    
    sqrt_disc = math.sqrt(disc)
    x1 = (-qb + sqrt_disc) / (2 * qa)
    x2 = (-qb - sqrt_disc) / (2 * qa)
    x = x1 if (0 < x1 < b2) else x2
    if x <= 0 or x >= b2: return 0
    
    amount = x
    if mode == 1:
        amount = (amount * g0 * b1) / (a1 + amount * g0)
    return int(amount)


# ── Tick-crossing detection ───────────────────────────────────────────────────

def _detect_tick_crossing(
    w3: Web3,
    tokens: list[str],
    fees: list[int],
    vr_amount: int,
    threshold: float,
    **quoter_kwargs,
) -> tuple[bool, float]:
    """
    Quote vr_amount through the path and compare output to expected.
    If output/input ratio deviates from the virtual-reserve prediction by
    more than threshold, a tick crossing occurred.
    """
    if vr_amount <= 0:
        return False, 0.0

    quoter_out = quote_exact_input(w3, tokens, fees, vr_amount, **quoter_kwargs)
    if quoter_out is None:
        return False, 0.0

    divergence = abs(1.0 - quoter_out / vr_amount)
    return divergence > threshold, divergence


# ── Tick walk orchestration ───────────────────────────────────────────────────

def _run_tick_walk(
    w3: Web3,
    pools: list[str],
    tokens: list[str],
    fees: list[int],
) -> OptimalResult:
    """
    Fetch oriented tick segments for both pools and run calc_optimal_tick_walk.

    pools[0] = borrow pool (lower price, we sell tokens[0] into it)
    pools[1] = unwind pool (higher price, we sell tokens[1] into it)
    tokens[0] = tokenIn (quote token = y in the algorithm)
    tokens[1] = intermediate (base token = x in the algorithm)
    """
    try:
        # Pool A: sell tokens[0] (tokenIn) into pools[0]
        # Pool B: sell tokens[1] (intermediate) into pools[1]
        segs_a = get_tick_segments(w3, pools[0], fees[0], token_in=tokens[0])
        segs_b = get_tick_segments(w3, pools[1], fees[1], token_in=tokens[1])
        print(f"  tick_walk segs_a={len(segs_a)}, segs_b={len(segs_b)}")
        if not segs_a or not segs_b:
            log.debug("tick_walk: empty segments")
            return OptimalResult(0, 0, "tick_walk", True)

        fa = fees[0] / 1_000_000
        fb = fees[1] / 1_000_000

        result = calc_optimal_tick_walk(segs_a, segs_b, fa, fb)

        if result.profit <= 0:
            return OptimalResult(0, 0, "tick_walk", True)

        # dya is in raw token units of tokens[0] (y=token_in coordinate)
        # int() truncates float precision — safe since we're well above wei precision
        amount_in = int(result.dya)
        profit    = int(result.profit)

        log.debug(f"tick_walk: amount_in={amount_in} profit={profit}")
        return OptimalResult(amount_in, profit, "tick_walk", True)

    except Exception as e:
        log.warning(f"_run_tick_walk failed: {e}")
        return OptimalResult(0, 0, "tick_walk", True)


# ── Pool type detection (cached) ─────────────────────────────────────────────

_V3_CACHE: dict[str, bool] = {}

UNISWAP_V3_SLOT0_ABI_MIN = [
    {"name": "slot0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [
         {"name": "sqrtPriceX96", "type": "uint160"},
         {"name": "tick",         "type": "int24"},
         {"name": "x", "type": "uint16"}, {"name": "y", "type": "uint16"},
         {"name": "z", "type": "uint16"},
         {"name": "feeProtocol",  "type": "uint8"},
         {"name": "unlocked",     "type": "bool"},
     ]},
]


def _is_v3(w3: Web3, pool: str) -> bool:
    pool = Web3.to_checksum_address(pool)
    if pool not in _V3_CACHE:
        try:
            w3.eth.contract(
                address=pool, abi=UNISWAP_V3_SLOT0_ABI_MIN
            ).functions.slot0().call()
            _V3_CACHE[pool] = True
        except Exception:
            _V3_CACHE[pool] = False
    return _V3_CACHE[pool]


def _get_amount_out(w3: Web3, amount_in: int, token_in: str, pool: str, fee: int) -> int:
    res_in, res_out = _get_ordered_reserves(w3, pool, token_in)
    g = (1_000_000 - fee) / 1_000_000
    a, b = float(res_in),  float(res_out)
    amount_out = (amount_in * g * b) / (a + amount_in * g)
    return amount_out

# ── Quadratic solver + scaling ────────────────────────────────────────────────

def _solve_quadratic(a: int, b: int, c: int) -> tuple[int, int]:
    disc = b * b - 4 * a * c
    if disc <= 0:
        return (0, 0)
    sqrt_disc = int(math.isqrt(disc * 10**6) / 10**3)
    if 2 * a == 0:
        return (0, 0)
    x1 = (-b + sqrt_disc) // (2 * a)
    x2 = (-b - sqrt_disc) // (2 * a)
    return (x1, x2)


def _scaling_factor(min_val: int) -> int:
    thresholds = [
        (10**24, 10**20), (10**23, 10**19), (10**22, 10**18),
        (10**21, 10**17), (10**20, 10**16), (10**19, 10**15),
        (10**18, 10**14), (10**17, 10**13), (10**16, 10**12),
        (10**15, 10**11),
    ]
    for threshold, factor in thresholds:
        if min_val > threshold:
            return factor
    return 10**10