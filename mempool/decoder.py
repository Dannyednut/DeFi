"""
mempool/decoder.py — Swap calldata decoder.

Decodes pending transactions to UniswapV2/V3 routers and compatible
forks (PancakeSwap, Sushiswap, Camelot, Aerodrome, etc.).

Returns a DecodedSwap with:
  - token path (list of addresses)
  - amountIn (exact or estimated)
  - amountOutMin
  - router name
  - protocol version

Supports:
  UniV2 Router:  swapExactTokensForTokens, swapTokensForExactTokens,
                 swapExactETHForTokens, swapTokensForExactETH,
                 swapExactTokensForETH, swapETHForExactTokens
  UniV3 Router:  exactInputSingle, exactInput, exactOutputSingle, exactOutput
  UniV3 SwapRouter02: same + multicall wrapping
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from eth_abi import decode as abi_decode

from log import get_logger
log = get_logger("decoder")

# ─── Function selectors ───────────────────────────────────────────────────────
# keccak256(signature)[:4] — precomputed

V2_SELECTORS: dict[str, str] = {
    "0x38ed1739": "swapExactTokensForTokens",
    "0x8803dbee": "swapTokensForExactTokens",
    "0x7ff36ab5": "swapExactETHForTokens",
    "0x4a25d94a": "swapTokensForExactETH",
    "0x18cbafe5": "swapExactTokensForETH",
    "0xfb3bdb41": "swapETHForExactTokens",
    # Fee-on-transfer variants
    "0x5c11d795": "swapExactTokensForTokensSupportingFeeOnTransferTokens",
    "0xb6f9de95": "swapExactETHForTokensSupportingFeeOnTransferTokens",
    "0x791ac947": "swapExactTokensForETHSupportingFeeOnTransferTokens",
}

V3_SELECTORS: dict[str, str] = {
    "0x414bf389": "exactInputSingle",
    "0xc04b8d59": "exactInput",
    "0xdb3e2198": "exactOutputSingle",
    "0xf28c0498": "exactOutput",
    # SwapRouter02
    "0x04e45aaf": "exactInputSingle",   # SwapRouter02 variant
    "0xb858183f": "exactInput",
    "0x5023b4df": "exactOutputSingle",
    "0x09b81346": "exactOutput",
    # multicall
    "0xac9650d8": "multicall",
    "0x5ae401dc": "multicall",
}

# ─── V3 path decoding ─────────────────────────────────────────────────────────

def decode_v3_path(path_bytes: bytes) -> tuple[list[str], list[int]]:
    """
    Decode a UniV3 packed path: address(20) + fee(3) + address(20) + ...
    Returns (token_addresses, fees).
    """
    tokens = []
    fees = []
    if len(path_bytes) < 20:
        return tokens, fees
    i = 0
    # First token
    tokens.append("0x" + path_bytes[i:i+20].hex())
    i += 20
    while i + 23 <= len(path_bytes):
        fee = int.from_bytes(path_bytes[i:i+3], "big")
        fees.append(fee)
        i += 3
        tokens.append("0x" + path_bytes[i:i+20].hex())
        i += 20
    return [t.lower() for t in tokens], fees


# ─── Decoded swap result ──────────────────────────────────────────────────────

@dataclass
class DecodedSwap:
    tx_hash:        str
    router:         str          # router address (lowercase)
    router_name:    str          # e.g. "UniswapV2", "PancakeV2"
    version:        int          # 2 or 3
    function_name:  str
    token_path:     list[str]    # [tokenIn, ..., tokenOut] (lowercase)
    amount_in:      int          # exact if swapExact*, estimated otherwise
    amount_out_min: int          # minimum acceptable out (slippage bound)
    amount_in_is_exact: bool     # True = exact input swap
    sender:         str          # tx.from
    gas_price:      int
    max_fee:        Optional[int] = None
    max_priority:   Optional[int] = None
    fee:        list[int] = field(default_factory=list)
    raw_tx:         dict = field(default_factory=dict)

    @property
    def token_in(self) -> str:
        return self.token_path[0] if self.token_path else ""

    @property
    def token_out(self) -> str:
        return self.token_path[-1] if self.token_path else ""

    @property
    def hop_count(self) -> int:
        return len(self.token_path) - 1


# ─── Decoder ─────────────────────────────────────────────────────────────────

class SwapDecoder:
    """
    Decodes raw transaction dicts into DecodedSwap objects.

    router_map: address (lowercase) → (router_name, version)
    Built from config chain DEX registry — see build_router_map().
    """

    def __init__(self, router_map: dict[str, tuple[str, int]]):
        self._routers = router_map  # addr → (name, version)

    def decode(self, tx: dict) -> Optional[DecodedSwap]:
        """Returns None if tx is not a decodable DEX swap."""
        try:
            to      = (tx.get("to") or "").lower()
            data    = tx.get("input") or tx.get("data") or ""
            sender  = (tx.get("from") or "").lower()
            tx_hash = tx.get("hash", "")

            # Normalise input field: web3.py returns HexBytes (bytes subclass),
            # some providers return a plain hex string "0x...".
            # Always produce: selector = "0x" + 4-byte lowercase hex string
            #                 calldata = raw bytes starting at byte 0
            if isinstance(data, (bytes, bytearray)):
                if len(data) < 4:
                    return None
                calldata = bytes(data)
                selector = "0x" + calldata[:4].hex().lower()
            elif isinstance(data, str):
                stripped = data[2:] if data.startswith("0x") else data
                if len(stripped) < 8:
                    return None
                calldata = bytes.fromhex(stripped)
                selector = "0x" + stripped[:8].lower()
            else:
                return None

            if not calldata or len(calldata) < 4:
                return None

            if to not in self._routers:
                return None

            router_name, version = self._routers[to]

            if version == 2:
                return self._decode_v2(
                    calldata, selector, tx_hash, to, router_name, sender, tx
                )
            
            elif version == 3:
                return self._decode_v3(
                    calldata, selector, tx_hash, to, router_name, sender, tx
                )

        except Exception as e:
            log.debug(f"decode error {tx.get('hash','')[:10]}: {e}")
        return None

    def _to_int(self, val) -> int:
        """Coerce potential hex string or byte value to integer."""
        if val is None:
            return 0
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            if val.startswith("0x"):
                return int(val, 16)
            try:
                return int(val)
            except ValueError:
                return 0
        if isinstance(val, (bytes, bytearray)):
            return int.from_bytes(val, "big")
        return 0


    # ── V2 ────────────────────────────────────────────────────────────────────

    def _decode_v2(
        self, calldata: bytes, selector: str,
        tx_hash: str, router: str, router_name: str,
        sender: str, raw_tx: dict,
    ) -> Optional[DecodedSwap]:
        func = V2_SELECTORS.get(selector)
        if not func:
            return None

        body = calldata[4:]

        try:
            if func in ("swapExactTokensForTokens",
                        "swapExactTokensForETH",
                        "swapExactTokensForTokensSupportingFeeOnTransferTokens",
                        "swapExactTokensForETHSupportingFeeOnTransferTokens"):
                # (uint256 amountIn, uint256 amountOutMin, address[] path, address to, uint256 deadline)
                decoded = abi_decode(
                    ["uint256", "uint256", "address[]", "address", "uint256"], body
                )
                amount_in, amount_out_min, path = decoded[0], decoded[1], decoded[2]
                return DecodedSwap(
                    tx_hash=tx_hash, router=router, router_name=router_name,
                    version=2, function_name=func,
                    token_path=[t.lower() for t in path],
                    amount_in=self._to_int(amount_in), 
                    amount_out_min=self._to_int(amount_out_min),
                    amount_in_is_exact=True, sender=sender,
                    gas_price=self._to_int(raw_tx.get("gasPrice", 0)), 
                    max_fee=self._to_int(raw_tx.get("maxFeePerGas")),
                    max_priority=self._to_int(raw_tx.get("maxPriorityFeePerGas")),
                    fee=[3000],
                    raw_tx=raw_tx,
                )

            elif func in ("swapTokensForExactTokens",
                          "swapTokensForExactETH",
                          "swapETHForExactTokens"):
                # (uint256 amountOut, uint256 amountInMax, address[] path, address to, uint256 deadline)
                decoded = abi_decode(
                    ["uint256", "uint256", "address[]", "address", "uint256"], body
                )
                amount_out, amount_in_max, path = decoded[0], decoded[1], decoded[2]
                return DecodedSwap(
                    tx_hash=tx_hash, router=router, router_name=router_name,
                    version=2, function_name=func,
                    token_path=[t.lower() for t in path],
                    amount_in=self._to_int(amount_in_max), 
                    amount_out_min=self._to_int(amount_out),
                    amount_in_is_exact=False, sender=sender,
                    gas_price=self._to_int(raw_tx.get("gasPrice", 0)), 
                    max_fee=self._to_int(raw_tx.get("maxFeePerGas")),
                    max_priority=self._to_int(raw_tx.get("maxPriorityFeePerGas")),
                    fee=[3000],
                    raw_tx=raw_tx,
                )

            elif func in ("swapExactETHForTokens",
                          "swapExactETHForTokensSupportingFeeOnTransferTokens"):
                # (uint256 amountOutMin, address[] path, address to, uint256 deadline)
                decoded = abi_decode(
                    ["uint256", "address[]", "address", "uint256"], body
                )
                amount_out_min, path = decoded[0], decoded[1]
                return DecodedSwap(
                    tx_hash=tx_hash, router=router, router_name=router_name,
                    version=2, function_name=func,
                    token_path=[t.lower() for t in path],
                    amount_in=self._to_int(raw_tx.get("value", 0)),
                    amount_out_min=self._to_int(amount_out_min),
                    amount_in_is_exact=True, sender=sender,
                    gas_price=self._to_int(raw_tx.get("gasPrice", 0)), 
                    max_fee=self._to_int(raw_tx.get("maxFeePerGas")),
                    max_priority=self._to_int(raw_tx.get("maxPriorityFeePerGas")),
                    fee=[3000],
                    raw_tx=raw_tx,
                )

        except Exception as e:
            log.debug(f"V2 decode failed ({func}): {e}")
        return None

    # ── V3 ────────────────────────────────────────────────────────────────────

    def _decode_v3(
        self, calldata: bytes, selector: str,
        tx_hash: str, router: str, router_name: str,
        sender: str, raw_tx: dict,
    ) -> Optional[DecodedSwap]:
        func = V3_SELECTORS.get(selector)
        if not func:
            return None

        body = calldata[4:].hex()
        start = 64 if not (func == "exactInputSingle" or func == "exactOutputSingle" or func == "multicall") else 0  # heuristic: last 224 bytes are usually the path
        body = bytes.fromhex(body[start:])

        if router_name == "UniswapV3" and func in ("exactInputSingle", "exactOutputSingle"):
            decode_types = ["address", "address", "uint24", "address", "uint256", "uint256", "uint256", "uint160"]
        elif router_name == "UniswapV3" and func in ("exactInput", "exactOutput"):
            decode_types = ["bytes", "address", "uint256", "uint256", "uint256"]
        else:
            decode_types = None

        try:
            if func == "exactInputSingle":
                # struct: tokenIn, tokenOut, fee, recipient, deadline, amountIn,
                #         amountOutMinimum, sqrtPriceLimitX96
                decoded = abi_decode(
                    decode_types or ["address", "address", "uint24", "address",
                     "uint256", "uint256", "uint160"],
                    body,
                )
                return DecodedSwap(
                    tx_hash=tx_hash, router=router, router_name=router_name,
                    version=3, function_name=func,
                    token_path=[decoded[0].lower(), decoded[1].lower()],
                    amount_in=self._to_int(decoded[5] if decode_types else decoded[4]), 
                    amount_out_min=self._to_int(decoded[6] if decode_types else decoded[5]),
                    amount_in_is_exact=True, sender=sender,
                    gas_price=self._to_int(raw_tx.get("gasPrice", 0)), 
                    max_fee=self._to_int(raw_tx.get("maxFeePerGas")),
                    max_priority=self._to_int(raw_tx.get("maxPriorityFeePerGas")),
                    fee=[int(decoded[2])],
                    raw_tx=raw_tx,
                )

            elif func == "exactInput":
                # struct: path(bytes), recipient, deadline, amountIn, amountOutMinimum
                decoded = abi_decode(
                    decode_types or ["bytes", "address", "uint256", "uint256"],
                    body,
                )
                path_tokens, path_fees = decode_v3_path(decoded[0])
                if len(path_tokens) < 2:
                    return None
                return DecodedSwap(
                    tx_hash=tx_hash, router=router, router_name=router_name,
                    version=3, function_name=func,
                    token_path=path_tokens,
                    amount_in=self._to_int(decoded[3] if decode_types else decoded[2]), 
                    amount_out_min=self._to_int(decoded[4] if decode_types else decoded[3]),
                    amount_in_is_exact=True, sender=sender,
                    gas_price=self._to_int(raw_tx.get("gasPrice", 0)), 
                    max_fee=self._to_int(raw_tx.get("maxFeePerGas")),
                    max_priority=self._to_int(raw_tx.get("maxPriorityFeePerGas")),
                    fee=path_fees,
                    raw_tx=raw_tx,
                )

            elif func == "exactOutputSingle":
                decoded = abi_decode(
                    decode_types or ["address", "address", "uint24", "address",
                     "uint256", "uint256", "uint160"],
                    body,
                )
                return DecodedSwap(
                    tx_hash=tx_hash, router=router, router_name=router_name,
                    version=3, function_name=func,
                    token_path=[decoded[0].lower(), decoded[1].lower()],
                    amount_in=self._to_int(decoded[6] if decode_types else decoded[5]), 
                    amount_out_min=self._to_int(decoded[5] if decode_types else decoded[6]),
                    amount_in_is_exact=False, sender=sender,
                    gas_price=self._to_int(raw_tx.get("gasPrice", 0)), 
                    max_fee=self._to_int(raw_tx.get("maxFeePerGas")),
                    max_priority=self._to_int(raw_tx.get("maxPriorityFeePerGas")),
                    fee=[int(decoded[2])],
                    raw_tx=raw_tx,
                )

            elif func == "exactOutput":
                decoded = abi_decode(
                    decode_types or ["bytes", "address", "uint256", "uint256"],
                    body,
                )
                path_tokens, path_fees = decode_v3_path(decoded[0])
                path_tokens.reverse()  # exactOutput path is reversed
                path_fees.reverse()
                if len(path_tokens) < 2:
                    return None
                return DecodedSwap(
                    tx_hash=tx_hash, router=router, router_name=router_name,
                    version=3, function_name=func,
                    token_path=path_tokens,
                    amount_in=self._to_int(decoded[4] if decode_types else decoded[3]), 
                    amount_out_min=self._to_int(decoded[3] if decode_types else decoded[2]),
                    amount_in_is_exact=False, sender=sender,
                    gas_price=self._to_int(raw_tx.get("gasPrice", 0)), 
                    max_fee=self._to_int(raw_tx.get("maxFeePerGas")),
                    max_priority=self._to_int(raw_tx.get("maxPriorityFeePerGas")),
                    fee=path_fees,
                    raw_tx=raw_tx,
                )

            elif func == "multicall":
                # Recursively decode each inner call
                return self._decode_multicall(
                    body, tx_hash, router, router_name, sender, raw_tx
                )

        except Exception as e:
            log.debug(f"V3 decode failed ({func}): {e}")
        return None

    def _decode_multicall(
        self, body: bytes, tx_hash: str, router: str,
        router_name: str, sender: str, raw_tx: dict,
    ) -> Optional[DecodedSwap]:
        """Try to decode the first recognisable swap inside a multicall."""
        try:
            # multicall(uint256 deadline, bytes[] data)  OR  multicall(bytes[] data)
            try:
                _, calls = abi_decode(["uint256", "bytes[]"], body)
            except Exception:
                calls, = abi_decode(["bytes[]"], body)

            for call_data in calls:
                if len(call_data) < 4:
                    continue
                inner_selector = "0x" + call_data[:4].hex()
                result = self._decode_v3(
                    call_data, inner_selector, tx_hash, router, router_name, sender, raw_tx
                )
                if result:
                    return result
        except Exception as e:
            log.debug(f"multicall decode failed: {e}")

        return None


# ─── Router map builder ───────────────────────────────────────────────────────

# Per-chain router addresses
# format: chain_id → list of (address, router_name, version)
ROUTER_ADDRESSES: dict[int, list[tuple[str, str, int]]] = {
    # Ethereum
    1: [
        # Uniswap
        ("0x7a250d5630b4cf539739df2c5dacb4c659f2488d", "UniswapV2",          2),
        ("0xe592427a0aece92de3edee1f18e0157c05861564", "UniswapV3",          3),
        ("0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45", "UniswapV3-02",       3),
        ("0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b", "Uniswap-Universal",  3),
        ("0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad", "Uniswap-Universal2", 3),  # v1.2
        # Sushiswap
        ("0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f", "SushiswapV2",        2),
        ("0x827179dd56d07a7eeA32e3873493835da2866976", "SushiRouteProcessor", 3),
        ("0xb36a0671b3d49587236d7833b01e79798175875f", "SushiRouteProcessor3",3),
        # 1inch
        ("0x1111111254eeb25477b68fb85ed929f73a960582", "1inch-v4",            2),
        ("0x1111111254fb6c44bac0bed2854e76f90643097d", "1inch-v5",            2),
        ("0x111111125421ca6dc452d289314280a0f8842a65", "1inch-v6",            2),
        # Paraswap
        ("0xdef171fe48cf0115b1d80b88dc8eab59176feed7", "Paraswap-v5",        2),
        ("0x6a000f20005980200259b80c5102003040001068", "Paraswap-v6",        2),
        # OKX / Metamask
        ("0xf332761c673b59b21ff6dfa8ad44c14e721578ab", "OKX-DEX",            2),
        ("0x881d40237659c251811cec9c364ef91dc08d300c", "Metamask-Swap",       2),
        # KyberSwap / Maverick
        ("0x6131b5fae19ea4f9d964eac0408e4408b66337b5", "KyberSwap",          2),
        ("0x1121c21dc5758c7515451bd25251c0397394ea4e", "MaverickV3",         3),
    ],
    # BSC
    56: [
        ("0x10ed43c718714eb63d5aa57b78b54704e256024e", "PancakeV2",    2),
        ("0x13f4ea83d0bd40e75c8222255bc855a974568dd4", "PancakeV3",    3),
        ("0x1b02da8cb0d097eb8d57a175b88c7d8b47997506", "SushiswapV2",  2),
        ("0xd99d1c33f9fc3444f8101754abc46c52416550d1", "PancakeV2Test", 2),
    ],
    # Polygon
    137: [
        ("0xa5e0829caced8ffdd4de3c43696c57f7d7a678ff", "QuickswapV2",  2),
        ("0x1b02da8cb0d097eb8d57a175b88c7d8b47997506", "SushiswapV2",  2),
        ("0xe592427a0aece92de3edee1f18e0157c05861564", "UniswapV3",    3),
        ("0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45", "UniswapV3-02", 3),
    ],
    # Base
    8453: [
        ("0x327df1e6de05895d2ab08513aadd9313fe505d86", "BaseswapV2",   2),
        ("0x2626664c2603336e57b271c5c0b26f421741e481", "UniswapV3-02", 3),
        ("0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43", "AerodromeV2",  2),
    ],
    # Arbitrum
    42161: [
        ("0x1b02da8cb0d097eb8d57a175b88c7d8b47997506", "SushiswapV2",  2),
        ("0xe592427a0aece92de3edee1f18e0157c05861564", "UniswapV3",    3),
        ("0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45", "UniswapV3-02", 3),
        ("0xc873fecbd354f5a56e00e710b90ef4201db2448d", "CamelotV2",    2),
    ],
    # zkSync
    324: [
        ("0x8b791913eb07c32779a16750e3868aa8495f5964", "MuteV2",       2),
        ("0x9b5def958d0f3b6955cbea4d5b7809b2fb26b059", "SyncswapV2",   2),
    ],
    # Sepolia
    11155111: [
        ("0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3", "UniswapV2",          2),
        ("0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E", "UniswapV3-02",       3),
        ("0x3A9D48AB9751398BbFa63ad67599Bb04e4BdF98b", "Uniswap-Universal",  3),
        # Sushiswap
        ("0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506", "SushiswapV2",        2),
        # ("0x827179dd56d07a7eeA32e3873493835da2866976", "SushiRouteProcessor", 3),
        # ("0xb36a0671b3d49587236d7833b01e79798175875f", "SushiRouteProcessor3",3),
        # # 1inch
        # ("0x1111111254eeb25477b68fb85ed929f73a960582", "1inch-v4",            2),
        # ("0x1111111254fb6c44bac0bed2854e76f90643097d", "1inch-v5",            2),
        # ("0x111111125421ca6dc452d289314280a0f8842a65", "1inch-v6",            2),
        # # Paraswap
        # ("0xdef171fe48cf0115b1d80b88dc8eab59176feed7", "Paraswap-v5",        2),
        # ("0x6a000f20005980200259b80c5102003040001068", "Paraswap-v6",        2),
        # # OKX / Metamask
        # ("0xf332761c673b59b21ff6dfa8ad44c14e721578ab", "OKX-DEX",            2),
        # ("0x881d40237659c251811cec9c364ef91dc08d300c", "Metamask-Swap",       2),
        # # KyberSwap / Maverick
        # ("0x6131b5fae19ea4f9d964eac0408e4408b66337b5", "KyberSwap",          2),
        # ("0x1121c21dc5758c7515451bd25251c0397394ea4e", "MaverickV3",         3),
    ],
}


def build_router_map(chain_id: int) -> dict[str, tuple[str, int]]:
    """Build the router map from the canonical chain config, plus legacy aggregators.

    DEX router addresses now have one source of truth in ``config.CHAINS``.
    ``ROUTER_ADDRESSES`` remains only as a compatibility list for routers such as
    1inch/Paraswap that are not represented as pool protocols.
    """
    from config import CHAINS
    entries = list(ROUTER_ADDRESSES.get(chain_id, []))
    configured = {
        (d.router.lower(), d.name, d.version)
        for d in CHAINS.get(chain_id, type("X", (), {"dexes": []})()).dexes
        if d.router and d.protocol not in {"curve"}
    }
    merged = {addr.lower(): (name, ver) for addr, name, ver in entries}
    for addr, name, ver in configured:
        merged[addr] = (name, ver)
    return merged


def get_router_addresses(chain_id: int) -> set[str]:
    """Returns all known router addresses for a chain (for watcher pre-filter)."""
    return set(build_router_map(chain_id))
