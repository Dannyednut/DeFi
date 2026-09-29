"""
Enhanced TVL Calculation Module
===========================
More accurate pool TVL estimation using multiple price sources.

TVL priority (highest → lowest):
  1. Both tokens priced → exact USD sum
  2. One token priced  → single-side × 2 (symmetric pool assumption)
  3. Neither priced    → 0.0  (cannot pass a USD threshold filter reliably)

For research/display only, use estimate_tvl_native() which returns a
WETH-or-token-denominated value instead of $0 when prices are unknown.
"""
from __future__ import annotations

import math
import requests
from typing import Optional, TYPE_CHECKING

from config import CHAIN_ID, NATIVE_PRICE_USD
from utils.gecko_terminal import GeckoTerminal

if TYPE_CHECKING:
    from graph import PoolEdge, TokenGraph
    from prices import PriceOracle

from log import get_logger

log = get_logger("tvl")


# ══════════════════════════════════════════════════════════════════════════════
# PRICE ORACLE INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

_oracle: Optional["PriceOracle"] = None
_graph_ref: Optional["TokenGraph"] = None
_FALLBACK: dict[str, float] = {}

# Token decimals overrides for non-18 decimal tokens
_DECIMALS: dict[str, int] = {
    # Ethereum Stablecoins
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,   # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,   # USDT
    "0x6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 8,   # WBTC
    
    # BSC Stablecoins
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,  # USDC-BSC (bridged)
    "0x55d398326f99059ff775485246999027b3197955": 18,  # USDT-BSC (bridged)
    "0xe9e7cea3dedca5984780bafc599bd69add087d56": 18,  # BUSD
    
    # Polygon Stablecoins
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": 6,   # USDC-Poly
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": 6,   # USDT-Poly
}

# NETWORKS: dict[int, str] = {
#     1: "eth",
#     56: "bsc",
#     137: "pos",
#     8453: "base",
#     42161: "arbitrum-one",
#     324: "zksync",
# }

# Known WETH-equivalent addresses (all lowercased) for native anchoring
_WETH_ADDRS: set[str] = set()
_NATIVE_PRICE_USD: float = 2000.0

gecko = GeckoTerminal()

def set_oracle(oracle: "PriceOracle") -> None:
    """Set the global price oracle."""
    global _oracle
    _oracle = oracle

def set_graph_ref(graph: "TokenGraph") -> None:
    """Set the global graph reference for price lookups."""
    global _graph_ref
    _graph_ref = graph



def init_prices(
    native_price_usd: float,
    wrapped_native: str,
    stablecoins: list[str],
) -> None:
    """Seed fallback prices, decimals, and WETH anchors."""
    global _NATIVE_PRICE_USD
    _NATIVE_PRICE_USD = native_price_usd

    wn = wrapped_native.lower()
    _FALLBACK[wn] = native_price_usd
    _DECIMALS[wn] = 18
    _WETH_ADDRS.add(wn)

    for addr in stablecoins:
        a = addr.lower()
        _FALLBACK[a] = 1.0
        # Only set if not already in overrides
        if a not in _DECIMALS:
            _DECIMALS[a] = 18 # Default to 18 for safely, USDT/USDC handled in static map


# def query_pool_tvl(address):
#     # Using the official public v2 API base URL

#     network = NETWORKS.get(CHAIN_ID)
#     if network is None:
#         log.warning(f"Unsupported chain ID: {CHAIN_ID}")
#         return None
#     base_url = "https://api.geckoterminal.com/api/v2/networks"
#     full_url = f"{base_url}/{network}/pools/{address}"
    
#     # A standard User-Agent header stops basic automated security blocks
#     headers = {
#         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
#     }
#     try:
#         response = requests.get(full_url, headers=headers)
        
#         if response.status_code == 200:
#             data = response.json()
#             # Extract the reserve_in_usd from the official v2 API response architecture
#             tvl = data.get("data", {}).get("attributes", {}).get("reserve_in_usd", None)
#             return tvl
#         else:
#             log.warning(f"Error fetching TVL for {address} on {network}: {response.status_code}")
#             return None
#     except Exception as e:
#         log.error(f"Exception while fetching TVL for {address} on {network}: {e}")
#         return None
    
def token_price(addr: str) -> Optional[float]:
    """Get USD price for a token. Returns None if truly unknown."""
    # 1. Check Oracle (CEX/Aggregated)
    if _oracle is not None:
        price = _oracle.get_price(addr)
        if price is not None:
            return price
            
    # 2. Check Graph Metadata (DEX-derived ETH or direct USD)
    if _graph_ref is not None:
        addr_lower = addr.lower()
        meta = _graph_ref.token_metadata.get(addr_lower)
        if meta:
            # 2a. Direct USD price from rich subgraphs
            if meta.get("usd_price"):
                return meta["usd_price"]
            
            # 2b. Derived ETH conversion
            if meta.get("derived_eth"):
                native_price = _oracle.get_native_price() if _oracle else _NATIVE_PRICE_USD
                return meta["derived_eth"] * native_price

    # 3. Fallback
    return _FALLBACK.get(addr.lower())


def token_decimals(addr: str) -> int:
    """Get token decimals (prefers Graph metadata, falls back to static map)."""
    addr_lower = addr.lower()
    
    # 1. Check Static Overrides (Trusted)
    if addr_lower in _DECIMALS:
        return _DECIMALS[addr_lower]
        
    # 2. Check Graph Metadata (Learned from Subgraph)
    if _graph_ref is not None:
        meta = _graph_ref.token_metadata.get(addr_lower)
        if meta and meta.get("decimals"):
            return int(meta["decimals"])
            
    # 3. Default
    return 18


def is_known_token(addr: str) -> bool:
    """Return True if we have a USD price for this token."""
    return token_price(addr) is not None


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED TVL CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def estimate_tvl_v2(
    token0: str,
    token1: str,
    reserve0: int,
    reserve1: int,
    decimals0: int = -1,
    decimals1: int = -1,
    pool_address: Optional[str] = None,
    dex: Optional[str] = None,
) -> float:
    """
    Estimate USD TVL for a V2 pool (SPEC 3.4).
    
    PRIMARY: Queries API via query_pool_tvl if pool_address is provided.
    FALLBACK: Uses Dynamic Base-Asset Estimation with local price lookups.
    """
    # PRIMARY: Try API first
    if pool_address is not None:
        api_tvl = gecko.pool_tvl_usd(CHAIN_ID, pool_address, dex=dex)
        if api_tvl is not None:
            return api_tvl
        api_info = gecko.pool_info(CHAIN_ID, pool_address, dex=dex)
        if api_info.get("error") == 404:
            # log.warning(f"No info found for {pool_address}: {api_info['error']} - Low liquidity or unsupported pool type.")
            return 0.0

    # FALLBACK: Local estimation
    if reserve0 == 0 or reserve1 == 0:
        return 0.0

    d0 = decimals0 if decimals0 >= 0 else token_decimals(token0)
    d1 = decimals1 if decimals1 >= 0 else token_decimals(token1)

    r0 = reserve0 / (10 ** d0)
    r1 = reserve1 / (10 ** d1)

    p0 = token_price(token0)
    p1 = token_price(token1)

    if p0 is not None and p1 is not None:
        return r0 * p0 + r1 * p1          # Exact calculation
    
    # SPEC 3.4: Base-Asset Estimation. 
    # If we only know one side's price (e.g. WETH), we assume symmetry.
    if p0 is not None:
        return r0 * p0 * 2.0
    if p1 is not None:
        return r1 * p1 * 2.0

    return 0.0


def estimate_tvl_v3(
    token0: str,
    token1: str,
    sqrt_price_x96: int,
    liquidity: int,
    decimals0: int = -1,
    decimals1: int = -1,
    pool_address: Optional[str] = None,
    dex: Optional[str] = None,
) -> float:
    """
    Estimate USD TVL for a V3 pool (SPEC 3.4).
    
    PRIMARY: Queries API via query_pool_tvl if pool_address is provided.
    FALLBACK: Uses virtual full-range reserves (liquidity / sqrtPrice) to derive TVL.
    """
    # PRIMARY: Try API first
    if pool_address is not None:
        api_tvl = gecko.pool_tvl_usd(CHAIN_ID, pool_address, dex=dex)
        if api_tvl is not None:
            return api_tvl
        api_info = gecko.pool_info(CHAIN_ID, pool_address, dex=dex)
        if api_info.get("error") == 404:
            # log.warning(f"No info found for {pool_address}: {api_info['error']} - Low liquidity or unsupported pool type.")
            return 0.0
    
    # FALLBACK: Local estimation
    if sqrt_price_x96 == 0 or liquidity == 0:
        return 0.0

    try:
        # SPEC 3.4: Base-Asset Estimation for V3
        d0 = decimals0 if decimals0 >= 0 else token_decimals(token0)
        d1 = decimals1 if decimals1 >= 0 else token_decimals(token1)
        
        p0 = token_price(token0)
        p1 = token_price(token1)

        # Virtual reserves (full-range approximation)
        # Using integer math for better precision (matches Uniswap V3 spec)
        r0_raw = (liquidity << 96) // sqrt_price_x96
        r1_raw = (liquidity * sqrt_price_x96) >> 96
        
        r0 = r0_raw / (10 ** d0)
        r1 = r1_raw / (10 ** d1)

        if p0 is not None and p1 is not None:
            return r0 * p0 + r1 * p1
        if p0 is not None:
            return r0 * p0 * 2.0
        if p1 is not None:
            return r1 * p1 * 2.0

        return 0.0

    except (OverflowError, ZeroDivisionError):
        return 0.0


def estimate_tvl_native(
    token0: str,
    token1: str,
    reserve0: int,
    reserve1: int,
    decimals0: int = -1,
    decimals1: int = -1,
) -> tuple[float, str]:
    """
    Research display helper: returns (value, unit) where unit is 'USD',
    'ETH', 'token0', or 'token1'.

    Priority:
      1. USD if any token is priced               → ('3400.0', 'USD')
      2. ETH-equiv if either token is WETH        → ('1.7', 'ETH')
      3. Raw reserve of the larger-balance token  → ('8.2e14', 'token0')

    Never returns a meaningless geometric-mean scalar.
    """
    if reserve0 == 0 and reserve1 == 0:
        return 0.0, "USD"

    d0 = decimals0 if decimals0 >= 0 else token_decimals(token0)
    d1 = decimals1 if decimals1 >= 0 else token_decimals(token1)

    r0 = reserve0 / (10 ** d0)
    r1 = reserve1 / (10 ** d1)

    p0 = token_price(token0)
    p1 = token_price(token1)

    # --- Priority 1: USD ---
    if p0 is not None and p1 is not None:
        return r0 * p0 + r1 * p1, "USD"
    if p0 is not None:
        return r0 * p0 * 2.0, "USD"
    if p1 is not None:
        return r1 * p1 * 2.0, "USD"

    # --- Priority 2: WETH anchor ---
    t0l, t1l = token0.lower(), token1.lower()
    if t0l in _WETH_ADDRS:
        return r0 * 2.0, "ETH"          # both sides assumed equal value
    if t1l in _WETH_ADDRS:
        return r1 * 2.0, "ETH"

    # --- Priority 3: raw reserve of the richer side ---
    if r0 >= r1:
        return r0, f"token0:{token0[:8]}"
    return r1, f"token1:{token1[:8]}"


def estimate_tvl_from_edge(edge: "PoolEdge") -> float:
    """Calculate USD TVL from a PoolEdge."""
    # If TVL is already set (e.g., from test data or cache), use it directly
    # if edge.tvl_usd > 0:
    #     return edge.tvl_usd
    
    if edge.version == 2:
        return estimate_tvl_v2(
            edge.token_in, edge.token_out,
            edge.reserve_in, edge.reserve_out,
            pool_address=edge.pool_address,
        )
    else:
        return estimate_tvl_v3(
            edge.token_in, edge.token_out,
            edge.sqrt_price_x96, edge.liquidity,
            pool_address=edge.pool_address,
        )


def is_above_min_liquidity(tvl: float, min_usd: float) -> bool:
    """
    Check if pool meets minimum liquidity threshold.

    A pool with tvl=0.0 (both tokens unknown) NEVER passes.
    This is intentional — unknown-priced pools cannot be reliably
    filtered by a USD threshold and are excluded from the main graph.
    They may still appear in research/discovery paths.
    """
    return tvl > min_usd


# ══════════════════════════════════════════════════════════════════════════════
# PROFIT CALCULATION UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def profit_in_best_unit(
    token_addr: str,
    profit_in_token: float,
) -> tuple[float, str]:
    """
    Convert a raw profit amount (already in human-readable token units)
    to the best available denomination.

    Returns (value, label) where label is 'USD', 'ETH', or 'tok:<addr8>'.

    Usage:
        value, unit = profit_in_best_unit(cycle.tokens[0], amount_out - amount_in)
        log.research(f"Profit: {value:.6f} {unit}")
    """
    addr = token_addr.lower()
    price = token_price(addr)

    if price is not None:
        return profit_in_token * price, "USD"

    if addr in _WETH_ADDRS:
        return profit_in_token, "ETH"

    return profit_in_token, f"tok:{token_addr[:8]}"


# ══════════════════════════════════════════════════════════════════════════════
# STABLE PAIR DETECTION
# ══════════════════════════════════════════════════════════════════════════════

STABLE_TOKEN_SETS = {
    # Ethereum
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    # BSC
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
    "0x55d398326f99059ff775485246999027b3197955",
    "0xe9e7cea3dedca5984780bafc599bd69add087d56",
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    # Polygon
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
    "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
    # Base
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
    "0x4200000000000000000000000000000000000006",
    # Arbitrum
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
}


def is_stable_pair(token0: str, token1: str) -> bool:
    t0, t1 = token0.lower(), token1.lower()
    if t0 in STABLE_TOKEN_SETS and t1 in STABLE_TOKEN_SETS:
        return True
    return sum(1 for t in [t0, t1] if t in STABLE_TOKEN_SETS) >= 1


def get_pair_category(token0: str, token1: str) -> str:
    t0, t1 = token0.lower(), token1.lower()
    if t0 in STABLE_TOKEN_SETS and t1 in STABLE_TOKEN_SETS:
        return "stable"
    wrapped = {
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "eth",
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "bnb",
        "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": "matic",
        "0x4200000000000000000000000000000000000006": "eth",
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "eth",
        "0x5aea5775959fbc2557cc8789bc1bf90a239d9a91": "eth",
    }
    if t0 in wrapped or t1 in wrapped:
        if t0 in STABLE_TOKEN_SETS or t1 in STABLE_TOKEN_SETS:
            return "main"
    return "other"
