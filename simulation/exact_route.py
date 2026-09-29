from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Any

from chain_context import ChainContext
from execution.route_ir import ExecutionPlan, ExecutionLeg
from protocols.models import PoolRef, Quote
from protocols.registry import ProtocolRegistry


@dataclass(frozen=True)
class SimulationLegResult:
    index: int
    protocol: str
    dex: str
    pool: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    fee_bps: int
    quote_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteSimulation:
    chain_id: int
    amount_in: int
    amount_out: int
    gross_profit: int
    legs: tuple[SimulationLegResult, ...]
    exact: bool
    requires_atomic_backend: bool = False
    error: str = ""

    @property
    def profitable(self) -> bool:
        return self.gross_profit > 0 and not self.error


class ExactRouteSimulator:
    """Sequential protocol-aware quote engine.

    Each hop is quoted by the protocol's canonical on-chain quoter/view where
    available. This is materially safer than applying a universal x*y=k model.

    ``exact`` means every hop was quoted from the canonical protocol state. It
    does not claim that the whole route is atomic unless an atomic backend is
    also used. Repeated pools are therefore marked as requiring atomic state
    simulation because the first swap mutates the pool before the second.
    """

    def __init__(self, w3, registry: ProtocolRegistry | None = None):
        self.w3 = w3
        self.chain = ChainContext.from_config(int(w3.eth.chain_id))
        self.registry = registry or ProtocolRegistry(w3)

    def _dex_config(self, dex: str):
        return self.chain.dex(dex)

    def _pool_ref(self, leg: ExecutionLeg) -> PoolRef:
        md = dict(leg.metadata or {})
        return PoolRef(
            address=leg.pool.lower(),
            protocol=leg.protocol.lower(),
            dex=leg.dex,
            token0=md.get("token0"),
            token1=md.get("token1"),
            fee_bps=int(leg.fee_bps or md.get("fee_bps", 0)),
            metadata=md,
        )

    def _adapter(self, leg: ExecutionLeg):
        adapter = self.registry.get(self._dex_config(leg.dex))
        if adapter is None:
            raise ValueError(f"No adapter for {leg.protocol}/{leg.dex} on chain {self.chain.chain_id}")
        return adapter

    def simulate(self, plan: ExecutionPlan, amount_in: int | None = None) -> RouteSimulation:
        if plan.chain_id != self.chain.chain_id:
            raise ValueError(f"plan chain {plan.chain_id} != RPC chain {self.chain.chain_id}")
        current = int(amount_in if amount_in is not None else plan.amount_in)
        if current <= 0:
            raise ValueError("simulation amount must be positive")

        seen_pools: set[str] = set()
        repeated_pool = False
        results: list[SimulationLegResult] = []
        try:
            for i, leg in enumerate(plan.legs):
                if leg.chain_id != self.chain.chain_id:
                    raise ValueError(f"leg {i} is on chain {leg.chain_id}")
                if i == 0 and leg.token_in.lower() != plan.token_in.lower():
                    raise ValueError("first leg token mismatch")
                if leg.amount_in not in (0, current):
                    # A plan may carry a discovery-time amount. The simulator
                    # owns the live amount flowing through the route.
                    raise ValueError(f"leg {i} amount_in={leg.amount_in} does not match live amount {current}")

                pool_key = leg.pool.lower()
                repeated = pool_key in seen_pools
                repeated_pool = repeated_pool or repeated
                seen_pools.add(pool_key)

                adapter = self._adapter(leg)
                ref = self._pool_ref(leg)
                quote: Quote | None = adapter.quote_exact_in(ref, leg.token_in, current)
                if quote is None:
                    return RouteSimulation(self.chain.chain_id, plan.amount_in, 0, -plan.amount_in,
                                           tuple(results), False, repeated_pool,
                                           f"quote failed at leg {i}: {leg.protocol}/{leg.dex}/{leg.pool}")
                if quote.amount_out < 0:
                    raise ValueError(f"negative quote at leg {i}")

                results.append(SimulationLegResult(
                    i, leg.protocol, leg.dex, leg.pool, leg.token_in, leg.token_out,
                    current, int(quote.amount_out), int(quote.fee_bps), dict(quote.metadata or {})
                ))
                current = int(quote.amount_out)

            return RouteSimulation(
                self.chain.chain_id,
                plan.amount_in,
                current,
                current - plan.amount_in,
                tuple(results),
                True,
                repeated_pool,
                "",
            )
        except Exception as exc:
            return RouteSimulation(self.chain.chain_id, plan.amount_in, 0, -plan.amount_in,
                                   tuple(results), False, True, str(exc))

    def simulate_many(self, plans: list[ExecutionPlan], amount_grid: list[int]) -> list[RouteSimulation]:
        out: list[RouteSimulation] = []
        for plan in plans:
            for amount in amount_grid:
                out.append(self.simulate(plan, amount))
        return out
