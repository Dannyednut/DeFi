from __future__ import annotations

import os
from typing import Any

import httpx
from eth_abi import decode as eth_abi_decode

from .base import ProtocolAdapter
from .models import PoolRef, Quote
from utils.web3_compat import encode_call

ZERO = "0x0000000000000000000000000000000000000000"

VAULT_ABI = [
    {
        "name": "getPoolTokens", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "outputs": [
            {"name": "tokens", "type": "address[]"},
            {"name": "balances", "type": "uint256[]"},
            {"name": "lastChangeBlock", "type": "uint256"},
        ],
    },
    {
        "name": "queryBatchSwap", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "kind", "type": "uint8"},
            {"name": "swaps", "type": "tuple[]", "components": [
                {"name": "poolId", "type": "bytes32"},
                {"name": "assetInIndex", "type": "uint256"},
                {"name": "assetOutIndex", "type": "uint256"},
                {"name": "amount", "type": "uint256"},
                {"name": "userData", "type": "bytes"},
            ]},
            {"name": "assets", "type": "address[]"},
            {"name": "funds", "type": "tuple", "components": [
                {"name": "sender", "type": "address"},
                {"name": "fromInternalBalance", "type": "bool"},
                {"name": "recipient", "type": "address"},
                {"name": "toInternalBalance", "type": "bool"},
            ]},
        ],
        "outputs": [{"name": "assetDeltas", "type": "int256[]"}],
    },
]

POOL_ID_ABI = [
    {"name": "getPoolId", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "bytes32"}]},
]


class BalancerV2Adapter(ProtocolAdapter):
    """Balancer V2 pool adapter.

    Discovery can be supplied by the Balancer API or by seeded PoolRefs. Exact
    quotes use Vault.queryBatchSwap, so weighted/stable/other pool math stays
    inside the canonical pool implementation instead of being reimplemented.
    """

    protocol = "balancer_v2"

    def _vault_address(self) -> str:
        return self.config.router or os.getenv(f"BALANCER_V2_VAULT_{self.config.chain_id}", "")

    def _vault(self):
        address = self._vault_address()
        if not address:
            raise RuntimeError("Balancer V2 Vault address is not configured")
        return self.w3.eth.contract(address=self.w3.to_checksum_address(address), abi=VAULT_ABI)

    def discover_pair(self, token_in: str, token_out: str) -> list[PoolRef]:
        # API discovery is intentionally optional. Seeded/indexed pools are the
        # deterministic path; on-demand API discovery is provided by discover_all.
        return []

    async def discover_all(self, min_tvl_usd: float = 25_000) -> list[PoolRef]:
        url = os.getenv("BALANCER_API_URL", "https://api-v3.balancer.fi/graphql")
        query = """
        query Pools($chain: GqlChain!, $minTvl: Float!) {
          poolGetPools(where: {chainIn: [$chain], minTvl: $minTvl}, first: 1000) {
            id address name dynamicData { totalLiquidity } poolTokens { address weight }
          }
        }
        """
        chain_map = {1: "MAINNET", 137: "POLYGON", 42161: "ARBITRUM", 8453: "BASE"}
        chain = chain_map.get(self.config.chain_id)
        if not chain:
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(url, json={"query": query, "variables": {"chain": chain, "minTvl": min_tvl_usd}})
                r.raise_for_status()
                payload = r.json()
        except Exception:
            return []

        rows = (((payload.get("data") or {}).get("poolGetPools")) or [])
        out: list[PoolRef] = []
        for row in rows:
            pool_id = str(row.get("id") or "")
            address = str(row.get("address") or "")
            token_rows = row.get("poolTokens") or []
            tokens = [str(x.get("address") if isinstance(x, dict) else x).lower() for x in token_rows]
            weights = [float(x.get("weight") or 0) if isinstance(x, dict) else 0 for x in token_rows]
            tvl = float(((row.get("dynamicData") or {}).get("totalLiquidity")) or 0)
            if not pool_id or not address or len(tokens) < 2 or tvl < min_tvl_usd:
                continue
            for i, a in enumerate(tokens):
                for j, b in enumerate(tokens):
                    if i != j:
                        out.append(PoolRef(
                            address.lower(), self.protocol, self.config.name, a, b, 0,
                            {"pool_id": pool_id, "coins": tokens, "tvl_usd": tvl, "name": row.get("name"), "weights": weights, "swap_fee": float(((row.get("dynamicData") or {}).get("swapFee")) or 0)},
                        ))
        return out

    def _pool_id(self, pool: PoolRef) -> bytes:
        raw = pool.metadata.get("pool_id")
        if isinstance(raw, bytes):
            return raw
        if not raw:
            c = self.w3.eth.contract(address=self.w3.to_checksum_address(pool.address), abi=POOL_ID_ABI)
            return c.functions.getPoolId().call()
        return bytes.fromhex(str(raw).removeprefix("0x"))

    def load_pool(self, pool: PoolRef) -> dict[str, Any] | None:
        try:
            tokens, balances, last_block = self._vault().functions.getPoolTokens(self._pool_id(pool)).call()
            tokens = [x.lower() for x in tokens]
            return {
                "tokens": tokens,
                "balances": [int(x) for x in balances],
                "last_change_block": int(last_block),
                "token0": pool.token0,
                "token1": pool.token1,
                "protocol": self.protocol,
                "tvl_usd": pool.metadata.get("tvl_usd", 0),
            }
        except Exception:
            return None

    def quote_exact_in(self, pool: PoolRef, token_in: str, amount_in: int) -> Quote | None:
        if amount_in <= 0:
            return None
        try:
            state = self.load_pool(pool)
            if not state:
                return None
            tokens = state["tokens"]
            token_in = token_in.lower()
            token_out = pool.token1.lower() if pool.token0.lower() == token_in else pool.token0.lower()
            if token_in not in tokens or token_out not in tokens:
                return None
            i, j = tokens.index(token_in), tokens.index(token_out)
            swap = (self._pool_id(pool), i, j, int(amount_in), b"")
            zero = self.w3.to_checksum_address(ZERO)
            funds = (zero, False, zero, False)
            deltas = self._vault().functions.queryBatchSwap(0, [swap], [self.w3.to_checksum_address(x) for x in tokens], funds).call()
            # queryBatchSwap returns positive input delta and negative output delta.
            amount_out = -int(deltas[j])
            if amount_out <= 0:
                return None
            return Quote(amount_in, amount_out, self.protocol, pool.address, 0, {"pool_id": pool.metadata.get("pool_id")})
        except Exception:
            return None

    def supports_graph_reserves(self) -> bool:
        return True

    # ── Batched refresh ──────────────────────────────────────────────────────
    # A single getPoolTokens(poolId) call is all a refresh needs — pool_id is
    # already known from PoolRef.metadata once the pool has been loaded once.

    def supports_batched_refresh(self) -> bool:
        return True

    def build_refresh_calls(self, pool: PoolRef) -> list[dict]:
        try:
            pool_id = self._pool_id(pool)
        except Exception:
            return []
        vault_address = self._vault_address()
        if not vault_address:
            return []
        return [{
            "target": self.w3.to_checksum_address(vault_address),
            "allowFailure": True,
            "callData": encode_call(self._vault(), "getPoolTokens", [pool_id]),
        }]

    def decode_refresh_result(self, pool: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        if len(raw_results) != 1:
            return None
        ok, data = raw_results[0]
        if not ok or not data:
            return None
        try:
            tokens, balances, last_block = eth_abi_decode(["address[]", "uint256[]", "uint256"], data)
        except Exception:
            return None
        return {
            "tokens": [t.lower() for t in tokens],
            "balances": [int(b) for b in balances],
            "last_change_block": int(last_block),
            "token0": pool.token0,
            "token1": pool.token1,
            "protocol": self.protocol,
            "tvl_usd": pool.metadata.get("tvl_usd", 0),
        }

    # ── Batched discovery ────────────────────────────────────────────────────
    # Balancer discovery needs exactly the same call as a refresh —
    # getPoolTokens(poolId) both confirms the pool exists (the Vault reverts
    # for an unknown poolId) and returns every token+balance in one shot — so
    # discovery just reuses build_refresh_calls/decode_refresh_result
    # directly rather than duplicating them. No separate verification round.

    def supports_batched_discovery(self) -> bool:
        return True

    def build_discovery_calls(self, candidate: PoolRef) -> list[dict]:
        return self.build_refresh_calls(candidate)

    def decode_discovery_result(self, candidate: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        state = self.decode_refresh_result(candidate, raw_results)
        if not state or len(state.get("tokens", [])) < 2:
            return None
        state["event_discovered"] = True
        return state
