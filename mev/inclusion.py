"""Execution economics and inclusion-aware bid selection.

Delegates to optimizer.execution_economics.BidOptimizer for the actual
arithmetic — this module used to have its own independent copy of the same
"tip capped by available profit" formula. Keeping InclusionEstimate/
BidPolicy's own names/shape since this module's own callers/tests already
depend on them; only the internals changed.
"""
from __future__ import annotations
from dataclasses import dataclass

from optimizer.execution_economics import BidConfig, BidOptimizer

@dataclass(frozen=True)
class InclusionEstimate:
    probability: float
    max_tip_wei: int
    expected_net_wei: int
    reason: str = ""

@dataclass(frozen=True)
class BidPolicy:
    min_expected_net_wei: int = 0
    max_tip_bps_of_gross: int = 9000
    safety_bps: int = 250

    def choose_tip(self, gross_profit_wei: int, gas_cost_wei: int, *, competing_tip_wei: int = 0) -> InclusionEstimate:
        optimizer = BidOptimizer(BidConfig(
            max_tip_bps_of_available=self.max_tip_bps_of_gross,
            safety_bps=self.safety_bps,
            min_net_profit_wei=self.min_expected_net_wei,
        ))
        result = optimizer.choose(gross_profit_wei, gas_cost_wei, competing_tip_wei=competing_tip_wei)
        # NOTE: BidResult has two distinct fields -- tip_wei (the actual
        # chosen bid, after the competing-tip clamp and safety discount) and
        # max_tip_wei (the raw ceiling before either). The old InclusionEstimate
        # only had one field, named max_tip_wei, but it actually held the
        # chosen bid (tip), not the ceiling -- map to tip_wei here, not
        # max_tip_wei, to preserve that original (slightly misleadingly
        # named) behavior.
        return InclusionEstimate(
            probability=1.0 if result.accepted else 0.0,
            max_tip_wei=result.tip_wei,
            expected_net_wei=result.expected_net_wei,
            reason="tip capped by gross-profit economics",
        )
