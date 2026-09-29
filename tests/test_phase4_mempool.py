from mempool.dedup import TxDeduper
from mempool.engine import MempoolOpportunityEngine, PendingObservation


def test_distinct_same_shape_transactions_are_not_deduped():
    d = TxDeduper(ttl=60)
    assert d.add('0xaaa') is True
    assert d.add('0xbbb') is True
    assert d.add('0xaaa') is False


def test_chain_validation():
    e = MempoolOpportunityEngine(1)
    e.validate_observation(PendingObservation(1, '0x1', '0x0', '0x0', 0, 0, 0, 0, 1, '0x'))


def test_score_requires_exact_quote_for_high_confidence():
    e = MempoolOpportunityEngine(1)
    assert e.score(state_freshness_ms=200, decoded=True, exact_quote=True, route_supported=True, trigger_seen=True) == 1.0
