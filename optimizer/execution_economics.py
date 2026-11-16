"""Phase 9 execution-economics and trade-size optimization.

Pure-Python, dependency-light components used before an opportunity reaches the
on-chain executor.  The optimizer deliberately separates:
  * quote optimization (trade size), and
  * execution economics (gas/loan/protocol/tip costs).

(Inclusion bidding — BidConfig/BidResult/BidOptimizer — was removed by
DSS-WO-ENG-016 §4 together with its only consumer, the retired
mev/inclusion.py policy wrapper: nothing in the canonical runtime path
selected tips through it. If economics-driven tip selection is wanted
later, it must be designed into the canonical executor path deliberately,
not preserved as an orphaned alternative.)

(DSS-WO-ENG-017 §4: the unused EconomicsInput dataclass and
ExecutionEconomics.from_input() constructor were removed with them —
no module ever constructed either. Callers construct ExecutionEconomics
directly, which also avoids from_input()'s hidden assumption that
gas_units × gas_price_wei share the unit of every other field: the
canonical enrichment (optimizer/opportunity_economics.py) converts the
native gas cost into the profit token's base units BEFORE construction.)

All integer amounts are in the route's native token units unless explicitly
stated otherwise. USD values are floats only at the presentation boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Callable, Iterable

QuoteFn = Callable[[int], int]


@dataclass(frozen=True)
class ExecutionEconomics:
    gross_profit_wei: int
    gas_cost_wei: int
    flashloan_fee_wei: int = 0
    protocol_cost_wei: int = 0
    builder_tip_wei: int = 0
    execution_buffer_wei: int = 0
    inclusion_probability: float = 1.0
    failure_cost_wei: int = 0

    @property
    def deterministic_net_wei(self) -> int:
        return (self.gross_profit_wei - self.gas_cost_wei - self.flashloan_fee_wei
                - self.protocol_cost_wei - self.builder_tip_wei - self.execution_buffer_wei)

    @property
    def expected_net_wei(self) -> float:
        return (self.inclusion_probability * self.deterministic_net_wei
                - (1.0 - self.inclusion_probability) * self.failure_cost_wei)

    @property
    def max_builder_tip_wei(self) -> int:
        return max(0, self.gross_profit_wei - self.gas_cost_wei
                   - self.flashloan_fee_wei - self.protocol_cost_wei - self.execution_buffer_wei)

    def profitable(self, minimum_profit_wei: int = 0) -> bool:
        return self.deterministic_net_wei >= int(minimum_profit_wei)


@dataclass(frozen=True)
class TradeSizePoint:
    amount_in: int
    amount_out: int
    gross_profit_wei: int
    gas_cost_wei: int
    flashloan_fee_wei: int
    protocol_cost_wei: int
    builder_tip_wei: int
    execution_buffer_wei: int
    net_profit_wei: int


@dataclass(frozen=True)
class TradeSizeResult:
    optimal_amount_in: int
    amount_out: int
    gross_profit_wei: int
    net_profit_wei: int
    points: tuple[TradeSizePoint, ...] = field(default_factory=tuple)
    approximate: bool = False


@dataclass(frozen=True)
class TradeSizeConfig:
    grid_points: int = 25
    refinement_rounds: int = 8
    min_improvement_wei: int = 0


class TradeSizeOptimizer:
    """Maximize net P&L for a single-route quote function.

    A coarse grid is evaluated first.  The best neighborhood is then refined
    by ternary search.  This is safer than assuming the entire profit curve is
    unimodal: the grid can select a local basin before refinement.
    """

    def __init__(self, config: TradeSizeConfig | None = None):
        self.config = config or TradeSizeConfig()

    def optimize(
        self,
        amount_min: int,
        amount_max: int,
        quote_fn: QuoteFn,
        *,
        gas_cost_fn: Callable[[int], int] | None = None,
        flashloan_fee_fn: Callable[[int], int] | None = None,
        protocol_cost_fn: Callable[[int], int] | None = None,
        builder_tip_fn: Callable[[int], int] | None = None,
        execution_buffer_fn: Callable[[int], int] | None = None,
    ) -> TradeSizeResult:
        lo, hi = max(1, int(amount_min)), max(1, int(amount_max))
        if hi < lo:
            lo, hi = hi, lo
        gas_cost_fn = gas_cost_fn or (lambda _: 0)
        flashloan_fee_fn = flashloan_fee_fn or (lambda _: 0)
        protocol_cost_fn = protocol_cost_fn or (lambda _: 0)
        builder_tip_fn = builder_tip_fn or (lambda _: 0)
        execution_buffer_fn = execution_buffer_fn or (lambda _: 0)

        cache: dict[int, TradeSizePoint] = {}

        def evaluate(amount: int) -> TradeSizePoint:
            amount = max(lo, min(hi, int(amount)))
            if amount in cache:
                return cache[amount]
            out = max(0, int(quote_fn(amount)))
            gross = out - amount
            point = TradeSizePoint(
                amount, out, gross,
                max(0, int(gas_cost_fn(amount))),
                max(0, int(flashloan_fee_fn(amount))),
                max(0, int(protocol_cost_fn(amount))),
                max(0, int(builder_tip_fn(amount))),
                max(0, int(execution_buffer_fn(amount))),
                gross - max(0, int(gas_cost_fn(amount)))
                - max(0, int(flashloan_fee_fn(amount)))
                - max(0, int(protocol_cost_fn(amount)))
                - max(0, int(builder_tip_fn(amount)))
                - max(0, int(execution_buffer_fn(amount))),
            )
            cache[amount] = point
            return point

        n = max(3, int(self.config.grid_points))
        if hi == lo:
            p = evaluate(lo)
            return TradeSizeResult(p.amount_in, p.amount_out, p.gross_profit_wei, p.net_profit_wei, (p,), False)

        step = (hi - lo) / (n - 1)
        grid = [evaluate(round(lo + i * step)) for i in range(n)]
        best = max(grid, key=lambda p: p.net_profit_wei)
        if best.net_profit_wei < self.config.min_improvement_wei:
            return TradeSizeResult(best.amount_in, best.amount_out, best.gross_profit_wei,
                                   best.net_profit_wei, tuple(grid), True)

        idx = min(range(len(grid)), key=lambda i: abs(grid[i].amount_in - best.amount_in))
        left = grid[max(0, idx - 1)].amount_in
        right = grid[min(len(grid) - 1, idx + 1)].amount_in
        for _ in range(max(0, int(self.config.refinement_rounds))):
            if right - left <= 2:
                break
            m1 = left + (right - left) // 3
            m2 = right - (right - left) // 3
            p1, p2 = evaluate(m1), evaluate(m2)
            if p1.net_profit_wei < p2.net_profit_wei:
                left = m1 + 1
            else:
                right = m2 - 1
        refined = [evaluate(x) for x in range(left, right + 1)] if right - left <= 256 else []
        candidates = grid + refined
        best = max(candidates, key=lambda p: p.net_profit_wei)
        return TradeSizeResult(best.amount_in, best.amount_out, best.gross_profit_wei,
                               best.net_profit_wei, tuple(sorted({p.amount_in: p for p in candidates}.values(), key=lambda p: p.amount_in)), True)
