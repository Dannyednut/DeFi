from __future__ import annotations

from .base import ProtocolAdapter
from .models import PoolRef, Quote
from config import UNISWAP_V2_FACTORY_ABI, UNISWAP_V2_PAIR_ABI

NULL = "0x0000000000000000000000000000000000000000"


class UniswapV2Adapter(ProtocolAdapter):
    protocol = "uniswap_v2"

    def _factory(self):
        return self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.config.factory),
            abi=UNISWAP_V2_FACTORY_ABI,
        )

    def discover_pair(self, token_in: str, token_out: str) -> list[PoolRef]:
        if not self.config.factory:
            return []
        addr = self._factory().functions.getPair(
            self.w3.to_checksum_address(token_in),
            self.w3.to_checksum_address(token_out),
        ).call()
        if not addr or addr.lower() == NULL:
            return []
        return [PoolRef(addr.lower(), self.config.protocol, self.config.name,
                        token_in.lower(), token_out.lower(), self.config.fee_bps)]

    def load_pool(self, pool: PoolRef) -> dict | None:
        c = self.w3.eth.contract(address=self.w3.to_checksum_address(pool.address), abi=UNISWAP_V2_PAIR_ABI)
        token0, token1 = pool.token0, pool.token1
        if not token0 or not token1:
            token0 = c.functions.token0().call().lower()
            token1 = c.functions.token1().call().lower()
        r0, r1, _ = c.functions.getReserves().call()
        if not r0 or not r1:
            return None
        return {"token0": token0.lower(), "token1": token1.lower(), "r0": r0, "r1": r1,
                "fee_bps": self.config.fee_bps, "protocol": self.config.protocol}

    def quote_exact_in(self, pool: PoolRef, token_in: str, amount_in: int) -> Quote | None:
        s = self.load_pool(pool)
        if not s or amount_in <= 0:
            return None
        if token_in.lower() == s["token0"]:
            rin, rout = s["r0"], s["r1"]
        else:
            rin, rout = s["r1"], s["r0"]
        fee = 10_000 - s["fee_bps"]
        out = amount_in * fee * rout // (rin * 10_000 + amount_in * fee)
        return Quote(amount_in, out, self.protocol, pool.address, s["fee_bps"])

    def supports_graph_reserves(self) -> bool:
        return True
