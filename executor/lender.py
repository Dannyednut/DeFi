"""
executor/lender.py
==================
Lender selection for flash loan sourcing.

Priority order:
  1. Aave V3 flash loan   — zero fee on most chains, highest liquidity
  2. Balancer flash loan  — zero fee, good for tokens not on Aave
  3. Pool flash swap      — always available, 0.3% fee (mode 1)

Liquidity check: simplest accurate method is balanceOf(token) on the
lending pool contract. Aave holds the actual tokens in its pool contract.
Balancer holds tokens in the Vault.
"""
from __future__ import annotations

from dataclasses import dataclass

from web3 import Web3
from log import get_logger

log = get_logger("executor.lender")

# Balancer Vault addresses per chain
BALANCER_VAULTS: dict[int, str] = {
    1:         "0xBA12222222228d8Ba445958a75a0704d566BF2C8",  # Ethereum
    137:       "0xBA12222222228d8Ba445958a75a0704d566BF2C8",  # Polygon
    42161:     "0xBA12222222228d8Ba445958a75a0704d566BF2C8",  # Arbitrum
    8453:      "0xBA12222222228d8Ba445958a75a0704d566BF2C8",  # Base
    11155111:  "0xBA12222222228d8Ba445958a75a0704d566BF2C8",  # Sepolia
    # BSC, zkSync: Balancer not deployed
}

# Minimal ERC20 balanceOf ABI
_ERC20_BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


@dataclass
class LenderChoice:
    force_balancer: bool
    mode: int           # 0 = external flash loan, 1 = pool flash swap
    lender_name: str    # for logging


def select_lender(
    w3: Web3,
    chain_id: int,
    token_in: str,
    amount_needed: int,
    for_mempool: bool = False
) -> LenderChoice:
    """
    Select the best available lender for the required flash loan amount.

    Args:
        w3:            Web3 instance.
        chain_id:      Current chain ID.
        token_in:      Token to borrow (checksummed).
        amount_needed: Amount required in token base units.
        aave_pool:     Aave V3 pool address from ChainConfig, or None.

    Returns:
        LenderChoice indicating which lender to use and the arb mode.
    """
    token_in = Web3.to_checksum_address(token_in)

    # ── 1. Balancer ───────────────────────────────────────────────────────────
    balancer_vault = BALANCER_VAULTS.get(chain_id)
    if balancer_vault and not for_mempool and not amount_needed == 0:
        try:
            available = _get_token_balance(w3, token_in, balancer_vault)
            if available >= amount_needed:
                log.debug(f"Lender: Balancer | available={available} needed={amount_needed}")
                return LenderChoice(
                    force_balancer=True,
                    mode=0,
                    lender_name="balancer",
                )
            else:
                log.debug(f"Balancer insufficient: available={available} needed={amount_needed}")
        except Exception as e:
            log.debug(f"Balancer liquidity check failed: {e}")

    # ── 2. Pool flash swap (mode 1) ───────────────────────────────────────────
    log.debug("Lender: pool flash swap (mode=1)")
    return LenderChoice(
        force_balancer=False,
        mode=1,
        lender_name="pool_flash_swap",
    )


def get_balancer_vault(chain_id: int) -> str | None:
    """Return Balancer Vault address for the given chain, or None."""
    return BALANCER_VAULTS.get(chain_id)


def _get_token_balance(w3: Web3, token: str, holder: str) -> int:
    """Return ERC20 balance of `holder` for `token`."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token),
        abi=_ERC20_BALANCE_ABI,
    )
    return contract.functions.balanceOf(
        Web3.to_checksum_address(holder)
    ).call()
