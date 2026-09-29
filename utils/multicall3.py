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
from utils.web3_compat import encode_call

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
    return encode_call(contract, "getReserves")


def _encode_slot0(w3: Web3, pool_addr: str) -> bytes:
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr),
        abi=UNISWAP_V3_SLOT0_ABI,
    )
    return encode_call(contract, "slot0")

def _encode_liquidity(w3: Web3, pool_addr: str) -> bytes:
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr),
        abi=UNISWAP_V3_LIQUIDITY_ABI,
    )
    return encode_call(contract, "liquidity")


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
    updated_addresses: set[str] = set()

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
                    if _apply_v2_reserves(graph, cache, addr, reserve0, reserve1):
                        updated += 1
                        updated_addresses.add(addr.lower())
                
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
                if _apply_v3_data(graph, cache, addr, data['sqrt'], data['tick'], data['liq']):
                    updated += 1
                    updated_addresses.add(addr.lower())
            else:
                log.debug(f"multicall3: incomplete v3 data for {addr} (sqrt={data.get('sqrt') is not None}, liq={data.get('liq') is not None})")
                failed += 1

        log.debug(f"Chunk done: {chunk_start + len(chunk)}/{total} processed")

    log.info(f"Multicall3 bulk refresh complete — {updated} updated, {failed} queries failed/skipped.")
    return updated_addresses


def multicall3_bulk_refresh_adapters(
    w3: Web3,
    pool_adapter_pairs: list[tuple[Any, Any]],
    chunk_calls: int = MULTICALL_CHUNK,
) -> dict[str, dict]:
    """Batch-refresh ANY protocol whose adapter implements
    ProtocolAdapter.build_refresh_calls()/decode_refresh_result() — Curve,
    Balancer, SyncSwap, Aerodrome today, more later — into Multicall3
    aggregate3 batches, the same way multicall3_bulk_refresh() above already
    does for native V2/V3 pools.

    Previously these protocols were refreshed one pool at a time via
    individual adapter.load_pool() RPC round-trips (see
    block_information.py::ProtocolStateSynchronizer._refresh_one and
    registry.py::update_pool_reserves) — N pools meant N (or more)
    sequential round-trips. This folds them into ceil(calls / chunk_calls)
    batched round-trips instead.

    Different protocols/ABIs can share a single aggregate3 batch — Multicall3
    only needs (target, callData) per call, it has no notion of "protocol",
    so calls for a Curve pool and a Balancer pool can sit in the same
    request.

    `pool_adapter_pairs` is a list of (PoolRef, ProtocolAdapter) tuples.
    Returns {pool_address.lower(): decoded_state_dict} for every pool that
    decoded successfully; pools with no calls or a failed/partial result are
    simply omitted (caller decides what to do — typically fall back to a
    sequential refresh for anything missing from the result).
    """
    if not pool_adapter_pairs:
        return {}

    mc = _get_multicall3(w3)
    out: dict[str, dict] = {}
    total_pools = len(pool_adapter_pairs)
    decoded_ok = 0
    decode_failed = 0

    idx = 0
    while idx < total_pools:
        batch = pool_adapter_pairs[idx: idx + chunk_calls]
        idx += chunk_calls

        calls: list[dict] = []
        spans: list[tuple[Any, Any, int, int]] = []  # (pool_ref, adapter, start, count)
        for pool_ref, adapter in batch:
            try:
                pool_calls = adapter.build_refresh_calls(pool_ref)
            except Exception as e:
                log.debug(f"build_refresh_calls failed for {pool_ref.address}: {e}")
                continue
            if not pool_calls:
                continue
            start = len(calls)
            calls.extend(pool_calls)
            spans.append((pool_ref, adapter, start, len(pool_calls)))

        if not calls:
            continue

        try:
            results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as e:
            log.warning(f"Adapter multicall batch of {len(calls)} calls ({len(spans)} pools) "
                        f"failed entirely: {e}. Skipping batch.")
            decode_failed += len(spans)
            continue

        for pool_ref, adapter, start, count in spans:
            raw = [(bool(ok), bytes(data)) for ok, data in results[start:start + count]]
            try:
                state = adapter.decode_refresh_result(pool_ref, raw)
            except Exception as e:
                log.debug(f"decode_refresh_result failed for {pool_ref.address}: {e}")
                state = None
            if not state:
                decode_failed += 1
                continue
            out[pool_ref.address.lower()] = state
            decoded_ok += 1

    log.info(f"Multicall3 adapter refresh complete — {decoded_ok}/{total_pools} pools decoded, "
             f"{decode_failed} failed/skipped.")
    return out


def multicall3_batch_discover_specialized(
    w3: Web3,
    candidates: list[tuple[Any, Any]],
    chunk_calls: int = MULTICALL_CHUNK,
) -> dict[str, dict]:
    """Batch-verify candidate pools for protocols with per-address discovery
    (Curve, Balancer, SyncSwap, Aerodrome today) into Multicall3 batches,
    instead of one discover_from_event()/discover_pair() sequence per
    candidate.

    `candidates` is a list of (PoolRef, ProtocolAdapter) tuples — the PoolRef
    only needs whatever metadata that adapter's build_discovery_calls()
    requires (e.g. Curve needs metadata["i"]/["j"]; Balancer needs
    metadata["pool_id"]; SyncSwap/Aerodrome need nothing beyond the address).

    Runs up to two batched rounds:
      1. build_discovery_calls()/decode_discovery_result() for every
         candidate, batched together regardless of protocol.
      2. For adapters where needs_discovery_verification() is True (their
         stage-1 result isn't trustworthy on its own — see protocols/base.py),
         build_discovery_verification_calls()/decode_discovery_verification()
         for just those survivors, in a second batch.

    Returns {pool_address.lower(): decoded_state_dict} for every candidate
    that passed both rounds it needed.
    """
    if not candidates:
        return {}

    mc = _get_multicall3(w3)

    # Round 1
    stage1: dict[str, tuple[Any, Any, dict]] = {}  # addr -> (ref, adapter, decoded)
    idx = 0
    while idx < len(candidates):
        batch = candidates[idx: idx + chunk_calls]
        idx += chunk_calls

        calls: list[dict] = []
        spans: list[tuple[Any, Any, int, int]] = []
        for ref, adapter in batch:
            try:
                pool_calls = adapter.build_discovery_calls(ref)
            except Exception as e:
                log.debug(f"build_discovery_calls failed for {ref.address}: {e}")
                continue
            if not pool_calls:
                continue
            start = len(calls)
            calls.extend(pool_calls)
            spans.append((ref, adapter, start, len(pool_calls)))

        if not calls:
            continue
        try:
            results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as e:
            log.warning(f"Discovery multicall round 1 ({len(calls)} calls, {len(spans)} candidates) "
                        f"failed entirely: {e}. Skipping batch.")
            continue

        for ref, adapter, start, count in spans:
            raw = [(bool(ok), bytes(data)) for ok, data in results[start:start + count]]
            try:
                decoded = adapter.decode_discovery_result(ref, raw)
            except Exception as e:
                log.debug(f"decode_discovery_result failed for {ref.address}: {e}")
                decoded = None
            if decoded:
                stage1[ref.address.lower()] = (ref, adapter, decoded)

    # Round 2 (verification), only for adapters that need it
    out: dict[str, dict] = {}
    need_verify = []
    for ref, adapter, decoded in stage1.values():
        if adapter.needs_discovery_verification():
            need_verify.append((ref, adapter, decoded))
        else:
            out[ref.address.lower()] = decoded

    idx = 0
    while idx < len(need_verify):
        batch = need_verify[idx: idx + chunk_calls]
        idx += chunk_calls

        calls = []
        spans2: list[tuple[Any, Any, dict, int, int]] = []
        for ref, adapter, decoded in batch:
            try:
                pool_calls = adapter.build_discovery_verification_calls(ref, decoded)
            except Exception as e:
                log.debug(f"build_discovery_verification_calls failed for {ref.address}: {e}")
                continue
            if not pool_calls:
                continue
            start = len(calls)
            calls.extend(pool_calls)
            spans2.append((ref, adapter, decoded, start, len(pool_calls)))

        if not calls:
            continue
        try:
            results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as e:
            log.warning(f"Discovery multicall round 2 (verification, {len(calls)} calls, "
                        f"{len(spans2)} candidates) failed entirely: {e}. Skipping batch.")
            continue

        for ref, adapter, decoded, start, count in spans2:
            raw = [(bool(ok), bytes(data)) for ok, data in results[start:start + count]]
            try:
                final = adapter.decode_discovery_verification(ref, decoded, raw)
            except Exception as e:
                log.debug(f"decode_discovery_verification failed for {ref.address}: {e}")
                final = None
            if final:
                out[ref.address.lower()] = final

    log.info(f"Multicall3 specialized discovery complete — {len(out)}/{len(candidates)} candidates verified.")
    return out


def multicall3_batch_lookup_pairs(
    w3: Web3,
    entries: list[tuple[Any, str, str]],
    chunk_calls: int = MULTICALL_CHUNK,
) -> list:
    """Batch-lookup pool addresses for KNOWN (token0, token1) pairs across
    protocols with a factory-style pair lookup (SyncSwap, Aerodrome) — the
    opposite direction from multicall3_batch_discover_specialized (there the
    address is known and tokens aren't; here the tokens are known and the
    address isn't).

    `entries` is a list of (adapter, token0, token1) tuples. Returns a flat
    list of PoolRef for every real pool found (a protocol like Aerodrome can
    return more than one PoolRef per entry — e.g. both a stable and a
    volatile pool for the same pair).

    Replaces discover_from_swap()'s previous `for pair: for dex: await
    adapter.discover_pair(...)` sequential loop.
    """
    if not entries:
        return []

    mc = _get_multicall3(w3)
    out: list = []
    idx = 0
    while idx < len(entries):
        batch = entries[idx: idx + chunk_calls]
        idx += chunk_calls

        calls: list[dict] = []
        spans: list[tuple[Any, str, str, int, int]] = []
        for adapter, t0, t1 in batch:
            try:
                pair_calls = adapter.build_pair_lookup_calls(t0, t1)
            except Exception as e:
                log.debug(f"build_pair_lookup_calls failed for {t0}/{t1}: {e}")
                continue
            if not pair_calls:
                continue
            start = len(calls)
            calls.extend(pair_calls)
            spans.append((adapter, t0, t1, start, len(pair_calls)))

        if not calls:
            continue
        try:
            results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as e:
            log.warning(f"Pair-lookup multicall ({len(calls)} calls, {len(spans)} entries) "
                        f"failed entirely: {e}. Skipping batch.")
            continue

        for adapter, t0, t1, start, count in spans:
            raw = [(bool(ok), bytes(data)) for ok, data in results[start:start + count]]
            try:
                refs = adapter.decode_pair_lookup(t0, t1, raw)
            except Exception as e:
                log.debug(f"decode_pair_lookup failed for {t0}/{t1}: {e}")
                refs = []
            out.extend(refs)

    return out



# ── Batched pool discovery ────────────────────────────────────────────────────

def _encode_token0(w3: Web3, addr: str) -> bytes:
    c = w3.eth.contract(address=w3.to_checksum_address(addr), abi=[{"name":"token0","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}])
    return encode_call(c, "token0")


def _encode_token1(w3: Web3, addr: str) -> bytes:
    c = w3.eth.contract(address=w3.to_checksum_address(addr), abi=[{"name":"token1","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}])
    return encode_call(c, "token1")


def _encode_get_pair(w3: Web3, factory: str, token0: str, token1: str) -> bytes:
    c = w3.eth.contract(address=w3.to_checksum_address(factory), abi=UNISWAP_V2_FACTORY_ABI)
    return encode_call(c, "getPair", [w3.to_checksum_address(token0), w3.to_checksum_address(token1)])


def _encode_get_pool(w3: Web3, factory: str, token0: str, token1: str, fee: int) -> bytes:
    c = w3.eth.contract(address=w3.to_checksum_address(factory), abi=UNISWAP_V3_FACTORY_ABI)
    return encode_call(c, "getPool", [w3.to_checksum_address(token0), w3.to_checksum_address(token1), int(fee)])



def multicall3_discover_token_pairs(w3: Web3, token_pairs: list[tuple[str, str]], chunk_calls: int = MULTICALL_CHUNK) -> list[dict]:
    """Batch factory getPair/getPool checks for mempool/swap-path discovery."""
    if not token_pairs:
        return []
    chain = get_chain()
    calls, meta = [], []
    seen = set()
    for a, b in token_pairs:
        t0, t1 = str(a).lower(), str(b).lower()
        pair_key = (min(t0, t1), max(t0, t1))
        if pair_key in seen:
            continue
        seen.add(pair_key)
        for dex in chain.dexes:
            if not dex.enabled or not dex.factory or dex.protocol not in {"uniswap_v2", "uniswap_v3"}:
                continue
            if dex.version == 2:
                calls.append({"target": Web3.to_checksum_address(dex.factory), "allowFailure": True, "callData": _encode_get_pair(w3, dex.factory, t0, t1)})
                meta.append((dex, t0, t1, None))
            else:
                for fee in (dex.fee_tiers or (100, 500, 2500, 3000, 10000)):
                    calls.append({"target": Web3.to_checksum_address(dex.factory), "allowFailure": True, "callData": _encode_get_pool(w3, dex.factory, t0, t1, fee)})
                    meta.append((dex, t0, t1, fee))
    mc = _get_multicall3(w3)
    found = []
    for start in range(0, len(calls), chunk_calls):
        batch = calls[start:start + chunk_calls]
        batch_meta = meta[start:start + chunk_calls]
        try:
            results = mc.functions.aggregate3(batch).call({"value": 0})
        except Exception as exc:
            log.warning("Multicall3 token-pair discovery chunk failed: %s", exc)
            continue
        for m, result in zip(batch_meta, results):
            if not result[0] or not result[1]:
                continue
            try:
                address = eth_abi_decode(["address"], result[1])[0].lower()
                if int(address, 16) != 0:
                    found.append({"pool_address": address, "dex": m[0], "token0": m[1], "token1": m[2], "fee": m[3]})
            except Exception:
                continue
    return found


def multicall3_discover_event_hints(w3: Web3, hints: list[dict], chunk_calls: int = MULTICALL_CHUNK) -> list[dict]:
    """Batch unknown-pool event discovery for generic V2/V3 deployments.

    The first stage introspects token0/token1 once per unknown pool. The second
    stage verifies those tokens against configured factories. Per-call failures
    are isolated with aggregate3(allowFailure=True), so one malformed contract
    cannot poison a whole discovery batch.
    """
    if not hints:
        return []
    chain = get_chain()
    dex_by_protocol = {}
    for d in chain.dexes:
        if d.enabled and d.factory and d.protocol in {"uniswap_v2", "uniswap_v3"}:
            dex_by_protocol.setdefault(d.protocol, []).append(d)

    unique = {}
    for h in hints:
        addr = str(h.get("pool_address", "")).lower()
        protocol = str(h.get("protocol", "")).lower()
        if addr.startswith("0x") and protocol in dex_by_protocol:
            unique[(protocol, addr)] = h

    if not unique:
        return []
    mc = _get_multicall3(w3)
    stage1 = []
    meta1 = []
    for (protocol, addr), hint in unique.items():
        for kind, encoder in (("token0", _encode_token0), ("token1", _encode_token1)):
            stage1.append({"target": Web3.to_checksum_address(addr), "allowFailure": True, "callData": encoder(w3, addr)})
            meta1.append((protocol, addr, kind, hint))

    decoded = {}
    for start in range(0, len(stage1), chunk_calls):
        calls = stage1[start:start + chunk_calls]
        meta = meta1[start:start + chunk_calls]
        try:
            results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as exc:
            log.warning("Multicall3 discovery introspection chunk failed: %s", exc)
            continue
        for m, result in zip(meta, results):
            if not result[0] or not result[1]:
                continue
            try:
                token = eth_abi_decode(["address"], result[1])[0].lower()
                decoded.setdefault((m[0], m[1]), {})[m[2]] = token
            except Exception:
                continue

    stage2 = []
    meta2 = []
    for (protocol, addr), h in unique.items():
        d = decoded.get((protocol, addr), {})
        t0, t1 = d.get("token0"), d.get("token1")
        if not t0 or not t1:
            continue
        for dex in dex_by_protocol[protocol]:
            if protocol == "uniswap_v2":
                stage2.append({"target": Web3.to_checksum_address(dex.factory), "allowFailure": True, "callData": _encode_get_pair(w3, dex.factory, t0, t1)})
                meta2.append((h, dex, addr, t0, t1, None))
            else:
                for fee in (dex.fee_tiers or (100, 500, 2500, 3000, 10000)):
                    stage2.append({"target": Web3.to_checksum_address(dex.factory), "allowFailure": True, "callData": _encode_get_pool(w3, dex.factory, t0, t1, fee)})
                    meta2.append((h, dex, addr, t0, t1, fee))

    found = []
    for start in range(0, len(stage2), chunk_calls):
        calls = stage2[start:start + chunk_calls]
        meta = meta2[start:start + chunk_calls]
        try:
            results = mc.functions.aggregate3(calls).call({"value": 0})
        except Exception as exc:
            log.warning("Multicall3 discovery verification chunk failed: %s", exc)
            continue
        for m, result in zip(meta, results):
            if not result[0] or not result[1]:
                continue
            try:
                expected = eth_abi_decode(["address"], result[1])[0].lower()
                if expected == m[2]:
                    found.append({"hint": m[0], "dex": m[1], "pool_address": m[2], "token0": m[3], "token1": m[4], "fee": m[5]})
            except Exception:
                continue
    return found

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
        return False
        
    t0, t1, dex = edges[0].token_in, edges[0].token_out, edges[0].dex_name
    existing_tvl = max((float(getattr(e, "tvl_usd", 0.0) or 0.0) for e in edges), default=0.0)
    tvl_usd = existing_tvl if existing_tvl > 0 else estimate_tvl_v2(t0, t1, reserve0, reserve1, dex=dex)
    
    if not is_above_min_liquidity(tvl_usd, MIN_LIQUIDITY_USD):
        graph.remove_pool(pool_addr)
        if cache:
            cache.mark_below_threshold(pool_addr)
        log.debug(f"Pool {pool_addr[:10]} dropped: low V2 TVL (${tvl_usd:,.2f})")
        return False

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
    return True


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
        return False
        
    t0, t1, dex = edges[0].token_in, edges[0].token_out, edges[0].dex_name
    existing_tvl = max((float(getattr(e, "tvl_usd", 0.0) or 0.0) for e in edges), default=0.0)
    tvl_usd = existing_tvl if existing_tvl > 0 else estimate_tvl_v3(t0, t1, sqrt_price_x96, liquidity, dex=dex)
    
    if not is_above_min_liquidity(tvl_usd, MIN_LIQUIDITY_USD):
        graph.remove_pool(pool_addr)
        if cache:
            cache.mark_below_threshold(pool_addr)
        log.debug(f"Pool {pool_addr[:10]} dropped: low V3 TVL (${tvl_usd:,.2f})")
        return False

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
    return True


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
    return encode_call(contract, "yieldOut", [arb_data])


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
                log.debug(f"Multicall3: call failed for cycle {idx} | success={success} | cycle={cycles[idx]} | return_data={return_data} ")
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
    return encode_call(quoter, "quoteExactInput", [path, amount_in])


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