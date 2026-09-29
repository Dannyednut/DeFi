"""Delegates to optimizer.execution_economics.ExecutionEconomics for the
actual arithmetic — this module used to have its own independent copy of
the same "gross - gas - loan - protocol - tip - buffer" formula. Keeping
SimulationEconomics' own name/shape/field names since this module's own
callers already depend on them; only the internals changed."""
from __future__ import annotations
from dataclasses import dataclass

from optimizer.execution_economics import ExecutionEconomics


@dataclass(frozen=True)
class SimulationEconomics:
    gross_profit: int
    gas_cost: int
    flashloan_fee: int = 0
    protocol_cost: int = 0
    builder_tip: int = 0
    execution_buffer: int = 0

    def _as_execution_economics(self) -> ExecutionEconomics:
        return ExecutionEconomics(
            gross_profit_wei=self.gross_profit,
            gas_cost_wei=self.gas_cost,
            flashloan_fee_wei=self.flashloan_fee,
            protocol_cost_wei=self.protocol_cost,
            builder_tip_wei=self.builder_tip,
            execution_buffer_wei=self.execution_buffer,
        )

    @property
    def net_profit(self) -> int:
        return self._as_execution_economics().deterministic_net_wei

    @property
    def max_builder_tip(self) -> int:
        return self._as_execution_economics().max_builder_tip_wei

    def profitable(self, minimum_profit: int = 0) -> bool:
        return self._as_execution_economics().profitable(minimum_profit)
