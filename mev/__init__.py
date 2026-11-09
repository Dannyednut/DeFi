"""Chain-specific MEV execution policy (DSS-WO-ENG-016).

The only production consumer of this package is executor/relay.py, the
single submission boundary: it enforces ChainMEVPolicy at private-bundle
submission time. The retired parallel MEV orchestration layer and its
unused support modules (models/simulator/inclusion) were removed by
DSS-WO-ENG-016 §4: mempool backrun opportunities flow through the one
canonical runtime path

    MempoolWatcher → MempoolPipeline → MempoolSimulator →
    OpportunityEngine.process_candidates → ExecutionRouter →
    ArbExecutor → Builder → Signer → Relay
"""
from .policy import ChainMEVPolicy

__all__ = ["ChainMEVPolicy"]
