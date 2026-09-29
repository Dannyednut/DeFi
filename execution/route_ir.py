from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from chain_context import assert_same_chain
class ExecutionKind(Enum):
    V2_SWAP='v2_swap'; AERODROME_SWAP='aerodrome_swap'; CURVE_EXCHANGE='curve_exchange'; BALANCER_BATCH_SWAP='balancer_batch_swap'; SYNCSWAP_SWAP='syncswap_swap'; V3_SWAP='v3_swap'
@dataclass(frozen=True)
class ExecutionLeg:
    chain_id:int; kind:ExecutionKind; protocol:str; dex:str; pool:str; token_in:str; token_out:str; amount_in:int; amount_out_min:int=0; fee_bps:int=0; metadata:dict=field(default_factory=dict)
@dataclass(frozen=True)
class ExecutionPlan:
    chain_id:int; token_in:str; amount_in:int; legs:tuple[ExecutionLeg,...]; deadline:int; min_profit:int=0; metadata:dict=field(default_factory=dict)
    def __post_init__(self):
        if self.amount_in<=0: raise ValueError('amount_in must be positive')
        if not self.legs: raise ValueError('execution plan requires at least one leg')
        assert_same_chain([self.chain_id,*[x.chain_id for x in self.legs]])
        if self.legs[0].token_in.lower()!=self.token_in.lower(): raise ValueError('first leg token_in mismatch')
    @property
    def protocols(self): return tuple(dict.fromkeys(x.protocol for x in self.legs))
