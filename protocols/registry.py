from __future__ import annotations

from .base import ProtocolAdapter
from .models import ProtocolKind
from .uniswap_v2 import UniswapV2Adapter
from .uniswap_v3 import UniswapV3Adapter
from .aerodrome import AerodromeV2Adapter
from .curve import CurveAdapter
from .syncswap import SyncSwapAdapter
from .balancer import BalancerV2Adapter


class ProtocolRegistry:
    """Builds adapters from DexConfig instead of scattering protocol checks."""

    _FACTORIES = {
        ProtocolKind.UNISWAP_V2.value: UniswapV2Adapter,
        ProtocolKind.UNISWAP_V3.value: UniswapV3Adapter,
        ProtocolKind.AERODROME_V2.value: AerodromeV2Adapter,
        ProtocolKind.CURVE.value: CurveAdapter,
        ProtocolKind.SYNC_SWAP.value: SyncSwapAdapter,
        ProtocolKind.BALANCER.value: BalancerV2Adapter,
    }

    def __init__(self, w3):
        self.w3 = w3
        self._adapters: dict[str, ProtocolAdapter] = {}

    def get(self, dex_config) -> ProtocolAdapter | None:
        if getattr(dex_config, "chain_id", 0) != getattr(self.w3.eth, "chain_id", dex_config.chain_id):
            raise ValueError(f"DEX {dex_config.name} belongs to chain {dex_config.chain_id}, RPC is {self.w3.eth.chain_id}")
        if not dex_config.enabled:
            return None
        key = dex_config.name.lower()
        if key not in self._adapters:
            cls = self._FACTORIES.get(dex_config.protocol)
            if not cls:
                return None
            self._adapters[key] = cls(self.w3, dex_config)
        return self._adapters[key]

    def all(self, chain) -> list[ProtocolAdapter]:
        return [a for d in chain.dexes if (a := self.get(d)) is not None]
