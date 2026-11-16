from .dedup import TxDeduper

# DSS-WO-ENG-016 §5: the unused mempool policy engine that used to live in
# this package was retired — the pipeline instantiated it but never called
# any of its methods. Classification and execution gating happen in the
# canonical path (OpportunityEngine validation, ChainMEVPolicy at the
# relay, opportunity ranking), so a second policy layer had no
# responsibility. The mempool package's production surface is the
# watcher/decoder/simulator/pipeline/outcome set plus TxDeduper.
__all__ = ["TxDeduper"]
