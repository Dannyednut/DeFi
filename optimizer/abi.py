"""
optimizer/abi.py
================
Quoter V2 ABI and multi-hop path packing.

QuoterV2 is deployed on Ethereum mainnet at:
    0x61fFE014bA17989E743c5F6cB21bF9697530B21e

The quoteExactInput function simulates a multi-hop exact-input swap without
executing it. It returns the output amount plus per-hop metadata.

Path encoding (Uniswap V3 packed path format):
    token0 (20 bytes) | fee (3 bytes) | token1 (20 bytes) | fee (3 bytes) | token2 ...
Each hop is 23 bytes: the output token of hop N is the input token of hop N+1.
"""
from __future__ import annotations

QUOTER_V2_ABI = [
    {
        "inputs": [
            {"internalType": "bytes",   "name": "path",      "type": "bytes"},
            {"internalType": "uint256", "name": "amountIn",  "type": "uint256"},
        ],
        "name": "quoteExactInput",
        "outputs": [
            {"internalType": "uint256",   "name": "amountOut",              "type": "uint256"},
            {"internalType": "uint160[]", "name": "sqrtPriceX96AfterList",  "type": "uint160[]"},
            {"internalType": "uint32[]",  "name": "initializedTicksCrossedList", "type": "uint32[]"},
            {"internalType": "uint256",   "name": "gasEstimate",            "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn",            "type": "address"},
                    {"internalType": "address", "name": "tokenOut",           "type": "address"},
                    {"internalType": "uint256", "name": "amountIn",           "type": "uint256"},
                    {"internalType": "uint24",  "name": "fee",                "type": "uint24"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96",  "type": "uint160"},
                ],
                "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut",           "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After",   "type": "uint160"},
            {"internalType": "uint32",  "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate",         "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# ABIs reused from multicall3.py — reproduced here so optimizer is self-contained
UNISWAP_V3_TICK_BITMAP_ABI = [
    {
        "name": "tickBitmap",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "wordPosition", "type": "int16"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]

UNISWAP_V3_TICKS_ABI = [
    {
        "name": "ticks",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tick", "type": "int24"}],
        "outputs": [
            {"name": "liquidityGross",          "type": "uint128"},
            {"name": "liquidityNet",             "type": "int128"},
            {"name": "feeGrowthOutside0X128",    "type": "uint256"},
            {"name": "feeGrowthOutside1X128",    "type": "uint256"},
            {"name": "tickCumulativeOutside",    "type": "int56"},
            {"name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
            {"name": "secondsOutside",           "type": "uint32"},
            {"name": "initialized",              "type": "bool"},
        ],
    }
]

UNISWAP_V3_SLOT0_ABI = [
    {
        "name": "slot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96",                  "type": "uint160"},
            {"name": "tick",                           "type": "int24"},
            {"name": "observationIndex",               "type": "uint16"},
            {"name": "observationCardinality",         "type": "uint16"},
            {"name": "observationCardinalityNext",     "type": "uint16"},
            {"name": "feeProtocol",                    "type": "uint8"},
            {"name": "unlocked",                       "type": "bool"},
        ],
    }
]

UNISWAP_V3_LIQUIDITY_ABI = [
    {
        "name": "liquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint128"}],
    }
]

UNISWAP_V3_FEE_ABI = [
    {
        "name": "fee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint24"}],
    }
]

_TOKEN0_ABI = [{"name": "token0", "type": "function", "stateMutability": "view",
                "inputs": [], "outputs": [{"name": "", "type": "address"}]}]


def pack_path(tokens: list[str], fees: list[int]) -> bytes:
    """
    Encode a multi-hop swap path in Uniswap V3 packed format.

    Format: token0 (20B) | fee0 (3B) | token1 (20B) | fee1 (3B) | token2 (20B) ...

    Args:
        tokens: List of token addresses in hop order, length N+1.
        fees:   List of fee tiers in pip format (e.g. 3000), length N.

    Returns:
        Packed bytes path ready for quoteExactInput.

    Example:
        pack_path(["0xWETH", "0xUSDC", "0xWETH"], [3000, 500])
    """
    assert len(tokens) == len(fees) + 1, "tokens must be len(fees)+1"
    result = bytes.fromhex(tokens[0][2:].lower().zfill(40))
    for fee, token in zip(fees, tokens[1:]):
        result += fee.to_bytes(3, "big")
        result += bytes.fromhex(token[2:].lower().zfill(40))
    return result
