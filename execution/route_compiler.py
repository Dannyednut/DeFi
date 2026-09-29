from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from web3 import Web3
from chain_context import ChainContext
from .route_ir import ExecutionKind, ExecutionLeg, ExecutionPlan
from .protocol_builders import curve_pool_exchange, curve_router_ng, aerodrome_v2, balancer_batch, syncswap

@dataclass(frozen=True)
class CallSpec:
    chain_id: int
    target: str
    value: int
    function: str
    args: tuple[Any, ...]
    protocol: str
    dex: str
    pool: str

class RouteCompiler:
    """Compile validated execution IR into protocol-specific calls."""
    def __init__(self, w3: Web3):
        self.w3 = w3
        self.chain_id = int(w3.eth.chain_id)
        self.chain = ChainContext.from_config(self.chain_id)

    def compile(self, plan: ExecutionPlan):
        if plan.chain_id != self.chain_id:
            raise ValueError(f'execution chain mismatch: rpc={self.chain_id}, plan={plan.chain_id}')
        return [self._compile_leg(x, plan) for x in plan.legs]

    def _addr(self, value: str) -> str:
        return Web3.to_checksum_address(value)

    def _compile_leg(self, leg: ExecutionLeg, plan: ExecutionPlan):
        if leg.chain_id != self.chain_id:
            raise ValueError(f'leg chain mismatch: {leg.chain_id} != {self.chain_id}')
        self.chain.validate_protocol(leg.protocol, leg.dex)
        m = leg.metadata
        recipient = self._addr(m.get('recipient') or m.get('to'))
        deadline = int(m.get('deadline') or plan.deadline)

        if leg.kind == ExecutionKind.V2_SWAP:
            d = self.chain.dex(leg.dex)
            return CallSpec(self.chain_id, self._addr(d.router), 0, 'swapExactTokensForTokens',
                (leg.amount_in, leg.amount_out_min, [self._addr(leg.token_in), self._addr(leg.token_out)], recipient, deadline),
                leg.protocol, leg.dex, leg.pool)

        if leg.kind == ExecutionKind.AERODROME_SWAP:
            d = self.chain.dex(leg.dex)
            call = aerodrome_v2(self._addr(d.router), leg.amount_in, leg.amount_out_min,
                self._addr(leg.token_in), self._addr(leg.token_out), bool(m.get('stable', False)),
                self._addr(m.get('factory') or d.factory), recipient, deadline)
            return CallSpec(self.chain_id, call.target, call.value, call.function, call.args,
                            leg.protocol, leg.dex, leg.pool)

        if leg.kind == ExecutionKind.CURVE_EXCHANGE:
            if m.get('router_ng'):
                call = curve_router_ng(self._addr(m['router_ng']),
                    [self._addr(x) for x in m['route']], m['swap_params'], leg.amount_in,
                    leg.amount_out_min, pools=[self._addr(x) for x in m.get('pools', [])],
                    receiver=recipient)
            else:
                call = curve_pool_exchange(self._addr(leg.pool), int(m['i']), int(m['j']),
                                           leg.amount_in, leg.amount_out_min,
                                           underlying=bool(m.get('underlying', False)))
            return CallSpec(self.chain_id, call.target, call.value, call.function, call.args,
                            leg.protocol, leg.dex, leg.pool)

        if leg.kind == ExecutionKind.BALANCER_BATCH_SWAP:
            vault = self._addr(m.get('vault') or self.chain.dex(leg.dex).router)
            pid = m['pool_id']
            pid = bytes.fromhex(pid.removeprefix('0x')) if isinstance(pid, str) else pid
            assets = [self._addr(x) for x in m['assets']]
            funds_raw = m['funds']
            funds = (self._addr(funds_raw[0]), bool(funds_raw[1]), self._addr(funds_raw[2]), bool(funds_raw[3]))
            limits = [int(x) for x in m['limits']]
            call = balancer_batch(vault, pid, m['asset_in_index'], m['asset_out_index'],
                                  leg.amount_in, assets, funds, limits, deadline)
            return CallSpec(self.chain_id, call.target, call.value, call.function, call.args,
                            leg.protocol, leg.dex, leg.pool)

        if leg.kind == ExecutionKind.SYNCSWAP_SWAP:
            router = self._addr(m.get('router') or self.chain.dex(leg.dex).router)
            call = syncswap(router, m.get('paths', m.get('args', ())), leg.amount_out_min, deadline)
            return CallSpec(self.chain_id, call.target, call.value, call.function, call.args,
                            leg.protocol, leg.dex, leg.pool)

        if leg.kind == ExecutionKind.V3_SWAP:
            d = self.chain.dex(leg.dex)
            return CallSpec(self.chain_id, self._addr(d.router), 0,
                            m.get('function', 'exactInput'), tuple(m.get('args', ())),
                            leg.protocol, leg.dex, leg.pool)
        raise ValueError(f'unsupported execution kind: {leg.kind}')
