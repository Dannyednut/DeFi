"""
executor/
=========
Arbitrage execution layer. Consumes Opportunity objects, selects lenders,
builds and signs EIP-1559 transactions, and relays to the network.

Public API:
    from executor import ArbExecutor, ExecutionResult

Example (in main.py process_block):
    executor = ArbExecutor(w3, chain_id=CHAIN_ID)
    for opp in opportunities:
        result = executor.submit(opp)
        if result and result.success:
            log.info(f"Executed: {result.tx_hash}")
"""
from .executor import ArbExecutor, ExecutionResult

__all__ = ["ArbExecutor", "ExecutionResult"]
