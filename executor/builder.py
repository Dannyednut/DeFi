"""
executor/builder.py
===================
Builds the ArbData struct and raw transaction for ArbExec.swap(), and
enforces the transaction policy at the construction boundary
(DSS-WO-ENG-015 R4).

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

Transaction policy (both build paths — single AND batch — pass the SAME
validation; batch execution is not a weaker alternative):

  * expected chain identity (tx chainId == configured chain);
  * the recipient is EXACTLY the chain's configured arbitrage execution
    contract (``ChainConfig.arb_exec_address`` — the chain-specific
    authority); no arbitrary recipient and no contract deployment;
  * ZERO native-token value for the current arbitrage execution path
    (flash-loan funded) — any unexpected native value is refused;
  * gas within the chain's fee-policy ceiling;
  * valid EIP-1559 or legacy fee parameters (never both, integers,
    priority ≤ max fee) within the chain's per-gas ceilings;
  * worst-case monetary exposure (gas × fee price) within the chain's
    per-transaction ceiling — a gas-unit cap is not a monetary cap;
  * no transaction access list (out of policy at this boundary);
  * fail closed on a missing execution profile, a missing chain-specific
    fee policy or a missing configured recipient — no permissive default
    is ever invented for an unreviewed chain.
"""
from __future__ import annotations

from web3 import Web3
from eth_abi import encode
from eth_abi.packed import encode_packed
from eth_utils import keccak
import time

from config import (
    DEFAULT_GAS_UNITS, GAS_MULTIPLIER, ARB_EXEC_ABI, NATIVE_PRICE_USD,
    get_chain,
)
from execution_profile import profile_for, fee_policy_for
from tvl import token_price
from log import get_logger
from .chain_execution import profile_for as _profile_for_reexport  # noqa: F401  (compat re-export)
from .errors import ExecutionRefused
from .audit import sanitize_error
from utils.web3_compat import encode_call

log = get_logger("executor.builder")

# Minimum profit in USD before we bother signing and submitting
MIN_EXECUTION_PROFIT_USD = 0.0

# EIP-1559 base fee multiplier — pays 120% of current base fee as ceiling
BASE_FEE_MULTIPLIER = 1.2


# ── Transaction policy (the construction boundary) ──────────────────────────

def _uint_field(value: object, name: str) -> int:
    """Strict unsigned-integer field parsing: reject bools, floats, strings
    and anything negative — fail closed on malformed inputs."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionRefused(
            f"malformed transaction field {name}: expected integer wei, "
            f"got {type(value).__name__}"
        )
    if value < 0:
        raise ExecutionRefused(f"negative transaction field {name}")
    return value


def expected_recipient(chain_id: int) -> str:
    """The authoritative transaction recipient for a chain: the chain-
    specific ``ChainConfig.arb_exec_address``.

    Fail closed when the chain is unconfigured or has no arbitrage
    execution contract address — there is no default recipient.
    """
    try:
        chain = get_chain(int(chain_id))
    except Exception as exc:
        raise ExecutionRefused(
            f"chain {int(chain_id)} is not configured — failing closed "
            f"({sanitize_error(exc)})"
        ) from None
    address = (chain.arb_exec_address or "").strip()
    if not address:
        raise ExecutionRefused(
            f"chain {int(chain_id)} has no arb_exec_address configured — "
            "failing closed (production execution stays disabled until an "
            "address is configured)"
        )
    return address.lower()


def validate_transaction_policy(tx: dict, *, chain_id: int) -> dict:
    """Enforce the full transaction policy on an UNSIGNED transaction.
    Returns the tx unchanged if safe; every violation raises
    ``ExecutionRefused``.

    BOTH transaction-construction paths (build_execute_tx and
    build_execute_batch_tx) run their result through this validator, so a
    batch transaction cannot bypass any check that a single transaction
    passes (DSS-WO-ENG-015 R4 §2.5).
    """
    chain_id = int(chain_id)

    # Fail closed on a missing execution profile or fee policy: a chain
    # without reviewed limits cannot execute.
    try:
        profile_for(chain_id)
        policy = fee_policy_for(chain_id)
    except ValueError as exc:
        raise ExecutionRefused(sanitize_error(exc)) from None

    # Expected chain identity.
    tx_chain = tx.get("chainId")
    if tx_chain is None:
        raise ExecutionRefused("transaction carries no chainId")
    if int(tx_chain) != chain_id:
        raise ExecutionRefused(
            f"transaction chainId {tx_chain} != configured chain {chain_id}"
        )

    # Recipient: exactly the configured arbitrage execution contract.
    to = tx.get("to")
    if not to or not str(to).strip():
        raise ExecutionRefused(
            "contract deployment rejected: arbitrage transactions must "
            "target the configured execution contract"
        )
    authority = expected_recipient(chain_id)
    if str(to).strip().lower() != authority:
        raise ExecutionRefused(
            f"recipient {str(to).strip().lower()} is not the configured "
            f"arbitrage execution contract {authority} — refusing (no "
            "arbitrary recipients, no deployments)"
        )

    # Zero native-token value for the current arbitrage execution path.
    value = _uint_field(tx.get("value", 0), "value")
    if value > policy.max_tx_value_wei:
        raise ExecutionRefused(
            f"transaction carries {value} wei of native value; the current "
            "arbitrage execution path is flash-loan funded and must carry "
            f"at most {policy.max_tx_value_wei} — refusing unexpected "
            "native value"
        )

    # Gas limit within the chain's ceiling.
    gas = _uint_field(tx.get("gas"), "gas")
    if gas <= 0 or gas > policy.max_gas_units:
        raise ExecutionRefused(
            f"gas limit {gas} outside the permitted range for chain "
            f"{chain_id} (1, {policy.max_gas_units}]"
        )

    # Fee parameters: exactly one envelope, integers, within the chain's
    # per-gas ceilings, priority ≤ max fee.
    has_1559 = tx.get("maxFeePerGas") is not None
    has_legacy = tx.get("gasPrice") is not None
    if has_1559 and has_legacy:
        raise ExecutionRefused(
            "malformed fee inputs: transaction carries both EIP-1559 "
            "(maxFeePerGas) and legacy (gasPrice) fee fields"
        )
    if has_1559:
        price = _uint_field(tx["maxFeePerGas"], "maxFeePerGas")
        if price > policy.max_fee_per_gas_wei:
            raise ExecutionRefused(
                f"maxFeePerGas {price} wei/gas exceeds the chain "
                f"{chain_id} cap {policy.max_fee_per_gas_wei} wei/gas"
            )
        prio = tx.get("maxPriorityFeePerGas")
        if prio is not None:
            prio = _uint_field(prio, "maxPriorityFeePerGas")
            if prio > price:
                raise ExecutionRefused(
                    "malformed fee inputs: maxPriorityFeePerGas exceeds "
                    "maxFeePerGas"
                )
            if prio > policy.max_priority_fee_wei:
                raise ExecutionRefused(
                    f"maxPriorityFeePerGas {prio} wei/gas exceeds the "
                    f"chain {chain_id} cap {policy.max_priority_fee_wei} "
                    "wei/gas"
                )
    elif has_legacy:
        price = _uint_field(tx["gasPrice"], "gasPrice")
        if price > policy.max_fee_per_gas_wei:
            raise ExecutionRefused(
                f"gasPrice {price} wei/gas exceeds the chain {chain_id} "
                f"cap {policy.max_fee_per_gas_wei} wei/gas"
            )
    else:
        raise ExecutionRefused(
            "missing fee input: transaction has neither maxFeePerGas "
            "(EIP-1559) nor gasPrice (legacy) — refusing to bound or sign"
        )

    # Monetary fee exposure: the worst-case expenditure of ONE transaction
    # is gas × fee price — a gas-unit cap is not a monetary cap.
    expenditure = gas * price
    if expenditure > policy.max_tx_fee_wei:
        raise ExecutionRefused(
            f"maximum fee expenditure {expenditure} wei (gas × fee price) "
            f"exceeds the chain {chain_id} cap {policy.max_tx_fee_wei} wei"
        )

    # Access lists are out of policy at this boundary: absent/None/empty
    # list accepted, anything else malformed or refused.
    access_list = tx.get("accessList")
    if access_list is not None and not isinstance(access_list, list):
        raise ExecutionRefused(
            "malformed accessList: expected a list — every non-list value "
            "is rejected"
        )
    if access_list:
        raise ExecutionRefused(
            "access lists are not supported at this execution boundary: "
            "the transaction policy rejects any supplied accessList "
            "(fail closed)"
        )

    return tx


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
    builder_tip_bps: int,
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
        builder_tip_bps:  Builder tip in basis points (1% = 100 bps).
        sender:           EOA address (from private key).
        gas_units:        Gas limit estimate.

    Returns:
        Unsigned transaction dict ready for signing.
    """
    profile = profile_for(int(w3.eth.chain_id))
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
    block_number = latest.get("number", w3.eth.block_number) + 1
    if profile.chain_id == 324:
        priority_fee = 0
    else:
        try:
            priority_fee = int(w3.eth.max_priority_fee)
        except Exception:
            priority_fee = Web3.to_wei(2, "gwei")
    max_fee = int(base_fee * BASE_FEE_MULTIPLIER) + priority_fee

    # The authoritative Exec contract (Dannyednut/EVM @ 898fe82f) exposes a
    # single swap entrypoint; the retired variant's swapFast was removed.
    tx = arb_exec.functions.swap(
        arb_data,
        force_balancer,
        int(builder_tip_bps),
        block_number
    ).build_transaction({
        "from":                 Web3.to_checksum_address(sender),
        "gas":                  int(gas_units),
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "nonce":                w3.eth.get_transaction_count(
                                    Web3.to_checksum_address(sender), "pending"
                                ),
        "chainId":              w3.eth.chain_id,
        "value":                0,
    })

    # Full transaction policy at the construction boundary — the same
    # validator the batch path runs (no weaker alternative).
    validate_transaction_policy(tx, chain_id=int(w3.eth.chain_id))
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
    # try:
    #     priority_fee = w3.eth.max_priority_fee
    # except Exception:
    priority_fee = Web3.to_wei(2, "gwei")

    max_fee = int(base_fee * BASE_FEE_MULTIPLIER) + priority_fee

    tx = arb_exec.functions.multiCall(calls).build_transaction({
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

    # DSS-WO-ENG-015 R4 (batch parity): the batch path runs the SAME full
    # transaction policy as the single path — chain identity, configured
    # recipient, zero native value, gas/fee limits and monetary exposure.
    # Batch execution is not a weaker alternative.
    validate_transaction_policy(tx, chain_id=int(w3.eth.chain_id))
    return tx


# ====================== HELPER FUNCTION ======================
def encode_execute_call(arb_contract, arb_data: dict, force_balancer: bool, builder_tip, valid_until: int) -> bytes:
    """
    Encodes a single call to ArbExec.swap(...)
    """
    # ArbExec.swap(arb, lender, builderFeeBps, validUntilBlock)
    calldata = encode_call(
        arb_contract,
        "swap",
        [
            (
                arb_data["amountIn"],
                arb_data["minProfit"],
                arb_data["tokens"],
                arb_data["pools"],
                arb_data["fees"],
                arb_data["tokenIn"],
                arb_data["mode"]
            ),
            force_balancer,      # lender / force Balancer
            int(builder_tip),    # builderFeeBps
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
        arb_data, gas_units, gas_usd, builder_tip = func(opp, for_batch=True)
        if not arb_data:
            log.debug(f"Skipping batch call for opp with missing data: {opp} | will probably fail if executed")
            continue
        call_data = encode_execute_call(
            arb_contract,
            arb_data=arb_data,
            force_balancer=False,           # or False
            builder_tip=builder_tip,       # or 0
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

def obfuscate_call_data(real_call_datas: list[bytes]) -> list[bytes]:
    """
    Mimics Solidity:
    bytes[] calldata obfuscated = new bytes[](5);
    for(uint i=0; i<5; i++) {
        obfuscated[i] = abi.encodePacked(
            bytes32(keccak256(abi.encode(block.timestamp, i))), 
            realCallData[i]
        );
    }
    """
    obfuscated = []
    
    for i, real_data in enumerate(real_call_datas):
        # 1. abi.encode(block.timestamp, i) → Solidity uint256 + uint256
        timestamp = int(time.time())  # Current timestamp (like block.timestamp)
        encoded_seed = encode(['uint256', 'uint256'], [timestamp, i])
        
        # 2. keccak256(encoded_seed) → 32 bytes
        junk_hash = keccak(encoded_seed)
        
        # 3. abi.encodePacked(bytes32, bytes) → junk_hash + real_data
        obfuscated_item = encode_packed(['bytes32', 'bytes'], [junk_hash, real_data])
        
        obfuscated.append(obfuscated_item)
    
    return obfuscated