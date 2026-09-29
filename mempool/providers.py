"""Chain-aware pending-transaction stream providers.

A node's mempool is local; no single public RPC gives a complete global view.
This module therefore treats every stream as a partial observation and supports
multiple independent websocket sources per chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PendingStreamConfig:
    chain_id: int
    name: str
    url: str
    subscription: str = "newPendingTransactions"
    enabled: bool = True
    priority: int = 1


# Alchemy's current documentation lists Ethereum, Sepolia, Polygon, Arbitrum
# and Optimism for the filtered pending subscription. Keep this explicit because
# pending-stream support is provider/chain specific and never global.
ALCHEMY_FILTERED_PENDING_CHAINS = frozenset({1, 11155111, 137, 42161, 10})


def subscription_for(chain_id: int, *, prefer_filtered: bool = True) -> str:
    if prefer_filtered and int(chain_id) in ALCHEMY_FILTERED_PENDING_CHAINS:
        return "alchemy_pendingTransactions"
    return "newPendingTransactions"


def build_streams(chain_id: int, ws_urls: list[str], *, prefer_filtered: bool = True) -> list[PendingStreamConfig]:
    sub = subscription_for(chain_id, prefer_filtered=prefer_filtered)
    return [
        PendingStreamConfig(chain_id=int(chain_id), name=f"ws-{i}", url=url, subscription=sub, priority=i)
        for i, url in enumerate(ws_urls)
        if url
    ]
