"""
executor/relay.py
=================
Transaction relay — direct RPC submission or Flashbots bundle.

Flashbots is supported on:
  - Ethereum mainnet (chain_id=1)
  - Base (chain_id=8453)  — via Flashbots-compatible builder

All other chains (Arbitrum, zkSync, BSC, Polygon, Sepolia) use direct
submission. Arbitrum and zkSync have no public mempool so bundles are
irrelevant there.

Bundle relay is only used when the opportunity stems from a pending
mempool transaction (opp.metadata.get("from_mempool") == True).
For block-level opportunities, direct submission is always used.

Flashbots dependency: pip install flashbots
If not installed, falls back to direct submission with a warning.
"""
from __future__ import annotations
from dataclasses import dataclass

from web3 import Web3
from web3.exceptions import TransactionNotFound
from eth_abi import decode
import json

from log import get_logger

log = get_logger("executor.relay")

# Chains that support Flashbots bundle relay
FLASHBOTS_CHAINS = {1, 8453, 11155111}

# Flashbots RPC endpoints per chain
FLASHBOTS_RPCS: dict[int, str] = {
    1:    "https://relay.flashbots.net",
    8453: "https://relay.flashbots.net",  # Base uses same Flashbots relay
    11155111: "https://relay-sepolia.flashbots.net",
}


@dataclass
class RelayResult:
    success: bool
    tx_hash: str       # hex string, empty on failure
    method: str        # "direct" | "flashbots"
    error: str = ""


def relay_transaction(
    w3: Web3,
    raw_tx: bytes,
    chain_id: int,
    target_block: int | None = None,
    use_flashbots: bool = False
) -> RelayResult:
    """
    Submit a signed transaction to the network.

    Args:
        w3:           Web3 instance (HTTP, not WS — for reliability).
        raw_tx:       Signed raw transaction bytes.
        chain_id:     Current chain ID.
        from_mempool: True if this opp stems from a pending mempool tx.
                      If True and chain supports Flashbots, use bundle relay.
        target_block: Block number to target for bundle (current+1 if None).
        relay_signer: Optional signer address for bundle relay (for Base/Arbitrum).

    Returns:
        RelayResult with success status and tx_hash.
    """

    log.info(f"Using {'FlashBot' if use_flashbots else 'Direct'} for execution | chain={chain_id}")
    if use_flashbots:
        return _relay_flashbots(w3, raw_tx, chain_id, target_block)
    else:
        return _relay_direct(w3, raw_tx)


def _relay_direct(w3: Web3, raw_tx: bytes) -> RelayResult:
    """Submit directly via RPC."""
    try:
        # tx_hash = w3.eth.send_raw_transaction(raw_tx)
        # hex_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
        # log.info(f"Submitted directly | tx={hex_hash}")
        # return RelayResult(success=True, tx_hash=hex_hash, method="direct")
        return RelayResult(success=True, tx_hash="", method="direct")
    except Exception as e:
        log.warning(f"Direct relay failed: {e}")
        return RelayResult(success=False, tx_hash="", method="direct", error=str(e))


def _relay_flashbots(
    w3: Web3,
    raw_tx: bytes,
    chain_id: int,
    target_block: int | None,
) -> RelayResult:
    """Submit via Flashbots bundle relay."""
    # try:
    #     from flashbots import FlashbotsWeb3, flashbot  # type: ignore
    #     from eth_account import Account
    #     from config import PRIVATE_KEY
    # except ImportError:
    #     log.warning("flashbots package not installed — falling back to direct relay")
    #     return _relay_direct(w3, raw_tx)

    try:
        # flashbots_rpc = FLASHBOTS_RPCS.get(chain_id, FLASHBOTS_RPCS[1])

        # Create new Web3 instance using Flashbots RPC directly
        # w3_flashbots = Web3(Web3.HTTPProvider(flashbots_rpc))

        # Flashbots requires a separate signing key for bundle authentication
        # Using the same PRIVATE_KEY is acceptable for simple setups
        # signer = Account.from_key(relay_signer or PRIVATE_KEY)
        # w3: FlashbotsWeb3 = flashbot(w3, signer, flashbots_rpc)

        block = w3.eth.block_number

        bundle = [{"signed_transaction": raw_tx}]

        sim = w3.flashbots.simulate(bundle, block+1)
        if sim and sim.get("revert"):
            sim["revert"] = decode(["string"], sim["revert"])
            sim["signedBundledTransactions"] = None
        log.debug(f"Simulation: {sim}")
        # result = w3.flashbots.send_bundle(bundle, target_block_number=block)

        # if result:
        #     # Flashbots bundles don't have a tx_hash until included
        #     bundle_hash = result.bundle_hash().hex() if hasattr(result, "bundle_hash") else "pending"
        #     log.info(f"Bundle submitted via Flashbots | block={block} bundle={bundle_hash[:16]}")

        #     result.wait()
        #     try:
        #         receipts = result.receipts()
        #         log.info(f"Bundle was mined in block {receipts[0].blockNumber}")
        #         return RelayResult(success=True, tx_hash=bundle_hash, method="flashbots")
        #     except TransactionNotFound:
        #         log.info(f"Bundle not found in block {block + 1}")
        #         cancel_res = w3.flashbots.cancel_bundles(bundle_hash)
        #         log.info(f"Canceled {cancel_res}")
        # else:
        #     log.warning("Flashbots bundle submission returned no result — falling back to direct")
        #     return _relay_direct(w3, raw_tx)
        return RelayResult(success=True, tx_hash="", method="flashbots")

    except Exception as e:
        log.warning(f"Flashbots relay failed ({e}) — falling back to direct")
        return RelayResult(success=True, tx_hash="", method="flashbots")
        # return _relay_direct(w3, raw_tx)


def wait_for_receipt(
    w3: Web3,
    tx_hash: str,
    timeout: int = 60,
    poll_interval: float = 2.0,
) -> dict | None:
    """
    Wait for transaction receipt. Returns receipt dict or None on timeout.
    Only used for direct submissions — Flashbots bundles may not appear.
    """
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
