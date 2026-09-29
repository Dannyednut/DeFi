"""Version-compatibility shim for web3.py 6.x and 7.x.

web3.py v7 renamed/relocated several things this codebase depends on
(``geth_poa_middleware`` -> ``ExtraDataToPOAMiddleware``, the sync WebSocket
provider moved and was renamed, ``Contract.encodeABI`` -> ``Contract.encode_abi``).
Rather than pin the dependency to one exact version, every call site that
needs one of these goes through this module, so `requirements.txt` can allow
a range and CI can catch a real incompatibility instead of the whole app
failing to import.

Nothing here changes behavior on a given web3 version — it only picks the
correct underlying name/class for whichever version is installed.
"""
from __future__ import annotations

from typing import Any

import web3
from web3 import Web3

WEB3_MAJOR = int(web3.__version__.split(".")[0])

# ── PoA / extra-data middleware ──────────────────────────────────────────────
if WEB3_MAJOR >= 7:
    from web3.middleware import ExtraDataToPOAMiddleware as POA_MIDDLEWARE
else:  # pragma: no cover - exercised only when running against web3 6.x
    from web3.middleware import geth_poa_middleware as POA_MIDDLEWARE  # type: ignore[attr-defined]


def inject_poa_middleware(w3: Web3) -> None:
    """Idempotently inject the PoA/extra-data middleware onto ``w3``."""
    try:
        w3.middleware_onion.inject(POA_MIDDLEWARE, layer=0)
    except ValueError:
        pass  # already injected for this instance


# ── WebSocket provider (sync request/response, not persistent streaming) ────
# NOTE: this is intentionally the *synchronous* WS provider, used only for
# request/response RPC calls over a WS URL. web3.py v7's `subscribe()` /
# true persistent streaming requires `AsyncWeb3`, which this codebase does
# not use — real-time block/mempool notifications are handled separately via
# raw `websockets` connections (see mempool/watcher.py). Do not expect
# `w3.eth.subscribe(...)` to work through this provider on either version.
if WEB3_MAJOR >= 7:
    from web3.providers import LegacyWebSocketProvider as WSProvider
else:  # pragma: no cover - exercised only when running against web3 6.x
    from web3.providers import WebsocketProvider as WSProvider  # type: ignore[attr-defined]


def make_ws_provider(url: str, **kwargs: Any):
    return WSProvider(url, **kwargs)


def is_ws_provider(provider: Any) -> bool:
    return isinstance(provider, WSProvider)


# ── Contract calldata encoding ───────────────────────────────────────────────
def encode_call(contract, fn_name: str, args: list | None = None) -> str:
    """Return calldata hex string for ``fn_name(*args)`` on ``contract``.

    web3 v6 exposes ``Contract.encodeABI(fn_name=..., args=...)``; v7 renamed
    this to ``Contract.encode_abi(fn_name, args=...)``.
    """
    args = args or []
    if hasattr(contract, "encodeABI"):
        return contract.encodeABI(fn_name=fn_name, args=args)
    return contract.encode_abi(fn_name, args=args)
