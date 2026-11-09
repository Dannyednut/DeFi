import pytest
from executor.chain_execution import profile_for, MempoolModel, validate_transaction_dict
from mev.policy import ChainMEVPolicy


def test_chain_profiles_are_explicit():
    assert profile_for(1).atomic_backrun_supported
    assert profile_for(42161).mempool_model == MempoolModel.SEQUENCER
    assert not profile_for(324).atomic_backrun_supported


def test_chain_policy_follows_profile():
    assert ChainMEVPolicy.for_chain(1).private_backrun is True
    assert ChainMEVPolicy.for_chain(324).private_backrun is False


def test_transaction_chain_validation():
    validate_transaction_dict({"chainId": 1, "to": "0x1", "data": "0x"}, 1)
    with pytest.raises(ValueError):
        validate_transaction_dict({"chainId": 56, "to": "0x1", "data": "0x"}, 1)


# DSS-WO-ENG-016 §4: the retired inclusion-bid policy wrapper's test was
# removed with the module — nothing in the canonical runtime path selected
# tips through it (the executor's builder tip is competition-tier based).

# DSS-WO-ENG-017 §3B/§3C: executor/nonce_manager.py and
# executor/relay_registry.py were retired (their import-coverage tests went
# with them):
#   * nonce_manager: the ExecutionRouter runs exactly ONE execution worker
#     (async_workers clamped to 1) and the executor fetches a fresh "pending"
#     nonce per transaction at build time — signing/submission is deliberately
#     serial, so no nonce race exists and no reservation layer is needed;
#   * relay_registry: relay selection is the chain's FLASHBOTS_RPCS
#     configuration + ChainMEVPolicy (executor/relay.py); there is no
#     multi-relay requirement and no second selection layer.
# See docs/DEPENDENCIES.md §11 for the full disposition table.
