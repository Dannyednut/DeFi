from __future__ import annotations
from dataclasses import dataclass

from simulation.exact_route import ExactRouteSimulator
from execution.route_ir import ExecutionPlan, ExecutionLeg, ExecutionKind


class FakeEth:
    chain_id = 1

class FakeW3:
    eth = FakeEth()
    @staticmethod
    def to_checksum_address(x): return x


@dataclass
class Q:
    amount_out: int
    fee_bps: int = 30
    metadata: dict = None

class FakeAdapter:
    def quote_exact_in(self, pool, token_in, amount_in):
        return type('Quote', (), {'amount_out': amount_in * 99 // 100, 'fee_bps': 30, 'metadata': {}})()

class FakeRegistry:
    def get(self, dex): return FakeAdapter()

class FakeChain:
    def __init__(self, chain_id=1): self.chain_id=chain_id
    def dex(self, name): return type('D', (), {'name':name,'protocol':'uniswap_v2','enabled':True,'chain_id':1})()


def test_exact_route_sequential_quotes(monkeypatch):
    import simulation.exact_route as m
    monkeypatch.setattr(m.ChainContext, 'from_config', staticmethod(lambda cid: FakeChain(cid)))
    sim = ExactRouteSimulator(FakeW3(), FakeRegistry())
    p = ExecutionPlan(1, '0xA', 1000, (
        ExecutionLeg(1, ExecutionKind.V2_SWAP, 'uniswap_v2','x','0x1','0xA','0xB',1000),
        ExecutionLeg(1, ExecutionKind.V2_SWAP, 'uniswap_v2','x','0x2','0xB','0xA',0),
    ), 1)
    r = sim.simulate(p)
    assert r.exact
    assert r.amount_out == 980
    assert r.gross_profit == -20


def test_plan_rejects_cross_chain():
    try:
        ExecutionPlan(1,'0xA',100,(ExecutionLeg(1,ExecutionKind.V2_SWAP,'uniswap_v2','x','0x1','0xA','0xB',100), ExecutionLeg(56,ExecutionKind.V2_SWAP,'uniswap_v2','x','0x2','0xB','0xA',0)),1)
    except ValueError as e:
        assert 'cross-chain' in str(e)
    else:
        raise AssertionError('cross-chain plan should fail')
