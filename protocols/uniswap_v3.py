"""
protocols/uniswap_v3.py
=======================
UniswapV3Adapter — the piece missing from the original adapter set.

Previously there was no adapter for Uniswap V3 at all: pool loading was
hand-rolled in registry.py::_load_v3_pool, and live quoting in
graph.py::best_rate_for_amount used a crude linear spot-rate approximation
(amount_in * spot_rate) that ignores how liquidity changes across tick
boundaries entirely. quote_exact_in here instead walks the pool's actual
tick segments (optimizer.tick_data.get_tick_segments +
optimizer.tick_walk.quote_exact_in_from_segments), the same machinery
already used elsewhere in this codebase for two-pool optimal-size solving —
just applied to a single pool and a given amount.

Note: unlike the other adapters, supports_batched_refresh() is NOT
implemented here. V2/V3 refresh already has a dedicated, more efficient
Multicall3 fast-path (utils.multicall3.multicall3_bulk_refresh, driven by
block_information.py's ProtocolStateSynchronizer) that batches
slot0()+liquidity() directly without going through the generic adapter
plumbing built for Curve/Balancer/SyncSwap/Aerodrome — this adapter's
load_pool()/quote_exact_in() are for pool discovery and amount-aware
quoting, not the block-triggered refresh hot path.
"""
from __future__ import annotations

from typing import Any

from .base import ProtocolAdapter
from .models import PoolRef, Quote
from utils.web3_compat import encode_call


class UniswapV3Adapter(ProtocolAdapter):
    protocol = "uniswap_v3"

    def _pool_contract(self, address: str):
        from config import UNISWAP_V3_POOL_ABI
        return self.w3.eth.contract(address=self.w3.to_checksum_address(address), abi=UNISWAP_V3_POOL_ABI)

    def _factory_contract(self):
        from config import UNISWAP_V3_FACTORY_ABI
        return self.w3.eth.contract(address=self.w3.to_checksum_address(self.config.factory), abi=UNISWAP_V3_FACTORY_ABI)

    def discover_pair(self, token_in: str, token_out: str) -> list[PoolRef]:
        if not self.config.factory:
            return []
        factory = self._factory_contract()
        t0, t1 = self.w3.to_checksum_address(token_in), self.w3.to_checksum_address(token_out)
        fee_tiers = self.config.fee_tiers or (500, 3000, 10000)
        out = []
        for fee in fee_tiers:
            try:
                addr = factory.functions.getPool(t0, t1, int(fee)).call()
            except Exception:
                continue
            if int(addr, 16) == 0:
                continue
            out.append(PoolRef(addr.lower(), self.protocol, self.config.name,
                                token_in.lower(), token_out.lower(), int(fee // 100), {"fee_pips": int(fee)}))
        return out

    def load_pool(self, pool: PoolRef) -> dict[str, Any] | None:
        try:
            c = self._pool_contract(pool.address)
            token0, token1 = pool.token0, pool.token1
            if not token0 or not token1:
                token0 = c.functions.token0().call().lower()
                token1 = c.functions.token1().call().lower()
            slot0 = c.functions.slot0().call()
            liquidity = c.functions.liquidity().call()
            sqrt_price_x96 = slot0[0]
            if not sqrt_price_x96:
                return None
            return {
                "token0": token0.lower(), "token1": token1.lower(),
                "sqrt_price_x96": int(sqrt_price_x96), "liquidity": int(liquidity),
                "protocol": self.protocol,
            }
        except Exception:
            return None

    def quote_exact_in(self, pool: PoolRef, token_in: str, amount_in: int) -> Quote | None:
        try:
            from optimizer.tick_data import get_tick_segments
            from optimizer.tick_walk import quote_exact_in_from_segments
            fee_pips = pool.metadata.get("fee_pips") or int(pool.fee_bps) * 100
            segments = get_tick_segments(self.w3, pool.address, int(fee_pips), token_in)
            if not segments:
                return None
            out = quote_exact_in_from_segments(segments, float(amount_in), fee_pips / 1_000_000.0)
            if out <= 0:
                return None
            return Quote(int(amount_in), int(out), self.protocol, pool.address, fee_pips,
                         {"quote_mode": "tick_walk_exact"})
        except Exception:
            return None

    def supports_graph_reserves(self) -> bool:
        # PoolEdge.exchange_rate already derives a fee-adjusted spot price
        # directly from the graph's cached sqrt_price_x96/liquidity (kept
        # fresh by the dedicated V2/V3 Multicall3 refresh path) with no live
        # call needed — quote_exact_in above (amount-aware, tick-exact) is
        # only used once a candidate has already been found via that spot
        # price, matching the same two-tier design the other adapters use.
        return True

    # ── Batched discovery ────────────────────────────────────────────────────
    # Mirrors SyncSwap/Aerodrome's shape: introspect the candidate, then
    # verify against the factory across every configured fee tier.

    def supports_batched_discovery(self) -> bool:
        return True

    def build_discovery_calls(self, candidate: PoolRef) -> list[dict]:
        c = self._pool_contract(candidate.address)
        target = self.w3.to_checksum_address(candidate.address)
        return [
            {"target": target, "allowFailure": True, "callData": encode_call(c, "token0")},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "token1")},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "fee")},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "slot0")},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "liquidity")},
        ]

    def decode_discovery_result(self, candidate: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        from eth_abi import decode as eth_abi_decode
        if len(raw_results) != 5:
            return None
        (ok0, d0), (ok1, d1), (okf, df), (oks, ds), (okl, dl) = raw_results
        if not (ok0 and ok1 and okf and oks and okl and d0 and d1 and df and ds and dl):
            return None
        try:
            (token0,) = eth_abi_decode(["address"], d0)
            (token1,) = eth_abi_decode(["address"], d1)
            (fee,) = eth_abi_decode(["uint24"], df)
            slot0 = eth_abi_decode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], ds)
            (liquidity,) = eth_abi_decode(["uint128"], dl)
        except Exception:
            return None
        sqrt_price_x96 = int(slot0[0])
        if not sqrt_price_x96:
            return None
        return {
            "token0": token0.lower(), "token1": token1.lower(), "fee_pips": int(fee),
            "sqrt_price_x96": sqrt_price_x96, "liquidity": int(liquidity), "protocol": self.protocol,
        }

    def needs_discovery_verification(self) -> bool:
        return True

    def build_discovery_verification_calls(self, candidate: PoolRef, discovered: dict[str, Any]) -> list[dict]:
        f = self._factory_contract()
        target = self.w3.to_checksum_address(self.config.factory)
        return [{
            "target": target, "allowFailure": True,
            "callData": encode_call(f, "getPool", [
                self.w3.to_checksum_address(discovered["token0"]),
                self.w3.to_checksum_address(discovered["token1"]),
                int(discovered["fee_pips"]),
            ]),
        }]

    def decode_discovery_verification(self, candidate: PoolRef, discovered: dict[str, Any], raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        from eth_abi import decode as eth_abi_decode
        if len(raw_results) != 1:
            return None
        ok, data = raw_results[0]
        if not ok or not data:
            return None
        try:
            (pool_addr,) = eth_abi_decode(["address"], data)
        except Exception:
            return None
        if pool_addr.lower() != candidate.address.lower():
            return None
        discovered["event_discovered"] = True
        return discovered

    # ── Batched pair lookup ──────────────────────────────────────────────────
    def supports_batched_pair_lookup(self) -> bool:
        return True

    def build_pair_lookup_calls(self, token0: str, token1: str) -> list[dict]:
        f = self._factory_contract()
        t0, t1 = self.w3.to_checksum_address(token0), self.w3.to_checksum_address(token1)
        target = self.w3.to_checksum_address(self.config.factory)
        fee_tiers = self.config.fee_tiers or (500, 3000, 10000)
        return [
            {"target": target, "allowFailure": True, "callData": encode_call(f, "getPool", [t0, t1, int(fee)])}
            for fee in fee_tiers
        ]

    def decode_pair_lookup(self, token0: str, token1: str, raw_results: list[tuple[bool, bytes]]) -> list[PoolRef]:
        from eth_abi import decode as eth_abi_decode
        fee_tiers = self.config.fee_tiers or (500, 3000, 10000)
        if len(raw_results) != len(fee_tiers):
            return []
        out = []
        for fee, (ok, data) in zip(fee_tiers, raw_results):
            if not ok or not data:
                continue
            try:
                (pool_addr,) = eth_abi_decode(["address"], data)
            except Exception:
                continue
            if int(pool_addr, 16) == 0:
                continue
            out.append(PoolRef(pool_addr.lower(), self.protocol, self.config.name, token0.lower(), token1.lower(),
                                int(fee // 100), {"fee_pips": int(fee)}))
        return out
