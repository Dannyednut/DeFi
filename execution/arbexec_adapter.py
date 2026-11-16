from __future__ import annotations

"""Adapter for the gas-optimized native ``ArbExec.swap`` execution path.

RESEARCH-ONLY (DSS-WO-ENG-017 §3G): nothing in the production runtime
imports this module. It is retained as the route-IR → Exec ``Data``
translation surface for future multi-protocol execution research.
Compiling or adapting a route here does NOT make that protocol executable
through the deployed Exec.sol, which executes Uniswap-V2-compatible pairs
and Uniswap-V3 pools only (contracts/Exec.sol ``_swap``) and rejects any
route with fewer than two pools (``tokensLen <= 2``).

The deployed ArbExec is deliberately narrower than the protocol-neutral route IR:
it executes Uniswap-V2-compatible pools and Uniswap-V3 pools directly and sources
capital from Balancer or the first pool's flash swap.  This adapter therefore
rejects routes that the contract cannot execute faithfully instead of silently
falling back to generic router calls.

Known contract-fidelity limits (documented, not hidden):
  * the pinned Exec.sol computes V2 outputs with the FIXED 997/1000 fee
    factor (contracts/libraries/Helper.sol ``getAmountOutV2``); the ``fees[]``
    pips are NOT consumed for V2 math on the executed path. A 25 bps fork
    pool would therefore be executed at 30 bps math — the per-leg fee check
    below admits 25 bps for route-shape purposes only, and any real
    execution of such a route would need its economics re-derived at 30 bps.
  * Exec.sol reverts any route with ``tokens.length <= 2`` (fewer than two
    pools); this adapter enforces the same floor explicitly.
"""

from dataclasses import dataclass
from typing import Any

from .route_ir import ExecutionKind, ExecutionPlan


@dataclass(frozen=True)
class ArbExecRoute:
    """The exact fields required by ``Exec.swap``."""

    amount_in: int
    min_profit: int
    tokens: tuple[str, ...]
    pools: tuple[str, ...]
    fees: tuple[int, ...]
    token_in: str
    mode: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "amountIn": self.amount_in,
            "minProfit": self.min_profit,
            "tokens": list(self.tokens),
            "pools": list(self.pools),
            "fees": list(self.fees),
            "tokenIn": self.token_in,
            "mode": self.mode,
        }


def _cs(w3, address: str) -> str:
    return w3.to_checksum_address(address)


def build_arbexec_route(
    plan: ExecutionPlan,
    w3,
    *,
    mode: int,
    borrowed_amount: int | None = None,
    min_profit: int | None = None,
) -> ArbExecRoute:
    """Translate an ``ExecutionPlan`` into native ArbExec ``Data``.

    ``mode=0`` borrows ``tokenIn`` from Balancer. ``mode=1`` borrows the token
    on the far side of the first pool, so callers must supply the borrowed
    amount in that token (the existing lender flow already computes it).
    """
    if mode not in (0, 1):
        raise ValueError("ArbExec mode must be 0 or 1")
    if plan.chain_id != int(w3.eth.chain_id):
        raise ValueError(f"chain mismatch: plan={plan.chain_id}, rpc={w3.eth.chain_id}")
    if not plan.legs:
        raise ValueError("empty route")

    tokens = [_cs(w3, plan.token_in)]
    pools: list[str] = []
    fees: list[int] = []

    expected = plan.token_in.lower()
    for i, leg in enumerate(plan.legs):
        if leg.kind not in (ExecutionKind.V2_SWAP, ExecutionKind.V3_SWAP):
            raise ValueError(
                f"ArbExec cannot execute {leg.kind}; use protocol-native execution for this route"
            )

        if leg.token_in.lower() != expected:
            raise ValueError(f"non-contiguous route at leg {i}: expected {expected}, got {leg.token_in}")

        # NOTE (DSS-WO-ENG-017 §3G): the pinned Exec.sol computes V2 outputs
        # with the FIXED 997/1000 fee factor and does NOT consume the V2 fee
        # pips on the executed path, so a 25 bps leg admitted here would be
        # executed at 30 bps math. This admission is route-shape research
        # surface only — see the module docstring.
        if leg.kind == ExecutionKind.V2_SWAP and leg.protocol.lower() not in (
            "uniswap_v2", "sushiswap_v2", "pancakeswap_v2", "ringswap_v2"
        ):
            raise ValueError(f"ArbExec V2 leg {i} uses unsupported protocol={leg.protocol}")

        fee_bps = int(leg.fee_bps or 30)
        if leg.kind == ExecutionKind.V2_SWAP and fee_bps not in (25, 30):
            raise ValueError(f"ArbExec V2 leg {i} has unsupported fee_bps={fee_bps}")

        pools.append(_cs(w3, leg.pool))
        tokens.append(_cs(w3, leg.token_out))
        if leg.kind == ExecutionKind.V2_SWAP:
            fee_pip = fee_bps * 100
        else:
            fee_pip = int(leg.metadata.get("fee", leg.metadata.get("fee_pip", leg.fee_bps * 100)) or 0)
        fees.append(fee_pip)
        expected = leg.token_out.lower()

    if expected != plan.token_in.lower():
        raise ValueError("ArbExec route must close back to tokenIn")
    if len(pools) < 2:
        # Exec.swap reverts any Data with tokens.length <= 2 (contracts/Exec.sol
        # BadPath guard) — a single-pool route is not executable.
        raise ValueError("ArbExec route requires at least two pools (Exec.sol BadPath)")
    if len(pools) != len(tokens) - 1 or len(fees) != len(pools):
        raise ValueError("invalid ArbExec route lengths")

    amount = int(plan.amount_in if borrowed_amount is None else borrowed_amount)
    if amount <= 0:
        raise ValueError("ArbExec amount must be positive")

    return ArbExecRoute(
        amount_in=amount,
        min_profit=int(plan.min_profit if min_profit is None else min_profit),
        tokens=tuple(tokens),
        pools=tuple(pools),
        fees=tuple(fees),
        token_in=_cs(w3, plan.token_in),
        mode=mode,
    )
