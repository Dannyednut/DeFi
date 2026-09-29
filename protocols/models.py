from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProtocolKind(Enum):
    UNISWAP_V2 = "uniswap_v2"
    UNISWAP_V3 = "uniswap_v3"
    AERODROME_V2 = "aerodrome_v2"
    CURVE = "curve"
    SYNC_SWAP = "syncswap"
    ALGEBRA = "algebra"
    BALANCER = "balancer_v2"


@dataclass(frozen=True)
class PoolRef:
    address: str
    protocol: str
    dex: str
    token0: str | None = None
    token1: str | None = None
    fee_bps: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Quote:
    amount_in: int
    amount_out: int
    protocol: str
    pool: str
    fee_bps: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
