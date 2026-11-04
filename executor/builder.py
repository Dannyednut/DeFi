"""
executor/builder.py
===================
Builds the ArbData struct and raw transaction for ArbExec.swap().

ArbData struct field order (must match Solidity):
  tokens      address[]
  pools       address[]
  amountIn    uint256
  tokenIn     address
  fees        uint24[]
  minProfit   uint256
  mode        uint8

Two paths:
  2-pool: use sorted ArbData returned by getProfit() — already optimal
  3+ pool: build ArbData from Opportunity fields + off-chain optimal amount
"""
from __future__ import annotations

from web3 import Web3

from config import DEFAULT_GAS_UNITS, GAS_MULTIPLIER, ARB_EXEC_ABI, NATIVE_PRICE_USD
from tvl import token_price
from log import get_logger

log = get_logger("executor.builder")

# Minimum profit in USD before we bother signing and submitting
MIN_EXECUTION_PROFIT_USD = 0.0

# EIP-1559 base fee multiplier — pays 150% of current base fee as ceiling
BASE_FEE_MULTIPLIER = 1.5


def build_arb_data(
    ad_tuple: tuple | None,
    opp_tokens: list[str],
    opp_pools: list[str],
    opp_fees_pip: list[int],
    amount_in: int,
    token_in: str,
    mode: int,
    min_profit: int = 0,
) -> dict:
    """
    Build the ArbData dict for ArbExec.swap().

    If ad_tuple is provided (from getProfit return), use its sorted fields
    directly. Otherwise build from the raw opportunity fields.

    Args:
        ad_tuple:      ArbData tuple from getProfit() — (tokens, pools, amountIn,
                       tokenIn, fees, minProfit, mode). None for 3+ pools.
        opp_tokens:    Raw token path from Opportunity (fallback if no ad_tuple).
        opp_pools:     Raw pool path from Opportunity (fallback).
        opp_fees_pip:  Fees in pip format (fallback).
        amount_in:     Optimal borrow amount (may override ad_tuple.amountIn).
        token_in:      Token being borrowed.
        mode:          0 = external flash loan, 1 = pool flash swap.
        min_profit:    Minimum profit in tokenIn wei to accept.

    Returns:
        ArbData dict ready for ArbExec.swap().
    """
    w3_cs = Web3.to_checksum_address

    if ad_tuple is not None:
        # Use sorted fields from getProfit — most accurate
        tokens     = [w3_cs(t) for t in ad_tuple[2]]
        pools      = [w3_cs(p) for p in ad_tuple[3]]
        # Use off-chain optimal if it improves on the on-chain amount
        arb_amount = int(amount_in) if amount_in > 0 else int(ad_tuple[0])
        token_in_  = w3_cs(ad_tuple[5])
        fees       = list(ad_tuple[4])
        mode_      = mode  # executor-determined mode overrides

    else:
        # 3+ pool path — build from raw opportunity fields
        tokens     = [w3_cs(t) for t in opp_tokens]
        pools      = [w3_cs(p) for p in opp_pools]
        arb_amount = int(amount_in)
        token_in_  = w3_cs(token_in)
        fees       = list(opp_fees_pip)
        mode_      = mode

    ad_tuple =  {
        "amountIn":   arb_amount,
        "minProfit":  min_profit,
        "tokens":     tokens,
        "pools":      pools,
        "fees":       fees,
        "tokenIn":    token_in_,
        "mode":       mode_,
    }
    # ad_tuple =  (
    #     int(arb_amount),
    #     min_profit,
    #     list(tokens),
    #     list(pools),
    #     list(fees),
    #     token_in_,
    #     mode_,
    # )
    return ad_tuple


def build_execute_tx(
    w3: Web3,
    arb_exec,
    arb_data: dict,
    force_balancer: bool,
    sender: str,
    gas_units: int = DEFAULT_GAS_UNITS,
) -> dict:
    """
    Build the raw EIP-1559 transaction dict for ArbExec.swap().

    Args:
        w3:               Web3 instance.
        arb_exec:         ArbExec contract.
        arb_data:         ArbData dict from build_arb_data().
        force_balancer:   Pass True to force Balancer flash loan.
        sender:           EOA address (from private key).
        gas_units:        Gas limit estimate.

    Returns:
        Unsigned transaction dict ready for signing.
    """
    # EIP-1559 gas pricing
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
    block_number = latest.get("number", w3.eth.block_number) + 1
    try:
        priority_fee = w3.eth.max_priority_fee
    except Exception:
        priority_fee = Web3.to_wei(1, "gwei")

    max_fee = int(base_fee * BASE_FEE_MULTIPLIER) + priority_fee

    tx = arb_exec.functions.swap(
        arb_data,
        force_balancer,
        block_number
    ).build_transaction({
        "from":                 Web3.to_checksum_address(sender),
        "gas":                  int(gas_units * GAS_MULTIPLIER),
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "nonce":                w3.eth.get_transaction_count(
                                    Web3.to_checksum_address(sender), "pending"
                                ),
        "chainId":              w3.eth.chain_id,
        "value":                0,
    })

    return tx

def build_execute_batch_tx(
    w3: Web3,
    arb_exec,
    calls: list,
    sender: str,
    gas_units: int = DEFAULT_GAS_UNITS,
) -> dict:
    """
    Build the raw EIP-1559 transaction dict for ArbExec.multiCall().

    Args:
        w3:               Web3 instance.
        arb_exec:         ArbExec contract.
        calls:            Calls list from build_batch_calls().
        sender:           EOA address (from private key).
        gas_units:        Gas limit estimate.

    Returns:
        Unsigned transaction dict ready for signing.
    """
    # EIP-1559 gas pricing
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
    try:
        priority_fee = w3.eth.max_priority_fee
    except Exception:
        priority_fee = Web3.to_wei(1, "gwei")

    max_fee = int(base_fee * BASE_FEE_MULTIPLIER) + priority_fee

    tx = arb_exec.functions.multiCall(
        calls
    ).build_transaction({
        "from":                 Web3.to_checksum_address(sender),
        "gas":                  int(gas_units * GAS_MULTIPLIER),
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "nonce":                w3.eth.get_transaction_count(
                                    Web3.to_checksum_address(sender), "pending"
                                ),
        "chainId":              w3.eth.chain_id,
        "value":                0,
    })

    return tx


# ====================== HELPER FUNCTION ======================
def encode_execute_call(arb_contract, arb_data: dict, force_balancer: bool, valid_until: int) -> bytes:
    """
    Encodes a single call to ArbExec.swap(...)
    """
    # Note: forceAave was removed, so we only pass 3 parameters after arb_data
    calldata = arb_contract.encodeABI(
        fn_name="swap",
        args=[
            (
                arb_data["amountIn"],
                arb_data["minProfit"],
                arb_data["tokens"],
                arb_data["pools"],
                arb_data["fees"],
                arb_data["tokenIn"],
                arb_data["mode"]
            ),
            # arb_data,
            force_balancer,      # forceBalancer
            valid_until
        ]
    )
    return calldata


# ====================== BUILD BATCH ======================
def build_batch_calls(func, arb_contract, opportunities: list) -> tuple:
    """
    opportunities = [
        {
            "amountIn": 1000000000000000000,
            "minProfit": 5000000000000000,
            "tokens": ["0xTokenA", "0xTokenB", "0xTokenA"],
            "pools": ["0xPool1", "0xPool2"],
            "fees": [3000, 3000],
            "tokenIn": "0xTokenA",
            "mode": 0
        },
        ...
    ]
    """
    calls = []
    total_gas_units = 0
    total_gas_usd = 0
    for opp in opportunities:
        arb_data, gas_units, gas_usd = func(opp, for_batch=True)
        call_data = encode_execute_call(
            arb_contract,
            arb_data=arb_data,
            force_balancer=False,           # or False
            valid_until=opp.block_number+4  # block number
        )
        calls.append(call_data)
        total_gas_units += gas_units
        total_gas_usd += gas_usd
    
    return calls, total_gas_units, total_gas_usd


def is_profitable_after_gas(
    profit_wei: int,
    token_in: str,
    gas_cost_usd: float,
    min_profit_usd: float = MIN_EXECUTION_PROFIT_USD,
) -> bool:
    """
    Return True if profit_wei exceeds gas cost + minimum threshold.

    Args:
        profit_wei:    Expected profit in token_in base units.
        token_in:      Token address for price lookup.
        gas_cost_usd:  Estimated gas cost in USD.
        min_profit_usd: Minimum net profit in USD required.
    """
    from tvl import token_decimals
    try:
        decimals  = token_decimals(token_in)
        price     = token_price(token_in) or NATIVE_PRICE_USD
        profit_usd = (profit_wei / 10 ** decimals) * price
        net        = profit_usd - gas_cost_usd
        if net < min_profit_usd:
            log.debug(f"Profit gate: net=${net:.4f} < min=${min_profit_usd:.2f}")
            return False
        return True
    except Exception as e:
        log.debug(f"Profit gate check failed: {e}")
        return False
