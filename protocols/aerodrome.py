from __future__ import annotations

from eth_abi import decode as eth_abi_decode

from .base import ProtocolAdapter
from .models import PoolRef, Quote
from config import UNISWAP_V2_PAIR_ABI
from utils.web3_compat import encode_call


class AerodromeV2Adapter(ProtocolAdapter):
    """Aerodrome Slipstream-era V2/volatile-stable pool adapter.

    Aerodrome's factory is not a vanilla Uniswap V2 factory: a pair is keyed by
    (tokenA, tokenB, stable).  We therefore discover both stable and volatile
    pools instead of incorrectly calling getPair().
    """
    protocol = "aerodrome_v2"

    FACTORY_ABI = [
        {"name": "getPool", "type": "function", "stateMutability": "view",
         "inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}, {"name": "stable", "type": "bool"}],
         "outputs": [{"name": "pool", "type": "address"}]},
    ]

    def _factory(self):
        return self.w3.eth.contract(address=self.w3.to_checksum_address(self.config.factory), abi=self.FACTORY_ABI)

    def discover_pair(self, token_in: str, token_out: str) -> list[PoolRef]:
        f = self._factory()
        out: list[PoolRef] = []
        for stable in (False, True):
            try:
                addr = f.functions.getPool(
                    self.w3.to_checksum_address(token_in),
                    self.w3.to_checksum_address(token_out), stable,
                ).call()
                if addr and int(addr, 16) != 0:
                    out.append(PoolRef(addr.lower(), self.config.protocol, self.config.name,
                                       token_in.lower(), token_out.lower(), self.config.fee_bps,
                                       {"stable": stable}))
            except Exception:
                continue
        return out

    def load_pool(self, pool: PoolRef) -> dict | None:
        pool_abi = UNISWAP_V2_PAIR_ABI + [{"name": "getAmountOut", "type": "function", "stateMutability": "view", "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "tokenIn", "type": "address"}], "outputs": [{"name": "amountOut", "type": "uint256"}]}]
        c = self.w3.eth.contract(address=self.w3.to_checksum_address(pool.address), abi=pool_abi)
        token0 = c.functions.token0().call().lower()
        token1 = c.functions.token1().call().lower()
        r0, r1, _ = c.functions.getReserves().call()
        if not r0 or not r1:
            return None
        return {"token0": token0, "token1": token1, "r0": r0, "r1": r1,
                "fee_bps": self.config.fee_bps, "protocol": self.protocol,
                "stable": pool.metadata.get("stable", False)}

    def quote_exact_in(self, pool: PoolRef, token_in: str, amount_in: int) -> Quote | None:
        s = self.load_pool(pool)
        if not s:
            return None
        try:
            pool_abi = [{"name": "getAmountOut", "type": "function", "stateMutability": "view", "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "tokenIn", "type": "address"}], "outputs": [{"name": "amountOut", "type": "uint256"}]}]
            c = self.w3.eth.contract(address=self.w3.to_checksum_address(pool.address), abi=pool_abi)
            out = int(c.functions.getAmountOut(int(amount_in), self.w3.to_checksum_address(token_in)).call())
            return Quote(amount_in, out, self.protocol, pool.address, s["fee_bps"], {"stable": s["stable"]})
        except Exception:
            # Fallback is only for old deployments without getAmountOut.
            rin, rout = ((s["r0"], s["r1"]) if token_in.lower() == s["token0"] else (s["r1"], s["r0"]))
            fee = 10_000 - s["fee_bps"]
            out = amount_in * fee * rout // (rin * 10_000 + amount_in * fee)
            return Quote(amount_in, out, self.protocol, pool.address, s["fee_bps"], {"stable": s["stable"]})

    def supports_graph_reserves(self) -> bool:
        return True

    # ── Batched refresh ──────────────────────────────────────────────────────
    # A refresh only needs getReserves() — token0/token1/stable-flag don't
    # change and are already known from the graph's cached PoolRef.

    def supports_batched_refresh(self) -> bool:
        return True

    def build_refresh_calls(self, pool: PoolRef) -> list[dict]:
        c = self.w3.eth.contract(address=self.w3.to_checksum_address(pool.address), abi=UNISWAP_V2_PAIR_ABI)
        target = self.w3.to_checksum_address(pool.address)
        calls = [{"target": target, "allowFailure": True, "callData": encode_call(c, "getReserves")}]
        # Aerodrome "stable" pools use a Solidly-style StableSwap invariant
        # (x^3*y + x*y^3 = k), not plain constant-product — their true spot
        # price is not reserve1/reserve0 the way a volatile pool's is. Add a
        # small probe getAmountOut() call for stable pools only, in the SAME
        # batch, so exchange_rate (graph.py) can price them correctly instead
        # of falling back to the (wrong, for a stable pool) reserve-ratio
        # formula. Volatile pools don't need this — reserve ratio is already
        # their exact spot price.
        if pool.metadata.get("stable"):
            amount_out_abi = [{"name": "getAmountOut", "type": "function", "stateMutability": "view",
                                "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "tokenIn", "type": "address"}],
                                "outputs": [{"name": "amountOut", "type": "uint256"}]}]
            c2 = self.w3.eth.contract(address=target, abi=amount_out_abi)
            decimals = 18
            try:
                from tvl import token_decimals
                decimals = token_decimals(pool.token0 or "")
            except Exception:
                pass
            probe = 10 ** max(1, decimals - 4)
            token_in = self.w3.to_checksum_address(pool.token0 or "")
            calls.append({"target": target, "allowFailure": True,
                           "callData": encode_call(c2, "getAmountOut", [probe, token_in])})
        return calls

    def decode_refresh_result(self, pool: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict | None:
        if len(raw_results) not in (1, 2):
            return None
        ok, data = raw_results[0]
        if not ok or not data:
            return None
        try:
            r0, r1, _ts = eth_abi_decode(["uint112", "uint112", "uint32"], data)
        except Exception:
            return None
        if not r0 or not r1:
            return None
        result = {"token0": pool.token0, "token1": pool.token1, "r0": int(r0), "r1": int(r1),
                  "fee_bps": self.config.fee_bps, "protocol": self.protocol,
                  "stable": pool.metadata.get("stable", False)}
        if len(raw_results) == 2:
            okq, dataq = raw_results[1]
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
                    result["spot_rate"] = (dy / probe) if probe else None
                except Exception:
                    pass
        return result

    # ── Batched discovery ────────────────────────────────────────────────────
    # Stage 1: read token0()/token1()/getReserves() off the candidate.
    # Stage 2: Aerodrome pools are keyed by (tokenA, tokenB, stable) — try
    # both stable=False and stable=True against the factory in the SAME
    # multicall batch and accept whichever (if either) returns this address.

    def supports_batched_discovery(self) -> bool:
        return True

    def build_discovery_calls(self, candidate: PoolRef) -> list[dict]:
        c = self.w3.eth.contract(address=self.w3.to_checksum_address(candidate.address), abi=UNISWAP_V2_PAIR_ABI)
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
            r0, r1, _ts = eth_abi_decode(["uint112", "uint112", "uint32"], d2)
        except Exception:
            return None
        if not r0 or not r1:
            return None
        return {"token0": token0.lower(), "token1": token1.lower(), "r0": int(r0), "r1": int(r1),
                "fee_bps": self.config.fee_bps, "protocol": self.protocol}

    def needs_discovery_verification(self) -> bool:
        return True

    def build_discovery_verification_calls(self, candidate: PoolRef, discovered: dict) -> list[dict]:
        f = self._factory()
        t0 = self.w3.to_checksum_address(discovered["token0"])
        t1 = self.w3.to_checksum_address(discovered["token1"])
        target = self.w3.to_checksum_address(self.config.factory)
        return [
            {"target": target, "allowFailure": True, "callData": encode_call(f, "getPool", [t0, t1, False])},
            {"target": target, "allowFailure": True, "callData": encode_call(f, "getPool", [t0, t1, True])},
        ]

    def decode_discovery_verification(self, candidate: PoolRef, discovered: dict, raw_results: list[tuple[bool, bytes]]) -> dict | None:
        if len(raw_results) != 2:
            return None
        for stable, (ok, data) in zip((False, True), raw_results):
            if not ok or not data:
                continue
            try:
                (pool_addr,) = eth_abi_decode(["address"], data)
            except Exception:
                continue
            if pool_addr.lower() == candidate.address.lower():
                discovered["stable"] = stable
                discovered["event_discovered"] = True
                return discovered
        return None

    # ── Batched pair lookup ──────────────────────────────────────────────────
    def supports_batched_pair_lookup(self) -> bool:
        return True

    def build_pair_lookup_calls(self, token0: str, token1: str) -> list[dict]:
        f = self._factory()
        t0 = self.w3.to_checksum_address(token0)
        t1 = self.w3.to_checksum_address(token1)
        target = self.w3.to_checksum_address(self.config.factory)
        return [
            {"target": target, "allowFailure": True, "callData": encode_call(f, "getPool", [t0, t1, False])},
            {"target": target, "allowFailure": True, "callData": encode_call(f, "getPool", [t0, t1, True])},
        ]

    def decode_pair_lookup(self, token0: str, token1: str, raw_results: list[tuple[bool, bytes]]) -> list[PoolRef]:
        if len(raw_results) != 2:
            return []
        out = []
        for stable, (ok, data) in zip((False, True), raw_results):
            if not ok or not data:
                continue
            try:
                (pool_addr,) = eth_abi_decode(["address"], data)
            except Exception:
                continue
            if int(pool_addr, 16) == 0:
                continue
            out.append(PoolRef(pool_addr.lower(), self.protocol, self.config.name, token0.lower(), token1.lower(),
                                self.config.fee_bps, {"stable": stable}))
        return out
