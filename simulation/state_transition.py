from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.request import Request, urlopen


class StateTransitionBackend(Protocol):
    def simulate_bundle(self, signed_txs: list[str], target_block: int, state_block: str = 'latest') -> dict[str, Any]: ...


@dataclass(frozen=True)
class BundleStateTransition:
    success: bool
    chain_id: int
    target_block: int
    gas_used: int
    gas_fees: int
    coinbase_diff: int
    tx_results: tuple[dict[str, Any], ...]
    error: str = ''
    raw: dict[str, Any] | None = None

    @property
    def atomic(self) -> bool:
        return self.success and not self.error and all(not (x.get('error') or x.get('revert')) for x in self.tx_results)


@dataclass
class RpcCallBundleBackend:
    url: str
    timeout: float = 7.5
    method: str = 'eth_callBundle'

    def simulate_bundle(self, signed_txs: list[str], target_block: int, state_block: str = 'latest') -> dict[str, Any]:
        if not signed_txs:
            raise ValueError('bundle cannot be empty')
        params = [{
            'txs': signed_txs,
            'blockNumber': hex(int(target_block)),
            'stateBlockNumber': state_block,
        }]
        body = json.dumps({'jsonrpc':'2.0','id':1,'method':self.method,'params':params}).encode()
        req = Request(self.url, data=body, headers={'Content-Type':'application/json'}, method='POST')
        with urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        if payload.get('error'):
            raise RuntimeError(str(payload['error']))
        return payload.get('result', payload)


def parse_bundle_transition(raw: dict[str, Any], chain_id: int, target_block: int) -> BundleStateTransition:
    results = tuple(raw.get('results') or ())
    gas_used = int(raw.get('totalGasUsed') or sum(int(x.get('gasUsed') or 0) for x in results))
    gas_fees = int(raw.get('gasFees') or sum(int(x.get('gasFees') or 0) for x in results))
    coinbase = int(raw.get('coinbaseDiff') or 0)
    failed = any(x.get('error') or x.get('revert') for x in results)
    return BundleStateTransition(
        not failed, int(chain_id), int(target_block), gas_used, gas_fees, coinbase,
        results, 'bundle transaction reverted' if failed else '', raw,
    )
