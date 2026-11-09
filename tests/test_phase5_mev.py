from mempool.reorg import TriggerTracker
from mev.policy import ChainMEVPolicy


def test_target_block_policy():
    p = ChainMEVPolicy.for_chain(1)
    p.validate_target(100, 101)
    try:
        p.validate_target(100, 104)
    except ValueError:
        pass
    else:
        raise AssertionError("stale/far target block accepted")


def test_replacement_by_sender_nonce():
    t = TriggerTracker()
    assert t.observe("0xa", "0xsender", 7)[0] is None
    prev, _ = t.observe("0xb", "0xsender", 7)
    assert prev == "0xa"
    assert t._by_hash["0xa"].status == "replaced"


# DSS-WO-ENG-016 §4: the retired alternative MEV bundle models/orchestration
# tests were removed with their modules. ChainMEVPolicy (retained — the relay
# consumes it at private-submission time) is covered by
# test_target_block_policy above and tests/test_phase6_execution.py.
