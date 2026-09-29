import pytest
from executor.chain_execution import profile_for, MempoolModel, validate_transaction_dict
from executor.relay_registry import RelayRegistry, RelayEndpoint
from executor.nonce_manager import NonceManager
from mev.inclusion import BidPolicy
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


def test_relay_registry_never_invents_capability():
    registry = RelayRegistry([RelayEndpoint("eth", 1, "https://relay", True, True)])
    assert registry.require_atomic_backrun(1)
    assert registry.require_atomic_backrun(42161) == []


def test_bid_policy_is_profit_bounded():
    estimate = BidPolicy().choose_tip(1_000, 100, competing_tip_wei=500)
    assert 0 <= estimate.max_tip_wei <= 900
    assert estimate.expected_net_wei >= 0
