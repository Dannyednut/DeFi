from mev.models import BundleCandidate
from mempool.reorg import TriggerTracker
from mev.policy import ChainMEVPolicy


def test_bundle_candidate_net_ceiling():
    c = BundleCandidate(1, "0xtrig", "0xraw", 100, 10_000, 100, 10, 1, 100)
    assert c.gas_ceiling_wei == 1000
    assert c.net_ceiling_profit_wei == 8900


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
