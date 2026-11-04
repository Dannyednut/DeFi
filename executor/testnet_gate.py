"""Testnet-only execution boundary for the DSS agent (DSS-WO-ENG-003 D).

This module is the single, explicit gate between "DSS can reason about
testnet transactions" and "DSS may actually sign and submit one".

Design invariants (all fail closed):

  * EXECUTE_ONCHAIN (the production execution guard) is untouched and
    stays false. This boundary is a SEPARATE gate that only ever permits
    explicitly allowlisted testnets.
  * The gate is disabled by default: without DSS_TESTNET_EXEC=1 nothing
    in this module will sign or submit anything.
  * Ethereum mainnet (chain ID 1) is ALWAYS rejected — hard-coded, not
    configurable.
  * Unknown chains fail closed. Only chains listed in
    ALLOWED_TESTNET_CHAINS may be used. Initially Sepolia only; adding a
    network requires a documented, individually reviewed change to that
    map (docs/testnet-policy.md records the review requirement).
  * The RPC-reported chain ID is verified immediately before signing AND
    again immediately before submission.
  * Missing wallet credentials, RPC configuration or required config is
    a hard error when the gate is enabled — never a fallback.
  * No arbitrary EOA transfers: the only permitted recipient without an
    entry in KNOWN_TARGETS is the agent wallet itself (self-transfer).
  * No unlimited token approvals: approve/increaseAllowance calldata is
    decoded and bounded amounts are enforced.
  * No unverified contract targets and no contract deployments: `to`
    must be a real recipient (self or a recorded known target);
    deployments (empty `to`) are rejected outright.
  * Every submitted transaction is recorded (purpose, chain ID, target,
    function, estimated/actual gas, hash, outcome) to a JSONL log that
    never contains credentials.

This module holds NO key material. The agent wallet is decrypted from
the DSS secret store (encrypted keystore + passphrase file) only inside
sign_and_submit_testnet_tx and only after every check above passed.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

# ── Policy constants ─────────────────────────────────────────────────────────

MAINNET_CHAIN_ID = 1

# Explicit testnet allowlist. Sepolia only for now. Adding a chain requires
# a documented network-specific review (status, RPC, contract deployments)
# recorded in docs/testnet-policy.md BEFORE it is listed here.
ALLOWED_TESTNET_CHAINS: dict[int, str] = {
    11155111: "Ethereum Sepolia",
}

# Hard expenditure caps for a single testnet transaction.
MAX_TESTNET_TX_VALUE_WEI = 10**15          # 0.001 native units
MAX_TESTNET_GAS_UNITS = 500_000
MAX_TOKEN_APPROVAL_AMOUNT = 10**18         # bounded approvals only

# Well-known approval selectors: approve(address,uint256), increaseAllowance
APPROVAL_SELECTORS = {bytes.fromhex("095ea7b3"), bytes.fromhex("39509351")}

# Verified contract targets permitted as recipients. Deliberately EMPTY:
# until the Coordinator approves specific deployments, the only permitted
# recipient is the agent wallet itself (self-transfers for relay/nonce
# verification). Entries: chain_id -> {address_lower: purpose}
KNOWN_TARGETS: dict[int, dict[str, str]] = {}

# Where the per-transaction audit log is written (logs/*.jsonl is
# gitignored — the log stays in the agent runtime, never in Git).
DEFAULT_TX_LOG_PATH = os.path.join("logs", "testnet_tx_log.jsonl")

AGENT_SECRET_STORE = "/home/z/my-project/.secrets"
DEFAULT_KEYSTORE_PATH = os.path.join(AGENT_SECRET_STORE, "dss_testnet_wallet.keystore.json")
DEFAULT_PASSPHRASE_PATH = os.path.join(AGENT_SECRET_STORE, "dss_testnet_wallet.pass")
DEFAULT_ADDRESS_PATH = os.path.join(AGENT_SECRET_STORE, "dss_testnet_wallet.address")


class TestnetSafetyError(RuntimeError):
    """Raised whenever any testnet safety precondition fails."""

    # Not a pytest test class despite the Test prefix.
    __test__ = False


# ── Gate configuration ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class TestnetExecutionConfig:
    """Everything needed to permit ONE testnet execution session.

    Built from the environment; `enabled` requires the explicit testnet
    execution gate flag. All credential paths point into the DSS secret
    store; the raw key is only ever decrypted in memory at signing time.
    """

    enabled: bool
    rpc_url: Optional[str] = None
    keystore_path: str = DEFAULT_KEYSTORE_PATH
    passphrase_path: str = DEFAULT_PASSPHRASE_PATH
    address_path: str = DEFAULT_ADDRESS_PATH
    expected_chain_id: int = 11155111
    max_value_wei: int = MAX_TESTNET_TX_VALUE_WEI
    max_gas_units: int = MAX_TESTNET_GAS_UNITS
    tx_log_path: str = DEFAULT_TX_LOG_PATH

    # Not a pytest test class despite the Test prefix.
    __test__ = False

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "TestnetExecutionConfig":
        e = env if env is not None else os.environ
        return cls(
            enabled=e.get("DSS_TESTNET_EXEC", "") == "1",
            rpc_url=e.get("DSS_TESTNET_RPC_URL") or None,
            expected_chain_id=int(e.get("DSS_TESTNET_CHAIN_ID", "11155111")),
        )

    def require_ready(self) -> None:
        """Fail closed unless the gate is explicitly enabled AND every
        required input is present."""
        if not self.enabled:
            raise TestnetSafetyError(
                "testnet execution gate is DISABLED "
                "(set DSS_TESTNET_EXEC=1 to enable; it is off by default)"
            )
        if not self.rpc_url:
            raise TestnetSafetyError("DSS_TESTNET_RPC_URL is required when the gate is enabled")
        if self.expected_chain_id not in ALLOWED_TESTNET_CHAINS:
            raise TestnetSafetyError(
                f"chain {self.expected_chain_id} is not on the testnet allowlist"
            )
        for path in (self.keystore_path, self.passphrase_path, self.address_path):
            if not os.path.isfile(path):
                raise TestnetSafetyError(f"agent wallet credential missing: {path}")

    def load_agent_address(self) -> str:
        """Read the (non-secret) recorded agent address and sanity-check it."""
        with open(self.address_path) as f:
            addr = f.read().strip()
        if not addr.startswith("0x") or len(addr) != 42:
            raise TestnetSafetyError("recorded agent address is malformed")
        return addr


# ── Chain identity gate ──────────────────────────────────────────────────────

def check_chain_id(chain_id: int, expected: Optional[int] = None) -> int:
    """Validate a chain ID against the testnet boundary.

    Mainnet is always rejected, unknown chains fail closed, and if an
    `expected` chain is given the reported one must match it exactly.
    """
    chain_id = int(chain_id)
    if chain_id == MAINNET_CHAIN_ID:
        raise TestnetSafetyError(
            "DSS testnet execution path refuses Ethereum mainnet (chain ID 1)"
        )
    if chain_id not in ALLOWED_TESTNET_CHAINS:
        raise TestnetSafetyError(
            f"chain ID {chain_id} is not on the DSS testnet allowlist "
            f"({sorted(ALLOWED_TESTNET_CHAINS)}) — failing closed"
        )
    if expected is not None and int(expected) != chain_id:
        raise TestnetSafetyError(
            f"RPC chain mismatch: expected {expected}, RPC reports {chain_id}"
        )
    return chain_id


# ── Transaction policy ───────────────────────────────────────────────────────

def validate_transaction(
    tx: dict,
    *,
    sender: str,
    chain_id: int,
    max_value_wei: int = MAX_TESTNET_TX_VALUE_WEI,
    max_gas_units: int = MAX_TESTNET_GAS_UNITS,
    known_targets: Optional[dict[int, dict[str, str]]] = None,
) -> dict:
    """Enforce the transaction policy. Returns the tx unchanged if safe.

    Rules (each violation raises TestnetSafetyError):
      * no deployments (`to` must be a real recipient);
      * recipient must be the sender itself or a recorded known target
        for this chain (no arbitrary EOA transfers, no unverified targets);
      * value within the hard cap;
      * gas within the hard cap;
      * approval calldata must be bounded (no unlimited allowances);
      * tx chainId, when present, must equal the verified chain ID.
    """
    targets = KNOWN_TARGETS if known_targets is None else known_targets
    to = tx.get("to")
    if not to or not str(to).strip():
        raise TestnetSafetyError(
            "contract deployment rejected: testnet transactions must target "
            "a reviewed recipient"
        )
    to_lower = str(to).lower()
    sender_lower = str(sender).lower()

    if to_lower != sender_lower and to_lower not in targets.get(int(chain_id), {}):
        raise TestnetSafetyError(
            f"recipient {to_lower} is neither the agent wallet nor a "
            "reviewed known target — refusing (no arbitrary EOA transfers, "
            "no unverified contract targets)"
        )

    value = int(tx.get("value", 0))
    if value < 0 or value > max_value_wei:
        raise TestnetSafetyError(
            f"transaction value {value} wei exceeds the testnet cap "
            f"{max_value_wei} wei"
        )

    gas = int(tx.get("gas", 0) or 0)
    if gas <= 0 or gas > max_gas_units:
        raise TestnetSafetyError(
            f"gas limit {gas} outside the permitted testnet range "
            f"(1, {max_gas_units}]"
        )

    data = tx.get("data") or b""
    if isinstance(data, str):
        data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    if len(data) >= 4 and data[:4] in APPROVAL_SELECTORS:
        # approve(address,uint256) / increaseAllowance(address,uint256):
        # amount is the second 32-byte word (after the address word).
        amount = int.from_bytes(data[4 + 32 : 4 + 64], "big")
        if amount > MAX_TOKEN_APPROVAL_AMOUNT:
            raise TestnetSafetyError(
                "unlimited (or over-cap) token approval rejected; only "
                f"bounded approvals <= {MAX_TOKEN_APPROVAL_AMOUNT} allowed"
            )

    if tx.get("chainId") is not None and int(tx["chainId"]) != int(chain_id):
        raise TestnetSafetyError(
            f"transaction chainId {tx['chainId']} != verified chain {chain_id}"
        )
    return tx


# ── Audit log ────────────────────────────────────────────────────────────────

TX_LOG_FIELDS = (
    "timestamp", "purpose", "chain_id", "target", "function",
    "estimated_gas", "actual_gas_used", "tx_hash", "outcome", "error",
)


def record_testnet_tx(log_path: str, **fields) -> dict:
    """Append one transaction record to the JSONL audit log.

    Credentials never enter this function; unexpected fields are dropped
    so the record shape stays auditable.
    """
    record = {k: fields.get(k) for k in TX_LOG_FIELDS}
    record["timestamp"] = record["timestamp"] or time.time()
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


# ── Signing / submission orchestration ───────────────────────────────────────

def sign_and_submit_testnet_tx(
    tx: dict,
    *,
    purpose: str,
    config: Optional[TestnetExecutionConfig] = None,
    web3_factory: Optional[Callable[[str], object]] = None,
    known_targets: Optional[dict[int, dict[str, str]]] = None,
    receipt_timeout: int = 120,
) -> dict:
    """The ONLY sanctioned path from intention to a live testnet transaction.

    Sequence (every step fails closed):
      1. gate config must be enabled and fully provisioned;
      2. connect to the configured RPC and verify its REPORTED chain ID
         against the allowlist and the configured expectation (pre-sign);
      3. validate the transaction against the policy;
      4. decrypt the agent wallet from the secret store and bind it to the
         recorded agent address;
      5. sign;
      6. re-verify the RPC chain ID (pre-submission);
      7. submit, wait for the receipt, and record the outcome.

    Returns the audit record. Never returns or logs key material.
    """
    from eth_account import Account  # lazy import; no key work before checks

    cfg = config or TestnetExecutionConfig.from_env()
    cfg.require_ready()  # 1

    if web3_factory is None:
        def web3_factory(url: str):
            from web3 import Web3, HTTPProvider
            return Web3(HTTPProvider(url))

    w3 = web3_factory(cfg.rpc_url)  # 2
    if not getattr(w3, "is_connected", lambda: True)():
        raise TestnetSafetyError("testnet RPC is not reachable")
    reported = int(w3.eth.chain_id)
    check_chain_id(reported, cfg.expected_chain_id)  # pre-sign verification

    chain_id = reported
    tx = dict(tx)
    tx["chainId"] = chain_id
    sender = cfg.load_agent_address()
    validate_transaction(
        tx, sender=sender, chain_id=chain_id,
        max_value_wei=cfg.max_value_wei, max_gas_units=cfg.max_gas_units,
        known_targets=known_targets,
    )  # 3

    # 4. Decrypt the agent wallet; bind it to the recorded identity.
    with open(cfg.keystore_path) as f:
        keystore = json.load(f)
    with open(cfg.passphrase_path, "rb") as f:
        passphrase = bytes.fromhex(f.read().strip().decode())
    key_bytes = Account.decrypt(keystore, passphrase)
    account = Account.from_key(key_bytes)
    del key_bytes
    if account.address.lower() != sender.lower():
        raise TestnetSafetyError(
            "agent wallet identity mismatch: keystore does not match the "
            "recorded agent address — refusing to sign"
        )

    if "nonce" not in tx or tx.get("nonce") is None:
        tx["nonce"] = w3.eth.get_transaction_count(sender)
    if tx.get("gas") is None:
        tx["gas"] = 21000
    if tx.get("maxFeePerGas") is None:
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
        tx["maxFeePerGas"] = int(base_fee * 1.5) + w3.to_wei(1, "gwei")
        tx["maxPriorityFeePerGas"] = w3.to_wei(1, "gwei")
        tx["type"] = 2
    estimated_gas = tx["gas"]

    signed = account.sign_transaction(tx)  # 5
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction

    check_chain_id(int(w3.eth.chain_id), cfg.expected_chain_id)  # 6: pre-submission

    tx_hash = w3.eth.send_raw_transaction(raw)  # 7
    tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)

    actual_gas_used = None
    outcome = "submitted"
    error = None
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=receipt_timeout)
        status = receipt.get("status", 0) if hasattr(receipt, "get") else getattr(receipt, "status", 0)
        actual_gas_used = (
            receipt.get("gasUsed") if hasattr(receipt, "get") else getattr(receipt, "gasUsed", None)
        )
        outcome = "mined" if int(status) == 1 else "reverted"
    except Exception as exc:  # receipt timeout — submission already happened
        outcome = "receipt_unavailable"
        error = str(exc)

    selector = "transfer"
    data = tx.get("data") or b""
    if isinstance(data, str):
        data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    if len(data) >= 4:
        selector = "0x" + data[:4].hex()
    elif int(tx.get("value", 0)) > 0:
        selector = "eth_transfer"

    record = record_testnet_tx(
        cfg.tx_log_path,
        purpose=purpose,
        chain_id=chain_id,
        target=str(tx.get("to", "")).lower(),
        function=selector,
        estimated_gas=estimated_gas,
        actual_gas_used=actual_gas_used,
        tx_hash=tx_hash_hex,
        outcome=outcome,
        error=error,
    )
    return record
