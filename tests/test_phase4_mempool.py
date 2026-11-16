from mempool.dedup import TxDeduper


def test_distinct_same_shape_transactions_are_not_deduped():
    d = TxDeduper(ttl=60)
    assert d.add('0xaaa') is True
    assert d.add('0xbbb') is True
    assert d.add('0xaaa') is False


# DSS-WO-ENG-016 §5: the unused mempool policy engine tests were removed
# together with the retired module — the pipeline never called any of its
# methods. Chain-aware classification and execution gating are covered by
# tests/test_phase6_execution.py (chain profiles / MEV policy) and the
# canonical promotion path by tests/test_phase18_pipeline.py.
