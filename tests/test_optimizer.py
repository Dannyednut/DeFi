import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils.rpc_manager import MultiRPCManager
from config import get_rpc_configs
from optimizer.optimal import get_optimal_amount
from web3 import Web3
from log import setup_logging

def test_get_optimal_amount(mode, amount=None):
    setup_logging()
    
    print("Initializing environment...")
    rpc_configs = get_rpc_configs()
    rpc_manager = MultiRPCManager.from_config(rpc_configs)
    w3 = rpc_manager.get_web3(ws=False)
    
    # Data derived from shadow_trades or observed arb cycle
    tokens = [
        "0xfff9976782d46cc05630d1f6ebab18b2324d6b14",
        "0x1c7d4b196cb0c7b01d743fbc6116a902379c7238",
        "0xfff9976782d46cc05630d1f6ebab18b2324d6b14"
    ]
    tokens = [Web3.to_checksum_address(t) for t in tokens]
    
    pools = [
        "0x72e46e15ef83c896de44b1874b4af7ddab5b4f74",
        "0x6ce0896eae6d4bd668fde41bb784548fb8f59b50"
    ]
    pools = [Web3.to_checksum_address(p) for p in pools]
    
    fees = [3000, 3000]
    
    print(f"Testing optimal amount for path:")
    print(f"  Tokens: {tokens}")
    print(f"  Pools: {pools}")
    print(f"  Fees: {fees}\n")
    
    try:
        print("\n--- Testing 1: MAIN ENTRYPOINT ---")
        result = get_optimal_amount(w3, pools, tokens, fees, mode=mode)
        print("=== OPTIMAL RESULT ===")
        print(f"Amount In:       {result.amount_in}")
        print(f"Expected Profit: {result.expected_profit}")
        print(f"Method Used:     {result.method}")
        print(f"Tick Crossed:    {result.tick_crossed}")
        
        print("\n--- Testing 2: ON-CHAIN getProfit ---")
        try:
            from config import get_chain, ARB_EXEC_ABI
            arb_exec_address = get_chain().arb_exec_address
            if arb_exec_address:
                arb_exec = w3.eth.contract(address=w3.to_checksum_address(arb_exec_address), abi=ARB_EXEC_ABI)
                amount_in = result.amount_in if amount == None else amount
                arb_data = {
                    "tokens": tokens,
                    "pools": pools,
                    "amountIn": amount_in,
                    "tokenIn": tokens[0],
                    "borrowPool": pools[0],
                    "fees": fees,
                    "minProfit": 0,
                    "mode": mode,
                }
                out_amount, profit = arb_exec.functions.getProfit(arb_data).call()
                print(f"On-chain Out Amount: {out_amount}")
                print(f"On-chain Profit:     {profit}")
            else:
                print("No ARB_EXEC_ADDRESS defined in config.")
        except Exception as e:
            print(f"Error calling getProfit: {e}")

        print("\n--- Testing 3: GOLDEN SECTION (Forced) ---")
        from optimizer.golden_section import find_optimal_amount
        best_amt, best_profit = find_optimal_amount(w3, tokens, fees, max_iterations=20)
        print(f"Amount In:       {best_amt}")
        print(f"Expected Profit: {best_profit}")
        
        print("\n--- Testing 4: TICK WALK (Forced) ---")
        from optimizer.optimal import _run_tick_walk
        tw_result = _run_tick_walk(w3, pools, tokens, fees)
        print(f"Amount In:       {tw_result.amount_in}")
        print(f"Expected Profit: {tw_result.expected_profit}")

    except Exception as e:
        print(f"\nError calculating optimal amount: {e}")

if __name__ == "__main__":
    print("======= case 1 =======")
    test_get_optimal_amount(0)
    print()

    print("======= case 2 =======")
    test_get_optimal_amount(1)
    print()

    print("======= case 3 =======")
    test_get_optimal_amount(0, 0)
    print()

    print("======= case 4 =======")
    test_get_optimal_amount(1, 0)
    print()



