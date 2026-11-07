"""Chain-aware receipt handling for the executor.

DSS-WO-ENG-015: this module NO LONGER submits transactions. The legacy
broadcast helpers were removed so that no code path outside the unified
execution authorization boundary's gate orchestrations
(``executor/testnet_gate.py``, reached through ``executor/authorization.py``)
can sign or broadcast a raw transaction. Only read-only receipt handling
remains here.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class RelayResult:
    success: bool
    tx_hash: str       # hex string, empty on failure
    method: str        # "direct" | "flashbots" | "refused"
    error: str = ""


def wait_for_receipt(
    w3,
    tx_hash: str,
    timeout: int = 60,
    poll_interval: float = 2.0,
) -> dict | None:
    """
    Wait for transaction receipt. Returns receipt dict or None on timeout.
    Read-only: receipts are fetched for result accounting after the
    authorization boundary has already submitted a transaction.
    """
    from log import get_logger

    log = get_logger("executor.relay")
    try:
        receipt = w3.eth.wait_for_transaction_receipt(
            tx_hash,
            timeout=timeout,
            poll_latency=poll_interval,
        )
        return dict(receipt)
    except Exception as e:
        log.debug(f"Receipt wait timed out for {tx_hash}: {e}")
        return None
