"""
optimizer/tick_data.py
======================
Fetch and reconstruct V3 tick segments for a pool using Multicall3.
 
ORIENTATION CONTRACT:
  tick_walk.py models: dya(tokenY) → poolA → dx(tokenX) → poolB → dyb(tokenY)
  Both pools must share the same tokenX and tokenY definition.
    tokenY = quote token = the token input/output of the full arb round-trip
    tokenX = base token  = the token flowing between the two pools
 
  V3 pools have fixed token0/token1 ordering (by address sort). We orient
  segments so that y=token_in, x=token_out for this hop, regardless of
  whether token_in is token0 or token1.
 
  Selling token1 (token_in == token1, zero_for_one=False):
    Natural V3: x=token0, y=token1, sqrtP = sqrt(y/x) ascending as trade fills.
    → natural orientation already matches algorithm. No flip needed.
 
  Selling token0 (token_in == token0, zero_for_one=True):
    sqrtP moves DOWN in natural coordinates.
    Flip: sqrtP_new = 1/sqrtP_natural → ascending in new coordinates.
    Segments [lo,hi] become [1/hi, 1/lo]. List is re-sorted ascending.
 
Two multicall rounds:
  Round 1: slot0 (single call), then liquidity + tickBitmap words (batched)
  Round 2: ticks(idx) for each initialised tick (batched)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from web3 import Web3
from eth_abi import decode as eth_abi_decode

from .abi import (
    UNISWAP_V3_SLOT0_ABI,
    UNISWAP_V3_LIQUIDITY_ABI,
    UNISWAP_V3_TICK_BITMAP_ABI,
    UNISWAP_V3_TICKS_ABI,
    _TOKEN0_ABI
)
from log import get_logger

log = get_logger("optimizer.tick_data")

Q96 = 2**96

 
FEE_TO_TICK_SPACING: dict[int, int] = {
    100: 1, 500: 10, 3000: 60, 10000: 200,
}
 
BITMAP_WORDS_EACH_SIDE = 3
 
 
@dataclass
class TickSegment:
    """
    A contiguous price range with constant liquidity, in oriented coordinates.
    sqrt_price_lo < sqrt_price_hi always.
    Units: sqrtP = sqrt(y_token / x_token) where y=token_in for this hop.
    """
    sqrt_price_lo: float
    sqrt_price_hi: float
    liquidity: int
 
 
def get_tick_segments(
    w3: Web3,
    pool_address: str,
    fee: int,
    token_in: str,
    words_each_side: int = BITMAP_WORDS_EACH_SIDE,
) -> list[TickSegment]:
    """
    Fetch tick liquidity segments for a V3 pool, oriented for token_in.
 
    Args:
        w3:             Web3 instance.
        pool_address:   V3 pool address.
        fee:            Pool fee tier in pips (e.g. 3000).
        token_in:       Token being SOLD into this pool for this hop.
        words_each_side: Bitmap words to scan either side of current tick.
 
    Returns:
        List of TickSegment sorted ascending by sqrt_price_lo, in
        the coordinate system where sqrtP = sqrt(token_in / token_out).
    """
    tick_spacing = FEE_TO_TICK_SPACING.get(fee, 60)
    pool_addr    = Web3.to_checksum_address(pool_address)
    token_in     = Web3.to_checksum_address(token_in)
    from utils.multicall3 import _get_multicall3
    mc           = _get_multicall3(w3)
 
    pool_s0   = w3.eth.contract(address=pool_addr, abi=UNISWAP_V3_SLOT0_ABI)
    pool_liq  = w3.eth.contract(address=pool_addr, abi=UNISWAP_V3_LIQUIDITY_ABI)
    pool_bmap = w3.eth.contract(address=pool_addr, abi=UNISWAP_V3_TICK_BITMAP_ABI)
 
    # Single call to get current state before building batch
    slot0_raw      = pool_s0.functions.slot0().call()
    sqrt_price_x96 = slot0_raw[0]
    current_tick   = slot0_raw[1]
 
    token0 = Web3.to_checksum_address(
        w3.eth.contract(address=pool_addr, abi=_TOKEN0_ABI).functions.token0().call()
    )
    selling_token0 = (token_in == token0)
 
    # Bitmap words around current tick
    current_word   = current_tick // (tick_spacing * 256)
    word_positions = list(range(
        current_word - words_each_side,
        current_word + words_each_side + 1,
    ))
 
    # ── Round 1: liquidity + bitmap words ────────────────────────────────────
    calls_r1: list = []
    meta_r1:  list = []
 
    calls_r1.append({
        "target": pool_addr, "allowFailure": True,
        "callData": pool_liq.encodeABI(fn_name="liquidity"),
    })
    meta_r1.append(("liquidity", None))
 
    for wp in word_positions:
        calls_r1.append({
            "target": pool_addr, "allowFailure": True,
            "callData": pool_bmap.encodeABI(fn_name="tickBitmap", args=[wp]),
        })
        meta_r1.append(("bitmap", wp))
 
    results_r1 = mc.functions.aggregate3(calls_r1).call({"value": 0})
 
    current_liquidity = 0
    bitmap_words: dict[int, int] = {}
 
    for (kind, wp), (success, data) in zip(meta_r1, results_r1):
        if not success or not data:
            continue
        if kind == "liquidity":
            current_liquidity = eth_abi_decode(["uint128"], data)[0]
        elif kind == "bitmap":
            bitmap_words[wp] = eth_abi_decode(["uint256"], data)[0]
 
    # Decode initialised ticks from bitmap words
    initialised_ticks: list[int] = []
    for word_pos, word_val in bitmap_words.items():
        if word_val == 0:
            continue
        for bit in range(256):
            if word_val & (1 << bit):
                initialised_ticks.append((word_pos * 256 + bit) * tick_spacing)
 
    if not initialised_ticks:
        log.debug(f"tick_data: no initialised ticks for {pool_address[:10]}")
        return []
 
    initialised_ticks.sort()
 
    # ── Round 2: liquidityNet per tick ────────────────────────────────────────
    pool_ticks = w3.eth.contract(address=pool_addr, abi=UNISWAP_V3_TICKS_ABI)
    calls_r2 = [
        {"target": pool_addr, "allowFailure": True,
         "callData": pool_ticks.encodeABI(fn_name="ticks", args=[t])}
        for t in initialised_ticks
    ]
    results_r2 = mc.functions.aggregate3(calls_r2).call({"value": 0})
 
    tick_liquidity_net: dict[int, int] = {}
    for tick_idx, (success, data) in zip(initialised_ticks, results_r2):
        if not success or not data:
            continue
        decoded = eth_abi_decode(
            ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"],
            data,
        )
        tick_liquidity_net[tick_idx] = decoded[1]
 
    segments = _build_segments(
        initialised_ticks, tick_liquidity_net,
        current_tick, current_liquidity,
        sqrt_price_x96, tick_spacing, selling_token0,
    )
 
    log.debug(
        f"tick_data: {len(segments)} segments | {pool_address[:10]} "
        f"fee={fee} selling_token0={selling_token0}"
    )
    return segments
 
 
def _tick_to_sqrt_price(tick: int) -> float:
    """Tick → natural sqrt price (float). sqrtP = sqrt(token1/token0)."""
    return 1.0001 ** (tick / 2)
 
 
def _build_segments(
    initialised_ticks: list[int],
    tick_liquidity_net: dict[int, int],
    current_tick: int,
    current_liquidity: int,
    sqrt_price_x96: int,
    tick_spacing: int,
    selling_token0: bool,
) -> list[TickSegment]:
    """
    Walk ticks outward from current_tick to reconstruct per-segment liquidity,
    then orient segments for token_in.
 
    Natural V3 convention (selling token1, zero_for_one=False):
      Crossing tick upward:   liquidity += liquidityNet
      Crossing tick downward: liquidity -= liquidityNet
      sqrtP = sqrt(token1/token0), ascending as token1 is sold in.
 
    If selling_token0 (zero_for_one=True):
      Flip: sqrtP_new = 1/sqrtP_natural
      Segment [lo, hi] → [1/hi, 1/lo], re-sort ascending.
    """
    ticks_below = sorted([t for t in initialised_ticks if t <= current_tick], reverse=True)
    ticks_above = sorted([t for t in initialised_ticks if t > current_tick])
 
    current_sqrt = sqrt_price_x96 / Q96
    natural_segs: list[TickSegment] = []
 
    # Walk upward — price increasing (selling token1 into pool)
    liq       = current_liquidity
    prev_sqrt = current_sqrt
    for tick in ticks_above:
        nxt_sqrt = _tick_to_sqrt_price(tick)
        if liq > 0:
            natural_segs.append(TickSegment(prev_sqrt, nxt_sqrt, liq))
        liq      += tick_liquidity_net.get(tick, 0)
        prev_sqrt = nxt_sqrt
 
    # Walk downward — price decreasing (selling token0 into pool)
    liq       = current_liquidity
    prev_sqrt = current_sqrt
    for tick in ticks_below:
        nxt_sqrt = _tick_to_sqrt_price(tick)
        liq      -= tick_liquidity_net.get(tick + tick_spacing, 0)
        if liq > 0:
            natural_segs.append(TickSegment(nxt_sqrt, prev_sqrt, liq))
        prev_sqrt = nxt_sqrt
 
    natural_segs.sort(key=lambda s: s.sqrt_price_lo)
 
    if not selling_token0:
        # Natural orientation: y=token1=token_in ✓
        return natural_segs
 
    # Flip for selling_token0: sqrtP_new = 1/sqrtP_natural
    # [lo, hi] → [1/hi, 1/lo], always positive since lo > 0
    flipped = []
    for seg in natural_segs:
        if seg.sqrt_price_lo <= 0:
            continue
        flipped.append(TickSegment(
            sqrt_price_lo=1.0 / seg.sqrt_price_hi,
            sqrt_price_hi=1.0 / seg.sqrt_price_lo,
            liquidity=seg.liquidity,
        ))
    flipped.sort(key=lambda s: s.sqrt_price_lo)
    return flipped
 