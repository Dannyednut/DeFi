"""Optimization package.

The economics/split/ranking modules are dependency-light. The legacy on-chain
optimizer is imported lazily so analytics and pure-Python optimization can run
without Web3 installed.
"""
__all__ = ["get_optimal_amount", "_get_amount_out", "OptimalResult", "DEFAULT_TICK_CROSS_THRESHOLD"]

def __getattr__(name):
    if name in __all__:
        from . import optimal
        return getattr(optimal, name)
    raise AttributeError(name)
