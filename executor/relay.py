"""Chain-aware transaction relay.

Atomic backruns are fail-closed: they require explicit chain support, a target
block, trigger bytes, and a configured private bundle path. Other chains use
direct submission unless an operator adds a chain-specific private endpoint.
"""
from __future__ import annotations
from dataclasses import dataclass

from web3 import Web3
from web3.exceptions import TransactionNotFound
from utils.serializer import serialize
from chain_context import ChainContext
from mev.policy import ChainMEVPolicy
from .chain_execution import profile_for
from config import FLASHBOTS_RPCS

from log import get_logger

log = get_logger("executor.relay")

# Chains this process is configured to relay Flashbots-style bundles on.
# Derived from config.FLASHBOTS_RPCS, which is itself filtered against
# execution_profile.PROFILES[...].private_submission — see config.py.
FLASHBOTS_CHAINS: frozenset[int] = frozenset(FLASHBOTS_RPCS.keys())

@dataclass
class RelayResult:
    success: bool
    tx_hash: str       # hex string, empty on failure
    method: str        # "direct" | "flashbots"
    error: str = ""


def relay_transaction(
    w3: Web3,
    raw_tx: bytes,
    target_tx: bytes = b"",
    target_block: int | None = None,
    use_flashbots: bool = False
) -> RelayResult:
    """
    Submit a signed transaction to the network.

    Args:
        w3:           Web3 instance (HTTP, not WS — for reliability).
        raw_tx:       Signed raw transaction bytes.
        chain_id:     Current chain ID.
        target_block: Block number to target for bundle (current+1 if None).
        relay_signer: Optional signer address for bundle relay (for Base/Arbitrum).

    Returns:
        RelayResult with success status and tx_hash.
    """

    chain_id = int(w3.eth.chain_id)
    ChainContext.from_config(chain_id)
    if use_flashbots:
        policy = ChainMEVPolicy.for_chain(chain_id)
        if not policy.private_backrun:
            return RelayResult(False, "", "flashbots", f"private atomic backrun not enabled for chain {chain_id}")
        if not target_block:
            target_block = int(w3.eth.block_number) + 1
        if target_block <= int(w3.eth.block_number):
            return RelayResult(False, "", "flashbots", "target block is stale")
        return _relay_flashbots(w3, raw_tx, target_block, target_tx)
    return _relay_direct(w3, raw_tx)


def _relay_direct(w3: Web3, raw_tx: bytes) -> RelayResult:
    """Submit directly via RPC."""
    try:
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        hex_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
        log.info(f"Submitted directly | tx={hex_hash}")
        return RelayResult(success=True, tx_hash=hex_hash, method="direct")
        # return RelayResult(success=True, tx_hash="", method="direct")
    except Exception as e:
        log.warning(f"Direct relay failed: {e}")
        return RelayResult(success=False, tx_hash="", method="direct", error=str(e))


def _relay_flashbots(
    w3: Web3,
    raw_tx: bytes,
    target_block: int,
    target_tx: bytes = None,
) -> RelayResult:
    """Submit via Flashbots bundle relay."""
    try:
        if target_tx:
            bundle = [
                {"signed_transaction": target_tx},
                {"signed_transaction": raw_tx},
            ]
        else:
            bundle = [{"signed_transaction": raw_tx}]
        block = w3.eth.block_number

        # Never silently move a stale target forward. The simulation and bundle
        # must refer to the same target block.
        if int(target_block) <= int(block):
            return RelayResult(False, "", "flashbots", "target block is stale")
        result = w3.flashbots.send_bundle(bundle, target_block_number=int(target_block))

        if result:
            # Flashbots bundles don't have a tx_hash until included
            bundle_hash = result.bundle_hash().hex() if hasattr(result, "bundle_hash") else "pending"
            log.info(f"Bundle submitted via Flashbots | block={block} bundle={bundle_hash[:16]}")

            result.wait()
            try:
                receipts = result.receipts()
                log.info(f"Bundle was mined in block {receipts[0].blockNumber}")
                return RelayResult(success=True, tx_hash=bundle_hash, method="flashbots")
            except TransactionNotFound:
                log.info(f"Bundle not found in block {block + 1}")
                cancel_res = w3.flashbots.cancel_bundles(bundle_hash)
                log.info(f"Canceled {cancel_res}")
        else:
            log.warning("Private bundle submission returned no result; refusing public fallback for atomic backrun")
            return RelayResult(success=False, tx_hash="", method="flashbots", error="private bundle rejected")
        # return RelayResult(success=True, tx_hash="", method="flashbots")

    except Exception as e:
        log.warning(f"Private bundle relay failed ({e}); refusing public fallback for atomic backrun")
        return RelayResult(success=False, tx_hash="", method="flashbots", error=str(e))


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
