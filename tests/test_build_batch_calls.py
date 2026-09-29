"""
Real-time test for build_batch_calls function in executor/builder.py
Uses actual Web3 connection and ArbExec contract - no mocks.
"""
import sys
import os
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from executor.builder import build_batch_calls, build_arb_data
from logger import Opportunity, OppType
from web3 import Web3
from config import CHAIN_ID, get_chain, ARB_EXEC_ABI
from dotenv import load_dotenv

load_dotenv()


def create_test_opportunity(block_number, arb_data_tuple):
    """
    Create a real Opportunity object with the provided arb_data.
    
    Args:
        block_number: The block number for the opportunity
        arb_data_tuple: The arb_data tuple from getProfit [amountIn, minProfit, tokens, pools, fees, tokenIn, mode]
    
    Returns:
        Opportunity dataclass instance
    """
    tokens = arb_data_tuple[2]
    pools = arb_data_tuple[3]
    
    return Opportunity(
        opp_type=OppType.DEX_SPREAD,
        chain_id=CHAIN_ID,
        block_number=block_number,
        timestamp=time.time(),
        tokens=tokens,
        pools=pools,
        dexes=["UniswapV3", "UniswapV2"],
        estimated_profit_usd=2.39,
        estimated_profit_ratio=0.1349,
        amount_in_usd=36.72,
        gas_cost_usd=0.04,
        net_profit_usd=2.35,
        metadata={
            "hop_count": 2,
            "is_cross_dex": True,
            "dex_count": 2,
            "fees_bps": [5, 30],
            "fees_pip": [500, 3000],
            "versions": [3, 2],
            "cycle_type": "DEX-DEX",
            "profit_pct": 13.49,
            "gas_units": 350000.0,
            "root_token": tokens[0],
            "arb_data": arb_data_tuple,
            "optimal_amount_in": arb_data_tuple[0],
            "from_mempool": False,
        }
    )


def real_execute_func(opp, for_batch=True):
    """
    Real execution function that builds arb_data from opportunity metadata.
    This mirrors what _execute does in executor/executor.py
    """
    arb_data_tuple = opp.metadata.get("arb_data")
    
    if arb_data_tuple:
        # Build arb_data dict from the tuple
        arb_data = build_arb_data(
            ad_tuple=arb_data_tuple,
            opp_tokens=opp.tokens,
            opp_pools=opp.pools,
            opp_fees_pip=opp.metadata.get("fees_pip", []),
            amount_in=arb_data_tuple[0],
            token_in=arb_data_tuple[5],
            mode=1 if for_batch else arb_data_tuple[6],  # mode 1 for batch (pool flash swap)
            min_profit=arb_data_tuple[1],
        )
    else:
        # Build from opportunity fields directly
        arb_data = build_arb_data(
            ad_tuple=None,
            opp_tokens=opp.tokens,
            opp_pools=opp.pools,
            opp_fees_pip=opp.metadata.get("fees_pip", []),
            amount_in=opp.metadata.get("optimal_amount_in", 0),
            token_in=opp.tokens[0] if opp.tokens else "",
            mode=1 if for_batch else 0,
            min_profit=0,
        )
    
    gas_units = opp.metadata.get("gas_units", 350000)
    gas_usd = opp.gas_cost_usd
    
    return arb_data, gas_units, gas_usd


def get_web3_instance():
    """Get a real Web3 instance from RPC config."""
    from config import get_rpc_configs
    from utils.rpc_manager import MultiRPCManager
    
    rpc_configs = get_rpc_configs()
    rpc_manager = MultiRPCManager.from_config(rpc_configs)
    return rpc_manager.get_web3(ws=False)


def test_build_batch_calls_single():
    """Test build_batch_calls with a single real opportunity."""
    print("\n" + "="*60)
    print("TEST: build_batch_calls with single opportunity")
    print("="*60)
    
    # Real test data from user's logs
    arb_data = [
        0,  # amountIn
        0,  # minProfit
        ["0xfff9976782d46cc05630d1f6ebab18b2324d6b14", 
         "0x779877a7b0d9e8603169ddbd7836e478b4624789", 
         "0xfff9976782d46cc05630d1f6ebab18b2324d6b14"],  # tokens
        ["0x9bb3ea0b68491f2a0cf2776c850cbb566022b345", 
         "0x6561cf90fde56d6adccaa818c9bee07e0668d229"],  # pools
        [500, 3000],  # fees
        "0xfff9976782d46cc05630d1f6ebab18b2324d6b14",  # tokenIn
        0  # mode
    ]
    
    # Create real opportunity
    opp = create_test_opportunity(block_number=10784413, arb_data_tuple=arb_data)
    print(f"\nCreated opportunity:")
    print(f"  Block: {opp.block_number}")
    print(f"  Tokens: {opp.tokens}")
    print(f"  Pools: {opp.pools}")
    print(f"  arb_data: {opp.metadata['arb_data']}")
    
    # Get Web3 and contract
    w3 = get_web3_instance()
    chain = get_chain()
    
    if not chain.arb_exec_address:
        print("ERROR: No arb_exec_address configured. Check .env file.")
        return False
    
    print(f"\nConnecting to ArbExec contract at {chain.arb_exec_address}")
    arb_exec = w3.eth.contract(
        address=w3.to_checksum_address(chain.arb_exec_address),
        abi=ARB_EXEC_ABI
    )
    
    # Call build_batch_calls with real function and contract
    calls, total_gas, total_gas_usd = build_batch_calls(
        func=real_execute_func,
        arb_contract=arb_exec,
        opportunities=[opp]
    )
    
    print(f"\nResults:")
    print(f"  Number of calls: {len(calls)}")
    print(f"  Total gas units: {total_gas}")
    print(f"  Total gas USD: ${total_gas_usd:.4f}")
    print(f"  Call data length: {len(calls[0]) if calls else 0} bytes")
    # print(f"  Call data: {calls}")
    
    # Verify results
    assert len(calls) == 1, f"Expected 1 call, got {len(calls)}"
    assert total_gas == 350000, f"Expected gas 350000, got {total_gas}"
    assert len(calls[0]) > 0, "Call data should not be empty"
    
    print("\n✅ Single opportunity test PASSED")
    return True


def test_build_batch_calls_multiple():
    """Test build_batch_calls with multiple real opportunities."""
    print("\n" + "="*60)
    print("TEST: build_batch_calls with multiple opportunities")
    print("="*60)
    
    # Create multiple opportunities with different amounts
    arb_data_1 = [0, 0, ["0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14", "0x779877A7B0D9E8603169DdbD7836e478b4624789", "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14"], ["0xDD7CC9a0dA070fB8B60dC6680b596133fb4A7100", "0x6561cF90FDE56d6ADCcAa818C9bee07E0668d229"], [3000, 3000], "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14", 1]
    
    arb_data_2 = [0, 0, ["0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14", "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238", "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14"], ["0x72e46e15ef83c896de44B1874B4AF7dDAB5b4F74", "0x3289680dD4d6C10bb19b899729cda5eEF58AEfF1"], [3000, 500], "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14", 1]
    
    opp1 = create_test_opportunity(block_number=10798983, arb_data_tuple=arb_data_1)
    opp2 = create_test_opportunity(block_number=10798983, arb_data_tuple=arb_data_2)
    
    print(f"\nCreated {2} opportunities")
    print(f"  Opp 1 amountIn: {arb_data_1[0]}")
    print(f"  Opp 2 amountIn: {arb_data_2[0]}")
    
    # Get Web3 and contract
    w3 = get_web3_instance()
    chain = get_chain()
    
    if not chain.arb_exec_address:
        print("ERROR: No arb_exec_address configured")
        return False
    
    arb_exec = w3.eth.contract(
        address=w3.to_checksum_address(chain.arb_exec_address),
        abi=ARB_EXEC_ABI
    )
    
    # Build batch calls
    calls, total_gas, total_gas_usd = build_batch_calls(
        func=real_execute_func,
        arb_contract=arb_exec,
        opportunities=[opp1, opp2]
    )
    
    print(f"\nResults:")
    print(f"  Number of calls: {len(calls)}")
    print(f"  Total gas units: {total_gas}")
    print(f"  Expected gas: {350000 * 2}")
    print(f"  Call data: {calls}")
    
    # Verify
    assert len(calls) == 2, f"Expected 2 calls, got {len(calls)}"
    assert total_gas == 700000, f"Expected gas 700000, got {total_gas}"
    assert calls[0] != calls[1], "Calls should be different (different amounts)"
    
    print("\n✅ Multiple opportunities test PASSED")
    return True


def test_build_batch_calls_empty():
    """Test build_batch_calls with empty opportunities list."""
    print("\n" + "="*60)
    print("TEST: build_batch_calls with empty list")
    print("="*60)
    
    w3 = get_web3_instance()
    chain = get_chain()
    
    if not chain.arb_exec_address:
        print("ERROR: No arb_exec_address configured")
        return False
    
    arb_exec = w3.eth.contract(
        address=w3.to_checksum_address(chain.arb_exec_address),
        abi=ARB_EXEC_ABI
    )
    
    calls, total_gas, total_gas_usd = build_batch_calls(
        func=real_execute_func,
        arb_contract=arb_exec,
        opportunities=[]
    )
    
    print(f"\nResults:")
    print(f"  Calls: {len(calls)}")
    print(f"  Gas: {total_gas}")
    print(f"  Gas USD: ${total_gas_usd}")
    
    assert len(calls) == 0, "Should have 0 calls"
    assert total_gas == 0, "Should have 0 gas"
    
    print("\n✅ Empty list test PASSED")
    return True


def test_arb_data_structure():
    """Test that arb_data is correctly structured for contract calls."""
    print("\n" + "="*60)
    print("TEST: arb_data structure verification")
    print("="*60)
    
    arb_data = [
        12240000000000000,  # amountIn
        1000000000000000,   # minProfit
        ["0xfff9976782d46cc05630d1f6ebab18b2324d6b14", 
         "0x779877a7b0d9e8603169ddbd7836e478b4624789", 
         "0xfff9976782d46cc05630d1f6ebab18b2324d6b14"],
        ["0x9bb3ea0b68491f2a0cf2776c850cbb566022b345", 
         "0x6561cf90fde56d6adccaa818c9bee07e0668d229"],
        [500, 3000],
        "0xfff9976782d46cc05630d1f6ebab18b2324d6b14",
        1  # mode = 1 (pool flash swap for batch)
    ]
    
    opp = create_test_opportunity(block_number=10784500, arb_data_tuple=arb_data)
    
    # Test the real execute function
    arb_data_dict, gas_units, gas_usd = real_execute_func(opp, for_batch=True)
    
    print(f"\nGenerated arb_data dict:")
    print(f"  amountIn: {arb_data_dict['amountIn']}")
    print(f"  minProfit: {arb_data_dict['minProfit']}")
    print(f"  tokens: {arb_data_dict['tokens']}")
    print(f"  pools: {arb_data_dict['pools']}")
    print(f"  fees: {arb_data_dict['fees']}")
    print(f"  tokenIn: {arb_data_dict['tokenIn']}")
    print(f"  mode: {arb_data_dict['mode']}")
    
    # Verify structure
    assert arb_data_dict['amountIn'] == 12240000000000000
    assert arb_data_dict['minProfit'] == 1000000000000000
    assert len(arb_data_dict['tokens']) == 3
    assert len(arb_data_dict['pools']) == 2
    assert arb_data_dict['fees'] == [500, 3000]
    assert Web3.to_checksum_address(arb_data_dict['tokenIn']) == Web3.to_checksum_address("0xfff9976782d46cc05630d1f6ebab18b2324d6b14")
    assert arb_data_dict['mode'] == 1, "Mode should be 1 for batch execution (pool flash swap)"

    
    # Verify all addresses are checksummed
    for token in arb_data_dict['tokens']:
        assert token == Web3.to_checksum_address(token), f"Token {token} should be checksummed"
    for pool in arb_data_dict['pools']:
        assert pool == Web3.to_checksum_address(pool), f"Pool {pool} should be checksummed"
    
    print("\n✅ arb_data structure test PASSED")
    return True


if __name__ == "__main__":
    print("\n" + "="*60)
    print("REAL-TIME TEST: build_batch_calls")
    print("Using actual Web3 connection and ArbExec contract")
    print("="*60)
    
    all_passed = True
    
    try:
        all_passed &= test_arb_data_structure()
    except Exception as e:
        print(f"\n❌ test_arb_data_structure FAILED: {e}")
        all_passed = False
    
    try:
        all_passed &= test_build_batch_calls_empty()
    except Exception as e:
        print(f"\n❌ test_build_batch_calls_empty FAILED: {e}")
        all_passed = False
    
    try:
        all_passed &= test_build_batch_calls_single()
    except Exception as e:
        print(f"\n❌ test_build_batch_calls_single FAILED: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False
    
    try:
        all_passed &= test_build_batch_calls_multiple()
    except Exception as e:
        print(f"\n❌ test_build_batch_calls_multiple FAILED: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False
    
    print("\n" + "="*60)
    if all_passed:
        print("✅ ALL TESTS PASSED")
    else:
        print("❌ SOME TESTS FAILED")
    print("="*60)


