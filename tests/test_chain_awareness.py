import pytest
from chain_context import ChainContext, assert_same_chain


def test_chain_context_has_six_target_chains():
    for cid in (1, 56, 137, 8453, 42161, 324):
        ctx = ChainContext.from_config(cid)
        assert ctx.chain_id == cid
        assert ctx.wrapped_native.startswith('0x')


def test_cross_chain_route_is_rejected():
    with pytest.raises(ValueError, match='cross-chain'):
        assert_same_chain([1, 8453])


def test_chain_protocol_validation():
    ChainContext.from_config(8453).validate_protocol('aerodrome_v2', 'AerodromeV2')
    with pytest.raises((ValueError, KeyError)):
        ChainContext.from_config(1).validate_protocol('aerodrome_v2', 'AerodromeV2')
