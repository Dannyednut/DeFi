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


# DSS-WO-MI-106 WS-A: reconciled against the provider's own documentation
# (alchemy.com/docs/reference/alchemy-pendingtransactions, retrieved 2026-11):
# the filtered ``alchemy_pendingTransactions`` subscription is documented as
# supported on ETH Mainnet, ETH Sepolia and Matic (Polygon) Mainnet ONLY. The
# previous set also listed Arbitrum (42161) and Optimism (10), which the
# documentation does NOT — there the filtered subscription is rejected with a
# JSON-RPC error, which the old watcher silently swallowed (it logged
# "Subscribed" before any acknowledgement and treated the error response as
# noise), producing a connected-but-permanently-silent stream. Chains outside
# the documented set fall back to the standard ``newPendingTransactions``
# subscription, which every JSON-RPC node supports.
ALCHEMY_FILTERED_PENDING_CHAINS = frozenset({1, 11155111, 137})

# Documented filter limit: "A maximum of 1000 addresses can be added in the
# addresses filter" — 1k UNIQUE addresses COMBINED across fromAddress and
# toAddress. The watcher therefore snapshots at most this many addresses into
# ``toAddress`` and records a truncation when the configured set exceeds it.
ALCHEMY_FILTER_ADDRESS_LIMIT = 1000

# The provider documents NO way to modify a subscription's address filter after
# it is established — the only lifecycle operation is unsubscribe/resubscribe.
# The watcher therefore treats the server-side filter as FIXED at subscribe
# time (the configured router set, stable for the process lifetime) and keeps
# dynamically learned addresses as LOCAL acceptance filters only.
ALCHEMY_FILTER_FIXED_AT_SUBSCRIBE = True


def subscription_for(chain_id: int, *, prefer_filtered: bool = True) -> str:
    if prefer_filtered and int(chain_id) in ALCHEMY_FILTERED_PENDING_CHAINS:
        return "alchemy_pendingTransactions"
    return "newPendingTransactions"


def build_subscription_params(chain_id: int, targets: set[str], *, prefer_filtered: bool = True) -> tuple[str, list]:
    """Return (subscription, params) for an eth_subscribe request.

    DSS-WO-MI-106 WS-A: the filter shape follows the provider documentation —
    ``{"toAddress": [...], "hashesOnly": false}`` — and is capped at the
    documented 1000-address limit (truncation is reported by the caller's
    counters, never applied silently). Full transaction objects are requested
    only for the narrow target set, so ``hashesOnly=false`` avoids paying an
    ``eth_getTransactionByHash`` round-trip per notification.
    """
    subscription = subscription_for(chain_id, prefer_filtered=prefer_filtered)
    params: list = [subscription]
    if subscription == "alchemy_pendingTransactions":
        chosen = sorted({a.lower() for a in targets if a})[:ALCHEMY_FILTER_ADDRESS_LIMIT]
        params.append({"toAddress": chosen, "hashesOnly": False})
    return subscription, params


def build_streams(chain_id: int, ws_urls: list[str], *, prefer_filtered: bool = True) -> list[PendingStreamConfig]:
    sub = subscription_for(chain_id, prefer_filtered=prefer_filtered)
    return [
        PendingStreamConfig(chain_id=int(chain_id), name=f"ws-{i}", url=url, subscription=sub, priority=i)
        for i, url in enumerate(ws_urls)
        if url
    ]
