"""Chain-aware transaction relay — THE submission layer (DSS-WO-ENG-015 R4).

This module is restored to the existing architecture as the single
transaction-submission layer for direct RPC submission and supported
Flashbots/private bundle submission, plus receipt handling and relay result
reporting. There is deliberately NO second relay implementation inside
another module: every broadcast in the application flows through
``relay_transaction`` on the path

    ExecutionRouter → ArbExecutor → Builder → Signer → Relay → Receipt/Result

Enforcement at this boundary (every step fails closed):

  1. the master execution switch (EXECUTE_ONCHAIN) must be enabled at
     SUBMISSION time — read from the REAL process environment via
     ``config.execution_enabled()``; a disabled run cannot broadcast;
  2. the RPC-REPORTED chain ID must equal the executor's expected chain —
     re-checked again IMMEDIATELY before the broadcast on BOTH routes
     (direct send and Flashbots bundle submission alike; not only during
     transaction construction or signing);
  3. the signed transaction must originate from the approved signing
     process and correspond to the expected transaction/network: the raw
     payload is decoded (a strict, self-contained RLP decoder for the two
     envelopes the signer produces — legacy EIP-155 and EIP-1559) and its
     consensus chainId must match the expected chain, and the recovered
     signer must be the recorded agent identity;
  4. private submission respects the existing chain-specific MEV policy
     (``mev.policy.ChainMEVPolicy``): an atomic backrun is fail-closed on
     chains without private-submission support;
  5. a failed private submission NEVER falls back to public submission —
     operations that require atomic/private execution fail closed;
  6. errors never expose private keys, credentials or raw signed
     transaction material: exception text is scrubbed through the shared
     structural containment rules (executor/audit.py) with the exact
     signed payload hex registered as forbidden material.

Atomic backruns are fail-closed: they require explicit chain support, a
fresh target block, trigger bytes, and a configured private bundle path.
Other chains use direct submission.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from web3.exceptions import TransactionNotFound

from mev.policy import ChainMEVPolicy
from config import FLASHBOTS_RPCS, execution_enabled
from .errors import ExecutionRefused
from .audit import sanitize_error
from .signer import (
    check_chain_identity,
    load_wallet,
    recorded_agent_address,
)

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
    method: str        # "direct" | "flashbots" | "refused"
    error: str = ""


# ── Signed-payload verification (self-contained, version-stable) ────────────

def _rlp_decode_item(buf: bytes):
    """Decode ONE RLP item from `buf`; return (item, consumed) where item
    is bytes (an RLP string) or a list of decoded items. Minimal strict
    decoder covering the full RLP grammar — exactly enough to decode the
    transactions the signer produces; short reads and trailing bytes fail
    closed. Implemented here (instead of relying on library internals) so
    the signed-payload verification stays self-contained and
    version-stable. Never includes the payload bytes in error messages."""
    if not buf:
        raise ExecutionRefused("malformed signed transaction: empty RLP input")
    b0 = buf[0]
    if b0 <= 0x7F:  # single byte encodes itself
        return buf[0:1], 1
    if b0 <= 0xB7:  # short string
        n = b0 - 0x80
        if len(buf) < 1 + n:
            raise ExecutionRefused(
                "malformed signed transaction: truncated RLP string"
            )
        return buf[1:1 + n], 1 + n
    if b0 <= 0xBF:  # long string
        ll = b0 - 0xB7
        if len(buf) < 1 + ll:
            raise ExecutionRefused(
                "malformed signed transaction: truncated RLP length"
            )
        n = int.from_bytes(buf[1:1 + ll], "big")
        if len(buf) < 1 + ll + n:
            raise ExecutionRefused(
                "malformed signed transaction: truncated RLP string"
            )
        return buf[1 + ll:1 + ll + n], 1 + ll + n
    if b0 <= 0xF7:  # short list
        n = b0 - 0xC0
        header = 1
    else:            # long list
        ll = b0 - 0xF7
        if len(buf) < 1 + ll:
            raise ExecutionRefused(
                "malformed signed transaction: truncated RLP length"
            )
        n = int.from_bytes(buf[1:1 + ll], "big")
        header = 1 + ll
    if len(buf) < header + n:
        raise ExecutionRefused(
            "malformed signed transaction: truncated RLP list"
        )
    payload = buf[header:header + n]
    items = []
    pos = 0
    while pos < len(payload):
        item, used = _rlp_decode_item(payload[pos:])
        items.append(item)
        pos += used
    if pos != len(payload):
        raise ExecutionRefused(
            "malformed signed transaction: trailing RLP data"
        )
    return items, header + n


def _int_from_rlp(item: object, field: str) -> int:
    if isinstance(item, list):
        raise ExecutionRefused(
            f"malformed signed transaction: {field} is not a scalar"
        )
    return int.from_bytes(bytes(item), "big")


def decode_signed_transaction(raw: bytes) -> dict:
    """Decode a SIGNED transaction into its consensus fields.

    The signer produces ONLY two envelopes — legacy EIP-155 and EIP-1559
    typed — and decoding anything else fails closed. This is the decoding
    half of the signed-payload provenance check: the relay compares these
    fields against the expected network before any broadcast. Never
    includes the raw bytes in error messages.
    """
    raw = bytes(raw)
    if not raw:
        raise ExecutionRefused("malformed signed transaction: empty payload")
    if raw[0] == 0x02:
        # EIP-1559: 0x02 || rlp([chainId, nonce, maxPriorityFeePerGas,
        # maxFeePerGas, gas, to, value, data, accessList, yParity, r, s])
        body, used = _rlp_decode_item(raw[1:])
        if used != len(raw) - 1:
            raise ExecutionRefused(
                "malformed signed transaction: trailing bytes after "
                "EIP-1559 body"
            )
        if not isinstance(body, list) or len(body) != 12:
            raise ExecutionRefused(
                "malformed signed transaction: unexpected EIP-1559 field count"
            )
        to = body[5]
        if not isinstance(to, (bytes, bytearray)) or (to and len(to) != 20):
            raise ExecutionRefused(
                "malformed signed transaction: recipient is not an address"
            )
        access_list = body[8]
        if not isinstance(access_list, list):
            raise ExecutionRefused(
                "malformed signed transaction: EIP-1559 access list is "
                "not a list"
            )
        if access_list:
            # The signer never signs an access list, so a signed envelope
            # that carries one is by definition not a payload this engine
            # produced — refuse it before any relay interaction.
            raise ExecutionRefused(
                "signed transaction carries a non-empty EIP-1559 access "
                "list; access lists are not supported at this execution "
                "boundary — refusing"
            )
        return {
            "kind": "eip1559",
            "chain_id": _int_from_rlp(body[0], "chainId"),
            "nonce": _int_from_rlp(body[1], "nonce"),
            "max_priority_fee_per_gas": _int_from_rlp(
                body[2], "maxPriorityFeePerGas"
            ),
            "max_fee_per_gas": _int_from_rlp(body[3], "maxFeePerGas"),
            "gas": _int_from_rlp(body[4], "gas"),
            "to": bytes(to) if to else None,
            "value": _int_from_rlp(body[6], "value"),
            "data": bytes(body[7]),
        }
    if raw[0] == 0x01:
        raise ExecutionRefused(
            "unsupported signed transaction envelope (EIP-2930): the "
            "signer produces only legacy or EIP-1559 — refusing"
        )
    if raw[0] < 0xC0:
        raise ExecutionRefused(
            f"unsupported signed transaction envelope type 0x{raw[0]:02x}: "
            "refusing"
        )
    # Legacy: rlp([nonce, gasPrice, gas, to, value, data, v, r, s])
    body, used = _rlp_decode_item(raw)
    if used != len(raw):
        raise ExecutionRefused(
            "malformed signed transaction: trailing bytes after legacy body"
        )
    if not isinstance(body, list) or len(body) != 9:
        raise ExecutionRefused(
            "malformed signed transaction: unexpected legacy field count"
        )
    to = body[3]
    if not isinstance(to, (bytes, bytearray)) or (to and len(to) != 20):
        raise ExecutionRefused(
            "malformed signed transaction: recipient is not an address"
        )
    v = _int_from_rlp(body[6], "v")
    if v >= 35:  # EIP-155 replay protection
        chain_id: Optional[int] = (v - 35) // 2
    else:
        # pre-EIP-155 signature — no chain binding; refuse (this engine
        # always signs with a chain ID)
        chain_id = None
    return {
        "kind": "legacy",
        "chain_id": chain_id,
        "nonce": _int_from_rlp(body[0], "nonce"),
        "max_priority_fee_per_gas": None,
        "max_fee_per_gas": None,
        "gas": _int_from_rlp(body[2], "gas"),
        "to": bytes(to) if to else None,
        "value": _int_from_rlp(body[4], "value"),
        "data": bytes(body[5]),
        "gas_price": _int_from_rlp(body[1], "gasPrice"),
    }


def _recover_signer(raw: bytes) -> str:
    """Recover the signer address from a raw signed transaction."""
    from eth_account import Account

    try:
        return Account.recover_transaction(bytes(raw))
    except Exception as exc:
        raise ExecutionRefused(
            "signed transaction signature could not be recovered "
            f"({type(exc).__name__})"
        ) from None


def verify_signed_payload(
    raw_tx: bytes,
    *,
    expected_chain_id: int,
    expected_sender: str,
) -> dict:
    """Prove the signed payload originates from the approved signing
    process and corresponds to the expected transaction/network.

    Checks, fail closed:
      * the payload decodes as one of the signer's two envelopes;
      * its consensus chainId equals the expected chain (a payload built
        for another network is refused);
      * the recovered signer equals the recorded agent identity (a payload
        signed by anything but the approved signing path is refused).
    """
    decoded = decode_signed_transaction(raw_tx)
    if decoded["chain_id"] != int(expected_chain_id):
        raise ExecutionRefused(
            "signed transaction was built for chain "
            f"{decoded['chain_id']} but the expected chain is "
            f"{int(expected_chain_id)} — refusing to broadcast"
        )
    signer = _recover_signer(raw_tx)
    if signer.lower() != str(expected_sender).lower():
        raise ExecutionRefused(
            "signed transaction does not originate from the approved "
            "signing process (signer identity mismatch) — refusing to "
            "broadcast"
        )
    return decoded


# ── Flashbots transport (lazy, code-controlled destination) ──────────────────

def _ensure_flashbots_transport(w3, chain_id: int):
    """Attach the Flashbots middleware to the executor's Web3 transport.

    PRIVATE internal setup. The relay destination is read here, at
    construction time, from the code-controlled ``config.FLASHBOTS_RPCS``
    entry for the chain — no relay URL flows through any caller-visible
    parameter, so a caller cannot redirect a signed bundle to another
    transport. The relay-auth identity is the agent wallet itself, loaded
    exclusively through the single authoritative custody path
    (executor/signer.load_wallet) — there is no second relay identity and
    no wallet is ever generated here.

    Attachment is lazy (first private submission after the master switch
    and chain checks passed) and idempotent (the flashbots namespace is
    attached exactly once per Web3 instance). Offline tests attach their
    own middleware to a mock transport instead — a test technique, never a
    runtime execution argument.
    """
    if hasattr(w3, "flashbots"):
        return
    relay_url = FLASHBOTS_RPCS.get(int(chain_id))
    if not relay_url:
        raise ExecutionRefused(
            f"no Flashbots relay configured for chain {int(chain_id)}"
        )
    try:
        from flashbots import flashbot
        account = load_wallet()
        flashbot(w3, account, relay_url)
    except ExecutionRefused:
        raise
    except Exception as exc:
        raise ExecutionRefused(
            "Flashbots transport construction failed "
            f"({type(exc).__name__})"
        ) from None
    log.info(f"Flashbots transport attached | chain={int(chain_id)}")


# ── Submission ───────────────────────────────────────────────────────────────

def relay_transaction(
    w3,
    raw_tx: bytes,
    *,
    expected_chain_id: int,
    use_flashbots: bool = False,
    target_tx: bytes = b"",
    target_block: int | None = None,
) -> RelayResult:
    """
    Submit a signed transaction to the network — the ONLY submission path.

    Args:
        w3:               Web3 instance (HTTP, not WS — for reliability).
        raw_tx:           Signed raw transaction bytes from signer.sign_transaction.
        expected_chain_id: The executor's expected chain — the reported RPC
                           chain must match it here AND immediately before
                           the broadcast.
        use_flashbots:    Submit privately via the bundle relay. There is
                          no public-mempool fallback on this path: private
                          submission failures fail closed.
        target_tx:        Optional signed trigger transaction to front the
                          bundle (atomic backrun; gated by the chain MEV
                          policy).
        target_block:     Block number to target for bundle (current+1 if None).

    Returns:
        RelayResult with success status and tx_hash. Never None.

    Raises:
        ExecutionRefused: master switch disabled, chain-identity mismatch,
            or a signed payload that did not originate from the approved
            signing process — all BEFORE any transport interaction.
    """
    # 1. Master execution switch at the submission boundary — the
    #    authoritative process read. No parameter can override this.
    if not execution_enabled():
        raise ExecutionRefused(
            "execution is disabled (EXECUTE_ONCHAIN) — refusing to submit"
        )

    # 2. Chain identity: the RPC-reported chain must equal the expected
    #    chain before anything else happens.
    reported = check_chain_identity(w3, expected_chain_id)

    # 3. Signed-payload provenance: consensus chainId and signer identity
    #    must match the expected transaction/network.
    sender = recorded_agent_address()
    verify_signed_payload(
        raw_tx,
        expected_chain_id=reported,
        expected_sender=sender,
    )

    # The exact signed payload joins every error surface as forbidden
    # material: a transport exception that echoes the payload hex must
    # never leak it into a log or a caller-visible message.
    payload_hex = bytes(raw_tx).hex()
    forbidden = (payload_hex, "0x" + payload_hex)

    if use_flashbots:
        return _relay_flashbots(
            w3,
            raw_tx,
            chain_id=int(reported),
            target_tx=bytes(target_tx) if target_tx else b"",
            target_block=target_block,
            forbidden=forbidden,
        )
    return _relay_direct(w3, raw_tx, expected_chain_id=reported, forbidden=forbidden)


def _relay_direct(w3, raw_tx: bytes, *, expected_chain_id: int, forbidden: tuple) -> RelayResult:
    """Submit directly via RPC — with an IMMEDIATE pre-broadcast chain
    recheck (the last read before send_raw_transaction)."""
    try:
        check_chain_identity(w3, expected_chain_id)
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
    except ExecutionRefused as e:
        log.warning(f"Direct relay refused: {e}")
        return RelayResult(success=False, tx_hash="", method="refused", error=str(e))
    except Exception as e:
        log.warning(f"Direct relay failed: {sanitize_error(e, *forbidden)}")
        return RelayResult(
            success=False, tx_hash="", method="direct",
            error=sanitize_error(e, *forbidden),
        )
    hex_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
    log.info(f"Submitted directly | tx={hex_hash}")
    return RelayResult(success=True, tx_hash=hex_hash, method="direct")


def _relay_flashbots(
    w3,
    raw_tx: bytes,
    *,
    chain_id: int,
    target_tx: bytes = b"",
    target_block: int | None,
    forbidden: tuple,
) -> RelayResult:
    """Submit via the Flashbots bundle relay. Fail-closed: no public
    fallback, chain-specific MEV policy enforced, fresh target block."""
    policy = ChainMEVPolicy.for_chain(chain_id)
    if not policy.private_backrun:
        return RelayResult(
            False, "", "flashbots",
            f"private atomic backrun not enabled for chain {chain_id}",
        )
    if chain_id not in FLASHBOTS_CHAINS:
        return RelayResult(
            False, "", "flashbots",
            f"no Flashbots relay configured for chain {chain_id}",
        )

    _ensure_flashbots_transport(w3, chain_id)

    try:
        if not target_block:
            target_block = int(w3.eth.block_number) + 1
        block = int(w3.eth.block_number)

        # Never silently move a stale target forward. The simulation and
        # bundle must refer to the same target block.
        if int(target_block) <= block:
            return RelayResult(False, "", "flashbots", "target block is stale")

        if target_tx:
            bundle = [
                {"signed_transaction": target_tx},
                {"signed_transaction": raw_tx},
            ]
        else:
            bundle = [{"signed_transaction": raw_tx}]

        # FINAL chain-identity verification (DSS-WO-ENG-016 §3C — parity
        # with the direct route): after transport preparation and
        # immediately before the bundle leaves the process, the
        # RPC-REPORTED chain must still equal the expected chain. A stale,
        # re-pointed or mismatched transport at this point prevents the
        # submission entirely (fail closed).
        check_chain_identity(w3, chain_id)

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
                # Fail closed: the bundle was not included, so this attempt
                # failed. A previous version fell through here and returned
                # None, which crashed callers reading RelayResult attributes.
                return RelayResult(
                    success=False,
                    tx_hash="",
                    method="flashbots",
                    error="bundle not included in target block",
                )
        else:
            log.warning("Private bundle submission returned no result; refusing public fallback for atomic backrun")
            return RelayResult(success=False, tx_hash="", method="flashbots", error="private bundle rejected")

    except ExecutionRefused as e:
        # A policy refusal (e.g. the final chain-identity recheck) is a
        # REFUSED outcome, not a transport failure — same semantics as the
        # direct route. Nothing was submitted.
        log.warning(f"Private relay refused: {e}")
        return RelayResult(success=False, tx_hash="", method="refused", error=str(e))
    except Exception as e:
        log.warning(
            f"Private bundle relay failed ({sanitize_error(e, *forbidden)}); "
            "refusing public fallback for atomic backrun"
        )
        return RelayResult(
            success=False, tx_hash="", method="flashbots",
            error=sanitize_error(e, *forbidden),
        )


def simulate_bundle(w3, bundle: list[dict], target_block: int) -> dict:
    """Simulate a prepared bundle through the Flashbots transport
    (read-only relay operation used as a pre-flight profit refinement).

    The caller passes signed transaction dicts exactly as they will be
    submitted; this function performs no signing and returns the relay's
    simulation response. Transport failures are reduced to sanitized
    messages (the signed payload hex is registered as forbidden material).
    """
    if not execution_enabled():
        raise ExecutionRefused(
            "execution is disabled (EXECUTE_ONCHAIN) — refusing to simulate"
        )
    forbidden: list[str] = []
    for item in bundle:
        raw = item.get("signed_transaction", b"")
        if raw:
            hexed = bytes(raw).hex()
            forbidden.extend((hexed, "0x" + hexed))
    try:
        sim = w3.flashbots.simulate(bundle, target_block)
    except Exception as e:
        raise ExecutionRefused(
            f"bundle simulation failed: {sanitize_error(e, *forbidden)}"
        ) from None
    return sim


def wait_for_receipt(
    w3,
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
