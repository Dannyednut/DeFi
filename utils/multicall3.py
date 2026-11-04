"""
Multicall3 bulk reserve refresh
================================
Drop-in replacement for the sequential bulk_refresh in CatchupExecutor.

Multicall3 is deployed at the same address on virtually every EVM chain:
    0xcA11bde05977b3631167028862bE2a173976CA11

The aggregate3 function is marked `payable` in the ABI — this is intentional
(allows ETH forwarding to called contracts). For pure reserve reads we call it
with value=0, which is perfectly valid.

Two ABIs are provided:
  - MULTICALL3_ABI: minimal, only what we need (aggregate3)
  - Full ABI available at https://github.com/mds1/multicall
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import time

from web3 import Web3
from web3.contract import Contract
from eth_abi import decode as eth_abi_decode

from log import get_logger
# from graph import TokenGraph
# from cache import PoolCache
from tvl import estimate_tvl_v2, estimate_tvl_v3, is_above_min_liquidity
from config import MIN_LIQUIDITY_USD, ARB_EXEC_ABI, get_chain
from optimizer.abi import QUOTER_V2_ABI, pack_path

log = get_logger("multicall3")

# ── Constants ─────────────────────────────────────────────────────────────────

MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Minimal ABI — aggregate3 only.
# Note: payable means value= is accepted, not required. We always pass value=0.
MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "target",       "type": "address"},
                    {"internalType": "bool",    "name": "allowFailure", "type": "bool"},
                    {"internalType": "bytes",   "name": "callData",     "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Call3[]",
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"internalType": "bool",  "name": "success",    "type": "bool"},
                    {"internalType": "bytes", "name": "returnData", "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Result[]",
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "payable",   # payable but we call with value=0
        "type": "function",
    }
]

# Pool ABIs — only the view functions we need for reserve reads
UNISWAP_V2_RESERVES_ABI = [
    {
        "name": "getReserves",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "reserve0",           "type": "uint112"},
            {"name": "reserve1",           "type": "uint112"},
            {"name": "blockTimestampLast", "type": "uint32"},
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
            {"name": "sqrtPriceX96",   "type": "uint160"},
            {"name": "tick",           "type": "int24"},
            {"name": "observationIndex",              "type": "uint16"},
            {"name": "observationCardinality",        "type": "uint16"},
            {"name": "observationCardinalityNext",    "type": "uint16"},
            {"name": "feeProtocol",    "type": "uint8"},
            {"name": "unlocked",       "type": "bool"},
        ],
    }
]

UNISWAP_V3_LIQUIDITY_ABI = [
    {
        "name": "liquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint128"}]
    }
]

# Chunk size: Multicall3 can handle hundreds of calls per tx, but most RPC
# providers start timing out around 500-1000 calls per batch. 300 is safe.
MULTICALL_CHUNK = 300

# QuoterV2 address (mainnet) - used for V3 path simulation
QUOTER_ADDRESS = get_chain().quoter_v3


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_multicall3(w3: Web3) -> Contract:
    return w3.eth.contract(
        address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
        abi=MULTICALL3_ABI,
    )


def _encode_get_reserves(w3: Web3, pool_addr: str) -> bytes:
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr),
        abi=UNISWAP_V2_RESERVES_ABI,
    )
    return contract.encodeABI(fn_name="getReserves")


def _encode_slot0(w3: Web3, pool_addr: str) -> bytes:
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr),
        abi=UNISWAP_V3_SLOT0_ABI,
    )
    return contract.encodeABI(fn_name="slot0")

def _encode_liquidity(w3: Web3, pool_addr: str) -> bytes:
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr),
        abi=UNISWAP_V3_LIQUIDITY_ABI,
    )
    return contract.encodeABI(fn_name="liquidity")


def _decode_get_reserves(w3: Web3, pool_addr: str, data: bytes):
    return eth_abi_decode(["uint112", "uint112", "uint32"], data)


def _decode_slot0(w3: Web3, pool_addr: str, data: bytes):
    return eth_abi_decode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], data)

def _decode_liquidity(w3: Web3, pool_addr: str, data: bytes):
    return eth_abi_decode(["uint128"], data)


# ── Pool type detection ───────────────────────────────────────────────────────

def _is_v3(graph: "TokenGraph", pool_addr: str) -> bool:
    """
    Determine whether a pool is V3 (slot0) or V2 (getReserves).
    Uses the graph's internal index for efficiency.
    """
    edges = graph.get_edges_for_pool(pool_addr)
    if edges:
        # Check the version attribute of the first edge found for this pool
        return edges[0].version == 3
    return False


# ── Core batch function ───────────────────────────────────────────────────────

def multicall3_bulk_refresh(
    w3: Web3,
    graph: "TokenGraph",
    cache: "PoolCache",
    pool_addrs: set[str],
    chunk_size: int = MULTICALL_CHUNK,
) -> None:
    """
    Refresh reserves for all pools using Multicall3.aggregate3.

    Sends ceil(N / chunk_size) RPC calls total instead of N individual calls.
    Each call is `payable` but invoked with value=0 — safe for pure reads.

    allowFailure=True per call means one broken pool won't abort the whole batch.
    Failed individual calls are logged at DEBUG level and skipped.
    """
    if not pool_addrs:
        return

    mc = _get_multicall3(w3)
    pool_list = list(pool_addrs)
    total = len(pool_list)
    updated = 0
    failed  = 0

    log.info(f"Multicall3 bulk refresh: {total} pools in chunks of {chunk_size}…")

    for chunk_start in range(0, total, chunk_size):
        chunk = pool_list[chunk_start : chunk_start + chunk_size]

        # Build call structs — one per pool
        calls = []
        meta  = []   # parallel list: (pool_addr, query_type)
        
        for addr in chunk:
            is_v3 = _is_v3(graph, addr)
            try:
                if is_v3:
                    slot0_call = _encode_slot0(w3, addr)
                    liq_call = _encode_liquidity(w3, addr)
                    
                    calls.append({
                        "target":       Web3.to_checksum_address(addr),
                        "allowFailure": True, 
                        "callData":     slot0_call,
                    })
                    meta.append((addr, "v3_slot0"))
                    
                    calls.append({
                        "target":       Web3.to_checksum_address(addr),
                        "allowFailure": True, 
                        "callData":     liq_call,
                    })
                    meta.append((addr, "v3_liquidity"))
                else:
                    call_data = _encode_get_reserves(w3, addr)
                    calls.append({
                        "target":       Web3.to_checksum_address(addr),
                        "allowFailure": True,
                        "callData":     call_data,
                    })
                    meta.append((addr, "v2"))
            except Exception as e:
                log.debug(f"encode failed for {addr}: {e}")
                continue

        if not calls:
            continue

        try:
            # aggregate3 is payable — pass value=0 explicitly
            results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as e:
            log.warning(f"Multicall3 chunk {chunk_start}–{chunk_start + chunk_size} "
                        f"failed entirely: {e}. Skipping chunk.")
            failed += len(chunk)
            continue

        # Decode results
        v3_buffer = {}  # temporary dict to associate slot0 + liquidity for v3 pools

        for (addr, q_type), result in zip(meta, results):
            success, return_data = result  # aggregate3 returns (bool, bytes) tuples
            if not success or not return_data:
                log.debug(f"multicall3: call failed for {addr} ({q_type})")
                failed += 1
                continue
            
            try:
                if q_type == "v2":
                    decoded = _decode_get_reserves(w3, addr, return_data)
                    reserve0 = decoded[0]
                    reserve1 = decoded[1]
                    _apply_v2_reserves(graph, cache, addr, reserve0, reserve1)
                    updated += 1
                
                elif q_type == "v3_slot0":
                    decoded = _decode_slot0(w3, addr, return_data)
                    if addr not in v3_buffer:
                        v3_buffer[addr] = {}
                    v3_buffer[addr]['sqrt'] = decoded[0]
                    v3_buffer[addr]['tick'] = decoded[1]
                
                elif q_type == "v3_liquidity":
                    decoded = _decode_liquidity(w3, addr, return_data)
                    if addr not in v3_buffer:
                        v3_buffer[addr] = {}
                    v3_buffer[addr]['liq'] = decoded[0]

            except Exception as e:
                log.debug(f"multicall3: decode failed for {addr} ({q_type}): {e}")
                failed += 1

        # Apply collected V3 updates safely to graph & cache
        for addr, data in v3_buffer.items():
            if 'sqrt' in data and 'liq' in data:
                _apply_v3_data(graph, cache, addr, data['sqrt'], data['tick'], data['liq'])
                updated += 1
            else:
                log.debug(f"multicall3: incomplete v3 data for {addr} (sqrt={data.get('sqrt') is not None}, liq={data.get('liq') is not None})")
                failed += 1

        log.debug(f"Chunk done: {chunk_start + len(chunk)}/{total} processed")

    log.info(f"Multicall3 bulk refresh complete — {updated} updated, {failed} queries failed/skipped.")


# ── Graph update helpers ──────────────────────────────────────────────────────

def _apply_v2_reserves(
    graph: "TokenGraph",
    cache: "PoolCache",
    pool_addr: str,
    reserve0: int,
    reserve1: int,
) -> None:
    # Calculate TVL and filter (Spec 3.4)
    edges = graph._pool_index.get(pool_addr, [])
    if not edges:
        return
        
    t0, t1 = edges[0].token_in, edges[0].token_out
    tvl_usd = estimate_tvl_v2(t0, t1, reserve0, reserve1)
    
    if not is_above_min_liquidity(tvl_usd, MIN_LIQUIDITY_USD):
        graph.remove_pool(pool_addr)
        if cache:
            cache.mark_below_threshold(pool_addr)
        log.debug(f"Pool {pool_addr[:10]} dropped: low V2 TVL (${tvl_usd:,.2f})")
        return

    for pool in edges:
        if pool is not None:
            # Assign according to token order
            if pool.token_in.lower() < pool.token_out.lower():
                pool.reserve_in = reserve0
                pool.reserve_out = reserve1
            else:
                pool.reserve_in = reserve1
                pool.reserve_out = reserve0
            
            pool.tvl_usd = tvl_usd
            pool.last_updated = time.time()
    
    # Write through to cache
    with suppress(Exception):
        cache.update_reserves(pool_addr, r0=reserve0, r1=reserve1)


def _apply_v3_data(
    graph: "TokenGraph",
    cache: "PoolCache",
    pool_addr: str,
    sqrt_price_x96: int,
    tick: int,
    liquidity: int,
) -> None:
    # Calculate TVL and filter (Spec 3.4)
    edges = graph._pool_index.get(pool_addr, [])
    if not edges:
        return
        
    t0, t1 = edges[0].token_in, edges[0].token_out
    tvl_usd = estimate_tvl_v3(t0, t1, sqrt_price_x96, liquidity)
    
    if not is_above_min_liquidity(tvl_usd, MIN_LIQUIDITY_USD):
        graph.remove_pool(pool_addr)
        if cache:
            cache.mark_below_threshold(pool_addr)
        log.debug(f"Pool {pool_addr[:10]} dropped: low V3 TVL (${tvl_usd:,.2f})")
        return

    for pool in edges:
        if pool is not None:
            pool.sqrt_price_x96 = sqrt_price_x96
            pool.tick = tick
            if hasattr(pool, 'liquidity'):
                pool.liquidity = liquidity
            
            pool.tvl_usd = tvl_usd
            pool.last_updated = time.time()
        
    with suppress(Exception):
        cache.update_reserves(pool_addr, sqrt_price_x96=sqrt_price_x96, liquidity=liquidity)


# ── Batch Arb Simulation via Multicall3 ───────────────────────────────────────
ARB_EXEC_ADDRESS= get_chain().arb_exec_address
ARB_DATA_TYPES = "(uint256,uint256,address[],address[],uint24[],address,uint8)"

# yieldOut returns (ArbData ad, uint256 profit)
YIELD_OUT_RETURN_TYPES = [ARB_DATA_TYPES, "uint256"]

def _encode_yieldOut(w3: Web3, cycle) -> bytes:
    """Encode yieldOut call data for a cycle."""
    contract = w3.eth.contract(
        address=ARB_EXEC_ADDRESS,
        abi=ARB_EXEC_ABI,
    )
    arb_data = {
        "amountIn": 0, #int(cycle.amount_in_wei),
        "minProfit": 0,
        "tokens": [w3.to_checksum_address(t) for t in cycle.tokens],
        "pools": [w3.to_checksum_address(p) for p in cycle.pools],
        "fees": [int(f * 100) for f in cycle.fees],  # Convert bps to pip
        "tokenIn": w3.to_checksum_address(cycle.tokens[0]),
        "mode": 0,
    }
    # arb_data = (
    #     0,
    #     0,
    #     [w3.to_checksum_address(t) for t in cycle.tokens],
    #     [w3.to_checksum_address(p) for p in cycle.pools],
    #     [int(f * 100) for f in cycle.fees],
    #     w3.to_checksum_address(cycle.tokens[0]),
    #     0,
    # )
    return contract.encodeABI(fn_name="yieldOut", args=[arb_data])


def _decode_yieldOut(return_data: bytes) -> tuple[tuple, int] | None:
    """
    Decode raw returnData bytes from a yieldOut call.
    Returns (ad_tuple, profit) or None if decode fails.
    """
    if not return_data or return_data == b'\x00' * 32:
        return None, None
    try:
        decoded = eth_abi_decode(YIELD_OUT_RETURN_TYPES, return_data)
        # log.debug(f"Decoded yieldOut return data: {decoded}")
        (ad, profit) = eth_abi_decode(YIELD_OUT_RETURN_TYPES, return_data)
        return ad, profit
    except Exception as e:
        log.warning(f"Failed to decode yieldOut return data: {e}")
        return None, None

# ── Bulk yieldOut via Multicall3 ──────────────────────────────────────────
def multicall3_batch_yieldOut(
    w3: Web3,
    arb_exec_address: str,
    cycles: list,
    chunk_size: int = 50,
) -> dict[int, tuple]:
    """
    Call yieldOut() for multiple arb paths in one Multicall3 batch.

    Args:
        w3: Web3 instance
        arb_exec_address: ArbExec contract address
        cycles: List of ArbitrageCycle objects
        chunk_size: Number of calls per batch (yieldOut is heavy — keep small)

    Returns:
        Dict mapping cycle index -> (ad_tuple, profit_wei) for profitable cycles only.
    """
    if not cycles or not arb_exec_address:
        return {}

    addr = Web3.to_checksum_address(arb_exec_address)
    mc = _get_multicall3(w3)
    results: dict[int, tuple] = {}

    for chunk_start in range(0, len(cycles), chunk_size):
        chunk = cycles[chunk_start: chunk_start + chunk_size]

        calls = []
        for cycle in chunk:
            try:
                calldata = _encode_yieldOut(w3, cycle)
                calls.append({
                    "target":       addr,
                    "allowFailure": True,   # one bad path won't abort the batch
                    "callData":     calldata,
                })
            except Exception as e:
                calls.append({
                    "target":       addr,
                    "allowFailure": True,
                    "callData":     b"",    # placeholder — will fail gracefully
                })

        try:
            raw_results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as e:
            log.info(f"Entire chunk failed — skip this chunk: {e}")
            # print("Entire chunk failed — skip this chunk")
            continue

        for i, (success, return_data) in enumerate(raw_results):
            idx = chunk_start + i
            if not success:
                log.debug(f"Multicall3: call failed for cycle {idx} | success={success}")
                results[idx] = (None, None) #(success, return_data)
                continue
            decoded = _decode_yieldOut(return_data)
            if decoded: #and decoded[1] and decoded[1] > 0:   # only keep profitable
                log.debug(f"Multicall3: cycle {idx} profit={decoded[1]} wei")
                results[idx] = decoded
            # else:
            #     results[idx] = (None, None)

    return results

# ── Batch V3 Quote via Multicall3 ────────────────────────────────────────────
def _encode_quote_exact_input(w3: Web3, tokens: list[str], fees: list[int], amount_in: int) -> bytes:
    """Encode quoteExactInput call for a V3 path."""
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(QUOTER_ADDRESS),
        abi=QUOTER_V2_ABI,
    )
    path = pack_path(tokens, fees)
    return quoter.encodeABI(fn_name="quoteExactInput", args=[path, amount_in])


def _decode_quote_exact_input(return_data: bytes) -> int | None:
    """
    Decode quoteExactInput return data.
    Returns amountOut or None if decode fails.
    """
    if not return_data or return_data == b'\x00' * 32:
        return None
    try:
        # Returns: (amountOut, sqrtPriceX96AfterList[], initializedTicksCrossedList[], gasEstimate)
        decoded = eth_abi_decode(["uint256", "uint160[]", "uint32[]", "uint256"], return_data)
        return decoded[0]  # amountOut
    except Exception:
        return None


def multicall3_batch_quote_v3(
    w3: Web3,
    cycles: list,
    quoter_address: str = QUOTER_ADDRESS,
    chunk_size: int = 50,
) -> dict[int, int]:
    """
    Batch quoteExactInput calls for pure V3 cycles via Multicall3.

    Args:
        w3: Web3 instance
        cycles: List of ArbitrageCycle objects (must be all V3)
        quoter_address: QuoterV2 contract address
        chunk_size: Number of calls per batch

    Returns:
        Dict mapping cycle index -> amountOut (only profitable/valid results)
    """
    if not cycles:
        return {}

    quoter = Web3.to_checksum_address(quoter_address)
    mc = _get_multicall3(w3)
    results: dict[int, int] = {}

    for chunk_start in range(0, len(cycles), chunk_size):
        chunk = cycles[chunk_start: chunk_start + chunk_size]

        calls = []
        for cycle in chunk:
            try:
                # Convert fees from bps to pip (e.g., 0.003 -> 3000)
                fees_pip = [int(f * 100) for f in cycle.fees]
                calldata = _encode_quote_exact_input(
                    w3, cycle.tokens, fees_pip, cycle.amount_in_wei
                )
                calls.append({
                    "target": quoter,
                    "allowFailure": True,
                    "callData": calldata,
                })
            except Exception as e:
                log.debug(f"Failed to encode quote for cycle: {e}")
                calls.append({
                    "target": quoter,
                    "allowFailure": True,
                    "callData": b"",
                })

        if not calls:
            continue

        try:
            raw_results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as e:
            log.warning(f"Multicall3 quote batch failed: {e}")
            continue

        for i, (success, return_data) in enumerate(raw_results):
            idx = chunk_start + i
            if not success or not return_data:
                continue
            amount_out = _decode_quote_exact_input(return_data)
            if amount_out and amount_out > 0:
                # Calculate actual profit: amount_out - amount_in
                profit = amount_out - cycles[idx].amount_in_wei
                results[idx] = (None, profit)
    log.info(f"Multicall3 quote batch results: {len(results)} cycles")
    return results


# ── Integration ───────────────────────────────────────────────────────────────
#
# In catchup_executor.py, replace the bulk_refresh method body with:
#
#   from multicall3_refresh import multicall3_bulk_refresh
#
#   def bulk_refresh(self, w3, graph, cache, pool_addrs, **kwargs):
#       multicall3_bulk_refresh(w3, graph, cache, pool_addrs)