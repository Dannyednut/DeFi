from __future__ import annotations

import asyncio
from typing import Any

import httpx
from eth_abi import decode as eth_abi_decode

from .base import ProtocolAdapter
from .models import PoolRef, Quote
from utils.web3_compat import encode_call

CURVE_API = "https://api.curve.finance/v1"
CURVE_CHAIN_NAMES = {1: "ethereum", 56: "bsc", 137: "polygon", 8453: "base", 42161: "arbitrum", 324: "zksync"}

CURVE_POOL_ABI = [
    {"name": "coins", "type": "function", "stateMutability": "view", "inputs": [{"name": "arg0", "type": "uint256"}], "outputs": [{"name": "", "type": "address"}]},
    {"name": "balances", "type": "function", "stateMutability": "view", "inputs": [{"name": "arg0", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "get_dy", "type": "function", "stateMutability": "view", "inputs": [{"name": "i", "type": "uint256"}, {"name": "j", "type": "uint256"}, {"name": "dx", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "get_dy_underlying", "type": "function", "stateMutability": "view", "inputs": [{"name": "i", "type": "uint256"}, {"name": "j", "type": "uint256"}, {"name": "dx", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}]},
]


class CurveAdapter(ProtocolAdapter):
    protocol = "curve"

    def _pool_contract(self, address: str):
        return self.w3.eth.contract(address=self.w3.to_checksum_address(address), abi=CURVE_POOL_ABI)

    async def discover_all(self, min_tvl_usd: float = 10_000) -> list[PoolRef]:
        chain = CURVE_CHAIN_NAMES.get(self.config.chain_id)
        if not chain:
            return []
        url = f"{CURVE_API}/getPools/big/{chain}"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)
            r.raise_for_status()
            payload = r.json()
        rows = payload.get("data", payload)
        if isinstance(rows, dict):
            rows = rows.get("poolData", rows.get("pools", []))
        result: list[PoolRef] = []
        for row in rows or []:
            address = str(row.get("address") or row.get("swap") or row.get("poolAddress") or row.get("id") or "").lower()
            if not address.startswith("0x"):
                continue
            coins = row.get("coins") or row.get("underlyingCoins") or []
            coin_addrs = []
            for c in coins:
                if isinstance(c, str):
                    coin_addrs.append(c.lower())
                elif isinstance(c, dict):
                    a = c.get("address") or c.get("addressLower") or c.get("id")
                    if a: coin_addrs.append(str(a).lower())
            tvl = float(row.get("usdTotal") or row.get("usd_total") or row.get("tvl") or row.get("tvlUsd") or 0)
            meta = {"coins": coin_addrs, "tvl_usd": tvl, "curve_id": row.get("id"),
                    "name": row.get("name"), "reference_asset": row.get("referenceAsset")}
            for i, a in enumerate(coin_addrs):
                for j, b in enumerate(coin_addrs):
                    if i != j:
                        result.append(PoolRef(address, self.protocol, self.config.name, a, b, 0,
                                               {**meta, "i": i, "j": j}))
        return result

    def discover_pair(self, token_in: str, token_out: str) -> list[PoolRef]:
        # Pair discovery is API-backed and therefore async; callers that need
        # pair-on-demand discovery should use discover_all() and index the result.
        return []

    def load_pool(self, pool: PoolRef) -> dict[str, Any] | None:
        try:
            c = self._pool_contract(pool.address)
            i, j = int(pool.metadata["i"]), int(pool.metadata["j"])
            bi = c.functions.balances(i).call()
            bj = c.functions.balances(j).call()
            return {"r0": bi, "r1": bj, "token0": pool.token0, "token1": pool.token1,
                    "i": i, "j": j, "tvl_usd": pool.metadata.get("tvl_usd", 0),
                    "protocol": self.protocol}
        except Exception:
            return None

    def quote_exact_in(self, pool: PoolRef, token_in: str, amount_in: int) -> Quote | None:
        try:
            i, j = int(pool.metadata["i"]), int(pool.metadata["j"])
            c = self._pool_contract(pool.address)
            try:
                out = c.functions.get_dy(i, j, int(amount_in)).call()
                mode = "exchange"
            except Exception:
                out = c.functions.get_dy_underlying(i, j, int(amount_in)).call()
                mode = "exchange_underlying"
            return Quote(amount_in, int(out), self.protocol, pool.address, 0,
                         {"i": i, "j": j, "quote_mode": mode})
        except Exception:
            return None

    # ── Batched refresh ──────────────────────────────────────────────────────
    # A refresh only needs the two `balances(i)`/`balances(j)` reads — token
    # addresses and the i/j indices are already known from the graph/PoolRef
    # metadata, so unlike load_pool() (used for first-time discovery) this
    # never needs to re-derive them.

    def supports_batched_refresh(self) -> bool:
        return True

    def build_refresh_calls(self, pool: PoolRef) -> list[dict]:
        i, j = int(pool.metadata["i"]), int(pool.metadata["j"])
        c = self._pool_contract(pool.address)
        target = self.w3.to_checksum_address(pool.address)
        # A third call alongside the two balances() reads: get_dy(i, j, probe)
        # for a small probe amount, so exchange_rate/spot_rate can be
        # recomputed from the pool's REAL StableSwap invariant on every
        # refresh, not just balances(i)/balances(j) alone. Curve pools are
        # deliberately not priced as a simple balance ratio (unlike V2/
        # SyncSwap/Aerodrome) — StableSwap's whole point is holding price
        # near 1:1 across a wide range of balance ratios, so balances[j]/
        # balances[i] would be systematically wrong as a price signal here.
        # get_dy() against the actual contract is correct regardless of A
        # (the amplification coefficient) or how skewed the pool currently
        # is. This adds one more call to the same batch rather than a
        # separate round-trip, so the earlier Multicall3 batching work still
        # applies in full — see decode_refresh_result below for why this
        # was added (a real, previously-shipped staleness bug: without it,
        # only reserves refreshed on this path and spot_rate silently froze
        # at whatever it was computed as during initial pool registration).
        decimals = 18
        try:
            from tvl import token_decimals
            decimals = token_decimals(pool.token0 or "")
        except Exception:
            pass
        probe = 10 ** max(1, decimals - 4)
        return [
            {"target": target, "allowFailure": True, "callData": encode_call(c, "balances", [i])},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "balances", [j])},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "get_dy", [i, j, probe])},
        ]

    def decode_refresh_result(self, pool: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        if len(raw_results) != 3:
            return None
        (ok0, data0), (ok1, data1), (okq, dataq) = raw_results
        if not ok0 or not ok1 or not data0 or not data1:
            return None
        try:
            (bi,) = eth_abi_decode(["uint256"], data0)
            (bj,) = eth_abi_decode(["uint256"], data1)
        except Exception:
            return None
        # The probe quote is allowed to fail independently (e.g. a paused
        # pool might still answer balances() but revert on get_dy()) --
        # reserves/TVL still update in that case, just not spot_rate.
        spot_rate = None
        if okq and dataq:
            try:
                (dy,) = eth_abi_decode(["uint256"], dataq)
                decimals = 18
                try:
                    from tvl import token_decimals
                    decimals = token_decimals(pool.token0 or "")
                except Exception:
                    pass
                probe = 10 ** max(1, decimals - 4)
                spot_rate = (dy / probe) if probe else None
            except Exception:
                spot_rate = None
        return {"r0": int(bi), "r1": int(bj), "token0": pool.token0, "token1": pool.token1,
                "i": int(pool.metadata["i"]), "j": int(pool.metadata["j"]),
                "spot_rate": spot_rate,
                "tvl_usd": pool.metadata.get("tvl_usd", 0), "protocol": self.protocol}

    # ── Batched discovery ──────────────────────────────────────────────────
    # discover_from_event's Curve branch already trusts the (i, j) indices
    # decoded straight from the TokenExchange event's topics (there is no
    # separate factory to verify a Curve pool address against — the pool
    # contract itself, at the address the event came from, is the source of
    # truth for what coins(i)/coins(j) are) — so one round of coins(i)/
    # coins(j) is both necessary and sufficient; no verification round.

    def supports_batched_discovery(self) -> bool:
        return True

    def build_discovery_calls(self, candidate: PoolRef) -> list[dict]:
        i, j = int(candidate.metadata["i"]), int(candidate.metadata["j"])
        c = self._pool_contract(candidate.address)
        target = self.w3.to_checksum_address(candidate.address)
        return [
            {"target": target, "allowFailure": True, "callData": encode_call(c, "coins", [i])},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "coins", [j])},
        ]

    def decode_discovery_result(self, candidate: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        if len(raw_results) != 2:
            return None
        (ok0, data0), (ok1, data1) = raw_results
        if not ok0 or not ok1 or not data0 or not data1:
            return None
        try:
            (token0,) = eth_abi_decode(["address"], data0)
            (token1,) = eth_abi_decode(["address"], data1)
        except Exception:
            return None
        return {"token0": token0.lower(), "token1": token1.lower(),
                "i": int(candidate.metadata["i"]), "j": int(candidate.metadata["j"]),
                "protocol": self.protocol, "event_discovered": True}
