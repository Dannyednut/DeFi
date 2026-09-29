from __future__ import annotations

"""Protocol-specific execution calldata builders.

These builders are deliberately side-effect free: they return CallSpec objects
that can be simulated, encoded, or submitted by a higher-level executor.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProtocolCall:
    target: str
    function: str
    args: tuple[Any, ...]
    value: int = 0


def curve_pool_exchange(pool: str, i: int, j: int, amount_in: int, min_out: int,
                        *, underlying: bool = False) -> ProtocolCall:
    return ProtocolCall(pool, "exchange_underlying" if underlying else "exchange",
                        (int(i), int(j), int(amount_in), int(min_out)))


def curve_router_ng(router: str, route: list[str], swap_params: list[list[int]],
                    amount_in: int, min_out: int, *, pools: list[str] | None = None,
                    receiver: str | None = None) -> ProtocolCall:
    """Build Curve Router NG's compact multi-hop call.

    Curve's router determines route/swap parameters off-chain and executes up to
    five swaps atomically. See the canonical Router.vy interface.
    """
    route5 = list(route)[:11]
    route5 += ["0x0000000000000000000000000000000000000000"] * (11 - len(route5))
    params = [list(x)[:5] + [0] * max(0, 5 - len(x)) for x in swap_params[:5]]
    params += [[0, 0, 0, 0, 0] for _ in range(5 - len(params))]
    pool_list = list(pools or [])[:5]
    pool_list += ["0x0000000000000000000000000000000000000000"] * (5 - len(pool_list))
    args = (route5, params, int(amount_in), int(min_out), pool_list,
            receiver or "0x0000000000000000000000000000000000000000")
    return ProtocolCall(router, "exchange", args)


def aerodrome_v2(router: str, amount_in: int, min_out: int, token_in: str,
                 token_out: str, stable: bool, factory: str, recipient: str,
                 deadline: int) -> ProtocolCall:
    route = (token_in, token_out, bool(stable), factory)
    return ProtocolCall(router, "swapExactTokensForTokens",
                        (int(amount_in), int(min_out), [route], recipient, int(deadline)))


def balancer_batch(vault: str, pool_id: bytes, asset_in_index: int,
                   asset_out_index: int, amount_in: int, assets: list[str],
                   funds: tuple, limits: list[int], deadline: int) -> ProtocolCall:
    step = (pool_id, int(asset_in_index), int(asset_out_index), int(amount_in), b"")
    return ProtocolCall(vault, "batchSwap",
                        (0, [step], assets, funds, limits, int(deadline)))


def syncswap(router: str, paths: list[Any], min_out: int, deadline: int) -> ProtocolCall:
    return ProtocolCall(router, "swap", (paths, int(min_out), int(deadline)))
