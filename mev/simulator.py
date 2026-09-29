from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.request import Request, urlopen
from chain_context import ChainContext
from .models import BundleSimulation

class SimulationBackend(Protocol):
    def simulate(self, bundle: list[str], target_block: int, state_block: str = "latest") -> dict[str, Any]: ...

@dataclass
class JsonRpcBundleBackend:
    url: str
    timeout: float = 5.0
    method: str = "eth_callBundle"

    def simulate(self, bundle: list[str], target_block: int, state_block: str = "latest") -> dict[str, Any]:
        # Flashbots-style RPC. Different relays may use slightly different parameter names;
        # keep this adapter isolated so relay-specific changes do not leak into the engine.
        params = [{"txs": bundle, "blockNumber": hex(target_block), "stateBlockNumber": state_block}]
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":self.method,"params":params}).encode()
        req = Request(self.url, data=body, headers={"Content-Type":"application/json"}, method="POST")
        with urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result", payload)

class BundleSimulator:
    def __init__(self, chain_id: int, backend: SimulationBackend):
        self.chain = ChainContext.from_config(int(chain_id))
        self.backend = backend

    def simulate(self, signed_txs: list[str], target_block: int) -> BundleSimulation:
        if not signed_txs:
            raise ValueError("bundle must contain at least one signed transaction")
        current = None
        # Backend is authoritative for state; chain policy still prevents accidental cross-chain use.
        try:
            raw = self.backend.simulate(signed_txs, target_block)
            results = tuple(raw.get("results") or ())
            gas_used = int(raw.get("totalGasUsed") or sum(int(x.get("gasUsed") or 0) for x in results))
            gas_fees = int(raw.get("gasFees") or sum(int(x.get("gasFees") or 0) for x in results))
            coinbase = int(raw.get("coinbaseDiff") or 0)
            failed = [x for x in results if x.get("error") or x.get("revert")]
            return BundleSimulation(not failed, self.chain.chain_id, target_block, gas_used, gas_fees, coinbase, "bundle tx reverted" if failed else "", results, raw)
        except Exception as exc:
            return BundleSimulation(False, self.chain.chain_id, target_block, error=str(exc))
