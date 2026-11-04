"""
executor/signer.py
==================
Transaction signing using the private key from config.

Handles EIP-1559 transactions only (type 2).
Private key is loaded once at import time from config.PRIVATE_KEY.
"""
from __future__ import annotations

from eth_account import Account
from web3 import Web3

from config import PRIVATE_KEY
from log import get_logger

log = get_logger("executor.signer")

if not PRIVATE_KEY:
    log.warning("PRIVATE_KEY not set — executor will not be able to sign transactions")

# Derive sender address once
SENDER_ADDRESS: str = ""
if PRIVATE_KEY:
    try:
        SENDER_ADDRESS = Account.from_key(PRIVATE_KEY).address
        log.info(f"Executor signer ready | address={SENDER_ADDRESS}")
    except Exception as e:
        log.error(f"Invalid PRIVATE_KEY: {e}")


def sign_transaction(w3: Web3, tx: dict) -> bytes:
    """
    Sign an EIP-1559 transaction and return the raw signed bytes.

    Args:
        w3: Web3 instance (used for chain_id validation).
        tx: Unsigned transaction dict from builder.build_execute_tx().

    Returns:
        Raw signed transaction bytes ready for broadcast.

    Raises:
        RuntimeError: If PRIVATE_KEY is not configured.
        ValueError:   If transaction dict is malformed.
    """
    if not PRIVATE_KEY:
        raise RuntimeError("PRIVATE_KEY not configured — cannot sign transaction")

    signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    log.debug(f"Signed tx | hash={signed.hash.hex()} nonce={tx.get('nonce')}")
    return signed.rawTransaction


def get_sender() -> str:
    """Return the EOA address derived from PRIVATE_KEY."""
    if not SENDER_ADDRESS:
        raise RuntimeError("PRIVATE_KEY not configured — sender address unknown")
    return SENDER_ADDRESS
