"""Split-route optimizer for parallel pools on a single chain.

The optimizer is quote-function driven: callers can supply exact protocol
quotes. A reserve-based fallback is available for V2-style edges, but results
are marked approximate so they must be revalidated by the exact simulator.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Iterable

from graph import PoolEdge

QuoteFn = Callable[[PoolEdge, int], int]

@dataclass(frozen=True)
class Allocation:
    pool: str
    dex: str
    protocol: str
    amount_in: int
    amount_out: int

@dataclass(frozen=True)
class SplitRouteResult:
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    allocations: tuple[Allocation, ...]
    approximate: bool
    # What a SINGLE pool alone would have produced for the FULL amount_in --
    # the correct baseline for "is splitting actually better than just using
    # the best single pool". This used to not exist; the only baseline
    # available to callers was improvement_over_best_single below, which
    # compared against the largest individual ALLOCATION within the split
    # (i.e. a pool that only received a fraction of amount_in) instead of
    # what that pool (or any pool) would produce with the WHOLE amount.
    # Since splitting inherently divides amount_in into smaller pieces, that
    # comparison made every split look like a massive (often ~100%)
    # improvement regardless of whether splitting actually helped at all.
    best_single_amount_out: int = 0

    @property
    def improvement_over_best_single(self) -> int:
        if not self.allocations:
            return 0
        return self.amount_out - self.best_single_amount_out


def v2_quote(edge: PoolEdge, amount_in: int) -> int:
    if amount_in <= 0 or edge.reserve_in <= 0 or edge.reserve_out <= 0:
        return 0
    fee = max(0, min(9_999, int(edge.fee_bps or 30)))
    ai = amount_in * (10_000 - fee)
    return (ai * edge.reserve_out) // (edge.reserve_in * 10_000 + ai)


def greedy_split(edges: Iterable[PoolEdge], amount_in: int, quote_fn: QuoteFn | None = None,
                 chunks: int = 32, min_chunk: int = 1) -> SplitRouteResult:
    edges = [e for e in edges if e.tvl_usd >= 0]
    if quote_fn is None:
        edges = [e for e in edges if e.reserve_in > 0 and e.reserve_out > 0]
    if not edges or amount_in <= 0:
        return SplitRouteResult("", "", amount_in, 0, tuple(), True, 0)
    quote_fn = quote_fn or v2_quote
    # The correct "should I even bother splitting" baseline: what the best
    # SINGLE pool produces for the FULL amount_in, using the same quote_fn
    # the split itself uses -- not the output of any one pool's fractional
    # allocation within the split (see the note on SplitRouteResult above).
    best_single_amount_out = max((quote_fn(e, amount_in) for e in edges), default=0)
    allocations = {e.pool_address.lower(): 0 for e in edges}
    outputs = {e.pool_address.lower(): 0 for e in edges}
    remaining = amount_in
    step = max(min_chunk, amount_in // max(1, chunks))
    approximate = quote_fn is v2_quote

    # Greedy marginal allocation. Re-quoting after each chunk naturally
    # accounts for nonlinear price impact on each pool.
    while remaining > 0:
        delta = min(step, remaining)
        best = None
        best_marginal = -1
        for e in edges:
            key = e.pool_address.lower()
            before = outputs[key]
            after = quote_fn(e, allocations[key] + delta)
            marginal = after - before
            if marginal > best_marginal:
                best_marginal = marginal
                best = e
        if best is None or best_marginal <= 0:
            break
        key = best.pool_address.lower()
        allocations[key] += delta
        outputs[key] = quote_fn(best, allocations[key])
        remaining -= delta

    chosen = []
    for e in edges:
        key = e.pool_address.lower()
        if allocations[key] > 0:
            chosen.append(Allocation(e.pool_address, e.dex_name, e.protocol, allocations[key], outputs[key]))
    token_in = edges[0].token_in
    token_out = edges[0].token_out
    return SplitRouteResult(token_in, token_out, amount_in, sum(x.amount_out for x in chosen), tuple(chosen), approximate, best_single_amount_out)
