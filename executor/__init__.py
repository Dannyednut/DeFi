"""Arbitrage execution package.

Heavy Web3 dependencies are loaded lazily so lightweight policy/IR modules can
be imported by scanners and tests without initializing the live executor.
"""
__all__ = ["ArbExecutor", "ExecutionResult"]

def __getattr__(name):
    if name in __all__:
        from .executor import ArbExecutor, ExecutionResult
        return {"ArbExecutor": ArbExecutor, "ExecutionResult": ExecutionResult}[name]
    raise AttributeError(name)
