"""Distinct arbitrage detectors with a shared Opportunity schema.

Imports are lazy so a lightweight detector test does not pull the Web3-heavy
cycle/quoter stack into the interpreter.
"""

__all__ = [
    "CycleDetector", "SharpeDetector", "NewPoolDetector", "CrossProtocolDetector",
    "StableImbalanceDetector", "SplitRouteDetector", "LiquidationScanner",
]


def __getattr__(name):
    if name in {"CycleDetector", "SharpeDetector"}:
        from .cycles import CycleDetector, SharpeDetector
        return {"CycleDetector": CycleDetector, "SharpeDetector": SharpeDetector}[name]
    if name in {"NewPoolDetector", "CrossProtocolDetector", "StableImbalanceDetector", "LiquidationScanner"}:
        from .specialized import NewPoolDetector, CrossProtocolDetector, StableImbalanceDetector, LiquidationScanner
        return {"NewPoolDetector": NewPoolDetector, "CrossProtocolDetector": CrossProtocolDetector,
                "StableImbalanceDetector": StableImbalanceDetector, "LiquidationScanner": LiquidationScanner}[name]
    if name == "SplitRouteDetector":
        from .advanced import SplitRouteDetector
        return SplitRouteDetector
    raise AttributeError(name)
