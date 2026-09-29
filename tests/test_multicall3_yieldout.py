import os
import sys
import json
import logging

logging.basicConfig(level=logging.DEBUG)

from web3 import Web3

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils.multicall3 import multicall3_batch_yieldOut
from config import get_chain
from detectors.base import BaseDetector

class DummyDetector(BaseDetector):
    def on_block(self, block_number: int):
        pass

class MockCycle:
    def __init__(self, tokens, pools, fees, amount_in_wei):
        self.tokens = tokens
        self.pools = pools
        self.fees = fees
        self.amount_in_wei = amount_in_wei

def load_opps_from_logs(log_path, max_opps=5):
    cycles = []
    if not os.path.exists(log_path):
        print(f"Log file not found: {log_path}")
        return cycles
        
    with open(log_path, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if 'arb_tokens' in data and 'arb_pools' in data and 'fees' in data:
                    cycle = MockCycle(
                        tokens=data['arb_tokens'],
                        pools=data['arb_pools'],
                        fees=data['fees'],
                        amount_in_wei=data.get('simulated_amount_in', 0)
                    )
                    cycles.append(cycle)
                    if len(cycles) >= max_opps:
                        break
            except Exception as e:
                pass
    return cycles

def test_run_multicall3_batch_yieldOut():
    w3 = Web3(Web3.HTTPProvider("https://0xrpc.io/eth"))
    
    chain_config = get_chain()
    arb_exec_address = chain_config.arb_exec_address
    
    log_file_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'pending_opportunities.jsonl')
    
    cycles = load_opps_from_logs(log_file_path, max_opps=5)
    print(f"Loaded {len(cycles)} cycles from logs.")
    
    assert cycles, "No cycles to test."
    
    dummy_detector = DummyDetector(w3, None, None, None, chain_config.chain_id)
    
    print(f"Running multicall3_batch_yieldOut for {len(cycles)} cycles...")
    
    results = multicall3_batch_yieldOut(w3, arb_exec_address, cycles, chunk_size=50)
    print(f"Batch yieldOut completed. Results count: {len(results)}")
    
    assert isinstance(results, dict)
    assert len(results) == len(cycles)
    
    for idx, cycle in enumerate(cycles):
        mc_res = results.get(idx, (None, None))
        
        contract_res = (None, None)
        try:
            contract_res = dummy_detector._simulate_via_contract(cycle, getattr(cycle, 'amount_in_wei', 0))
        except Exception as e:
            contract_res = f"Error: {e}"
            
        print(f"Cycle {idx}:")
        print(f"  Multicall : {mc_res}")
        print(f"  Contract  : {contract_res}")
        
        # Optional: assert mc_res == contract_res
        
if __name__ == "__main__":
    test_run_multicall3_batch_yieldOut()
