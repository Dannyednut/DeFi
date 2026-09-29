from __future__ import annotations

from eth_abi import decode as eth_abi_decode

from .base import ProtocolAdapter
from .models import PoolRef, Quote
from utils.web3_compat import encode_call

FACTORY_ABI = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}],
     "outputs": [{"name": "pool", "type": "address"}]},
]
POOL_ABI = [
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "reserve0", "type": "uint256"}, {"name": "reserve1", "type": "uint256"}]},
    {"name": "getAmountOut", "type": "function", "stateMutability": "view", "inputs": [{"name": "tokenIn", "type": "address"}, {"name": "amountIn", "type": "uint256"}, {"name": "sender", "type": "address"}], "outputs": [{"name": "amountOut", "type": "uint256"}]},
]

class SyncSwapAdapter(ProtocolAdapter):
    protocol = "syncswap"

    def discover_pair(self, token_in: str, token_out: str) -> list[PoolRef]:
        f = self.w3.eth.contract(address=self.w3.to_checksum_address(self.config.factory), abi=FACTORY_ABI)
        try:
            pool = f.functions.getPool(self.w3.to_checksum_address(token_in), self.w3.to_checksum_address(token_out)).call()
        except Exception:
            return []
        if not pool or int(pool, 16) == 0:
            return []
        return [PoolRef(pool.lower(), self.protocol, self.config.name, token_in.lower(), token_out.lower(), self.config.fee_bps)]

    def load_pool(self, pool: PoolRef) -> dict | None:
        try:
            c = self.w3.eth.contract(address=self.w3.to_checksum_address(pool.address), abi=POOL_ABI)
            t0, t1 = c.functions.token0().call().lower(), c.functions.token1().call().lower()
            r0, r1 = c.functions.getReserves().call()
            return {"token0": t0, "token1": t1, "r0": int(r0), "r1": int(r1), "protocol": self.protocol}
        except Exception:
            return None

    def quote_exact_in(self, pool: PoolRef, token_in: str, amount_in: int) -> Quote | None:
        try:
            c = self.w3.eth.contract(address=self.w3.to_checksum_address(pool.address), abi=POOL_ABI)
            # sender is only used by the fee manager; the zero address gives a
            # deterministic view quote for classic/stable pools.
            out = c.functions.getAmountOut(self.w3.to_checksum_address(token_in), int(amount_in), "0x0000000000000000000000000000000000000000").call()
            return Quote(amount_in, int(out), self.protocol, pool.address, 0)
        except Exception:
            return None

    def supports_graph_reserves(self) -> bool:
        return True

    # ── Batched refresh ──────────────────────────────────────────────────────
    # A refresh only needs getReserves() — token0/token1 don't change and are
    # already known from the graph's cached PoolRef by the time we refresh.

    def supports_batched_refresh(self) -> bool:
        return True

    def build_refresh_calls(self, pool: PoolRef) -> list[dict]:
        c = self.w3.eth.contract(address=self.w3.to_checksum_address(pool.address), abi=POOL_ABI)
        return [{
            "target": self.w3.to_checksum_address(pool.address),
            "allowFailure": True,
            "callData": encode_call(c, "getReserves"),
        }]

    def decode_refresh_result(self, pool: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict | None:
        if len(raw_results) != 1:
            return None
        ok, data = raw_results[0]
        if not ok or not data:
            return None
        try:
            r0, r1 = eth_abi_decode(["uint256", "uint256"], data)
        except Exception:
            return None
        return {"token0": pool.token0, "token1": pool.token1, "r0": int(r0), "r1": int(r1), "protocol": self.protocol}

    # ── Batched discovery ────────────────────────────────────────────────────
    # Stage 1: read token0()/token1()/getReserves() directly off the
    # candidate address (we don't know its tokens yet, unlike a refresh).
    # Stage 2: confirm factory.getPool(token0, token1) actually returns this
    # exact address — a candidate can expose the right function signatures
    # without genuinely being a SyncSwap-deployed pool.

    def supports_batched_discovery(self) -> bool:
        return True

    def build_discovery_calls(self, candidate: PoolRef) -> list[dict]:
        c = self.w3.eth.contract(address=self.w3.to_checksum_address(candidate.address), abi=POOL_ABI)
        target = self.w3.to_checksum_address(candidate.address)
        return [
            {"target": target, "allowFailure": True, "callData": encode_call(c, "token0")},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "token1")},
            {"target": target, "allowFailure": True, "callData": encode_call(c, "getReserves")},
        ]

    def decode_discovery_result(self, candidate: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict | None:
        if len(raw_results) != 3:
            return None
        (ok0, d0), (ok1, d1), (ok2, d2) = raw_results
        if not (ok0 and ok1 and ok2 and d0 and d1 and d2):
            return None
        try:
            (token0,) = eth_abi_decode(["address"], d0)
            (token1,) = eth_abi_decode(["address"], d1)
            r0, r1 = eth_abi_decode(["uint256", "uint256"], d2)
        except Exception:
            return None
        if not r0 or not r1:
            return None
        return {"token0": token0.lower(), "token1": token1.lower(), "r0": int(r0), "r1": int(r1), "protocol": self.protocol}

    def needs_discovery_verification(self) -> bool:
        return True

    def build_discovery_verification_calls(self, candidate: PoolRef, discovered: dict) -> list[dict]:
        f = self.w3.eth.contract(address=self.w3.to_checksum_address(self.config.factory), abi=FACTORY_ABI)
        return [{
            "target": self.w3.to_checksum_address(self.config.factory),
            "allowFailure": True,
            "callData": encode_call(f, "getPool", [
                self.w3.to_checksum_address(discovered["token0"]),
                self.w3.to_checksum_address(discovered["token1"]),
            ]),
        }]

    def decode_discovery_verification(self, candidate: PoolRef, discovered: dict, raw_results: list[tuple[bool, bytes]]) -> dict | None:
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
        f = self.w3.eth.contract(address=self.w3.to_checksum_address(self.config.factory), abi=FACTORY_ABI)
        return [{
            "target": self.w3.to_checksum_address(self.config.factory),
            "allowFailure": True,
            "callData": encode_call(f, "getPool", [
                self.w3.to_checksum_address(token0),
                self.w3.to_checksum_address(token1),
            ]),
        }]

    def decode_pair_lookup(self, token0: str, token1: str, raw_results: list[tuple[bool, bytes]]) -> list[PoolRef]:
        if len(raw_results) != 1:
            return []
        ok, data = raw_results[0]
        if not ok or not data:
            return []
        try:
            (pool_addr,) = eth_abi_decode(["address"], data)
        except Exception:
            return []
        if int(pool_addr, 16) == 0:
            return []
        return [PoolRef(pool_addr.lower(), self.protocol, self.config.name, token0.lower(), token1.lower(), self.config.fee_bps, {})]
