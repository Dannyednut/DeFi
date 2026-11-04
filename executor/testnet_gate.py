"""Testnet-only execution boundary for the DSS agent (DSS-WO-ENG-003 D).

Unified signing and submission authority per DSS-WO-ENG-004 §3: this module
is the SINGLE authorization boundary for every testnet signing operation —
direct testnet RPC submission AND Flashbots bundle simulation/submission.
The Flashbots relay remains a separate transport only; it never creates an
independent wallet-loading, authorization or transaction-policy path.

Design invariants (all fail closed):

  * EXECUTE_ONCHAIN (the production execution guard) is untouched and
    stays false. This boundary is a SEPARATE gate that only ever permits
    explicitly allowlisted testnets.
  * The gate is disabled by default: without DSS_TESTNET_EXEC=1 nothing
    in this module will sign or submit anything. Bundle submission
    additionally requires a SUCCESSFUL simulation of the exact payload
    (see FlashbotsBundleSession — enforced in production code, not in
    tests).
  * Ethereum mainnet (chain ID 1) is ALWAYS rejected — hard-coded, not
    configurable.
  * Unknown chains fail closed. Only chains listed in
    ALLOWED_TESTNET_CHAINS may be used. Initially Sepolia only; adding a
    network requires a documented, individually reviewed change to that
    map (docs/testnet-policy.md records the review requirement). The
    production allowlist can NEVER be changed from the environment; a
    controlled allowlist can be injected explicitly by offline tests.
  * The RPC-reported chain ID is verified immediately before signing AND
    again immediately before submission (both transports).
  * Missing wallet credentials, RPC configuration or required config is
    a hard error when the gate is enabled — never a fallback.
  * Wallet custody: there is exactly ONE wallet-loading path
    (load_agent_wallet). It verifies secret-file presence, file types
    (no symlinks), restrictive permissions and directory exposure, then
    decrypts the v3 keystore with the hex-encoded passphrase file, builds
    a proper eth_account LocalAccount, and BINDS it to the recorded
    public address before any signing can happen.
  * Fee exposure is capped in monetary terms, not just gas units:
    gasLimit × maxFeePerGas (EIP-1559) or gasLimit × gasPrice (legacy)
    must be within MAX_TESTNET_TX_FEE_WEI; fee-price inputs that are
    missing, negative, malformed or above MAX_TESTNET_MAX_FEE_PER_GAS_WEI
    are rejected.
  * No arbitrary EOA transfers: the only permitted recipient without an
    entry in KNOWN_TARGETS is the agent wallet itself (self-transfer).
  * No unlimited token approvals: approve/increaseAllowance calldata is
    decoded and bounded amounts are enforced.
  * No unverified contract targets and no contract deployments: `to`
    must be a real recipient (self or a recorded known target);
    deployments (empty `to`) are rejected outright.
  * Every simulation and every submission is recorded (purpose, chain
    ID, target, function, estimated/actual gas, hash, outcome, fee
    exposure) to a JSONL log that never contains credentials.

This module holds NO key material. The agent wallet is decrypted from
the DSS secret store (encrypted keystore + passphrase file) only inside
load_agent_wallet, which every signing path must call.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from eth_account.signers.local import LocalAccount

# ── Policy constants ─────────────────────────────────────────────────────────

MAINNET_CHAIN_ID = 1

# Explicit testnet allowlist. Sepolia only for now. Adding a chain requires
# a documented network-specific review (status, RPC, contract deployments)
# recorded in docs/testnet-policy.md BEFORE it is listed here. Offline tests
# may inject a controlled allowlist via explicit function/config arguments;
# the production map below can never be altered from the environment.
ALLOWED_TESTNET_CHAINS: dict[int, str] = {
    11155111: "Ethereum Sepolia",
}

# Hard expenditure caps for a single testnet transaction.
MAX_TESTNET_TX_VALUE_WEI = 10**15          # 0.001 native units
MAX_TESTNET_GAS_UNITS = 500_000
MAX_TOKEN_APPROVAL_AMOUNT = 10**18         # bounded approvals only

# Maximum monetary fee exposure (DSS-WO-ENG-004 §5). A gas-unit cap is NOT
# a monetary cap: these bounds fail closed on the actual fee inputs.
#
#   * MAX_TESTNET_MAX_FEE_PER_GAS_WEI — sanity ceiling on the fee price
#     itself (maxFeePerGas / gasPrice). Sepolia base fees are typically
#     0.01–30 gwei; 50 gwei covers even severe congestion.
#   * MAX_TESTNET_MAX_PRIORITY_FEE_WEI — priority-fee ceiling (2 gwei is
#     far above what Sepolia inclusion requires).
#   * MAX_TESTNET_TX_FEE_WEI — hard ceiling on the total worst-case
#     expenditure per transaction: gasLimit × maxFeePerGas (EIP-1559) or
#     gasLimit × gasPrice (legacy).
#
# Proposed for the initial 0-value self-transfer (21,000 gas): worst-case
# expenditure 21,000 × 50 gwei = 1.05e15 wei ≈ 0.00105 native; the ceiling
# 2e15 wei (0.002 native) is ~1.9× that, i.e. it absorbs one full retry at
# the worst-case price. Coordinator review is required before any live
# transaction; raising these caps is a reviewed code change, never an
# environment setting.
MAX_TESTNET_MAX_FEE_PER_GAS_WEI = 50 * 10**9      # 50 gwei per gas
MAX_TESTNET_MAX_PRIORITY_FEE_WEI = 2 * 10**9      # 2 gwei per gas
MAX_TESTNET_TX_FEE_WEI = 2 * 10**15               # 0.002 native total fee

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

# Secret files must expose no group/world bits; their directory must not be
# accessible to other users. The recorded address file is public metadata and
# is only checked for presence/type.
SECRET_MODE_MASK = 0o077

# Flashbots transport for the allowlisted testnet. Verified provenance:
# Flashbots official docs, "Flashbots Auction > Testnets" (page updated
# 2025-12-16). Changing the relay is a reviewed code change, never an
# environment setting.
FLASHBOTS_SEPOLIA_RELAY = "https://relay-sepolia.flashbots.net"


class TestnetSafetyError(RuntimeError):
    """Raised whenever any testnet safety precondition fails."""

    # Not a pytest test class despite the Test prefix.
    __test__ = False


def effective_allowlist(
    allowlist: Optional[dict[int, str]] = None,
) -> dict[int, str]:
    """Return the allowlist in force: the production map unless a controlled
    test allowlist was explicitly injected. The environment can never change
    this."""
    return ALLOWED_TESTNET_CHAINS if allowlist is None else allowlist


# ── Gate configuration ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class TestnetExecutionConfig:
    """Everything needed to permit ONE testnet execution session.

    Built from the environment; `enabled` requires the explicit testnet
    execution gate flag. All credential paths point into the DSS secret
    store; the raw key is only ever decrypted in memory by
    load_agent_wallet. The allowlist and relay URL are NOT environment
    configurable — changing them is a reviewed code change (offline tests
    may inject an allowlist explicitly).
    """

    enabled: bool
    rpc_url: Optional[str] = None
    keystore_path: str = DEFAULT_KEYSTORE_PATH
    passphrase_path: str = DEFAULT_PASSPHRASE_PATH
    address_path: str = DEFAULT_ADDRESS_PATH
    expected_chain_id: int = 11155111
    max_value_wei: int = MAX_TESTNET_TX_VALUE_WEI
    max_gas_units: int = MAX_TESTNET_GAS_UNITS
    max_fee_per_gas_wei: int = MAX_TESTNET_MAX_FEE_PER_GAS_WEI
    max_priority_fee_wei: int = MAX_TESTNET_MAX_PRIORITY_FEE_WEI
    max_total_fee_wei: int = MAX_TESTNET_TX_FEE_WEI
    tx_log_path: str = DEFAULT_TX_LOG_PATH
    relay_url: str = FLASHBOTS_SEPOLIA_RELAY
    # Test injection only; from_env never sets it.
    allowlist: Optional[dict[int, str]] = None

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

    def effective_allowlist(self) -> dict[int, str]:
        return effective_allowlist(self.allowlist)

    def require_provisioned(self) -> None:
        """Fail closed unless every required input is present and the
        expected chain is allowlisted — WITHOUT requiring the execution
        flag. Read-only relay operations (bundle simulation) use this
        level; any submission additionally requires require_ready()."""
        if not self.rpc_url:
            raise TestnetSafetyError(
                "DSS_TESTNET_RPC_URL is required when the gate is enabled"
            )
        if self.expected_chain_id not in self.effective_allowlist():
            raise TestnetSafetyError(
                f"chain {self.expected_chain_id} is not on the testnet allowlist"
            )
        verify_secret_store(self)

    def require_ready(self) -> None:
        """Fail closed unless the gate is explicitly enabled AND every
        required input is present, the expected chain is allowlisted and
        the secret store passes custody checks."""
        if not self.enabled:
            raise TestnetSafetyError(
                "testnet execution gate is DISABLED "
                "(set DSS_TESTNET_EXEC=1 to enable; it is off by default)"
            )
        self.require_provisioned()

    def load_agent_address(self) -> str:
        """Read the (non-secret) recorded agent address and sanity-check it."""
        with open(self.address_path) as f:
            addr = f.read().strip()
        if not addr.startswith("0x") or len(addr) != 42:
            raise TestnetSafetyError("recorded agent address is malformed")
        return addr


# ── Chain identity gate ──────────────────────────────────────────────────────

def check_chain_id(
    chain_id: int,
    expected: Optional[int] = None,
    allowlist: Optional[dict[int, str]] = None,
) -> int:
    """Validate a chain ID against the testnet boundary.

    Mainnet is always rejected, unknown chains fail closed, and if an
    `expected` chain is given the reported one must match it exactly.
    `allowlist` may be injected by offline tests to exercise the
    allowlisted-but-unexpected branch; production always uses the
    Sepolia-only map.
    """
    chain_id = int(chain_id)
    if chain_id == MAINNET_CHAIN_ID:
        raise TestnetSafetyError(
            "DSS testnet execution path refuses Ethereum mainnet (chain ID 1)"
        )
    chains = effective_allowlist(allowlist)
    if chain_id not in chains:
        raise TestnetSafetyError(
            f"chain ID {chain_id} is not on the DSS testnet allowlist "
            f"({sorted(chains)}) — failing closed"
        )
    if expected is not None and int(expected) != chain_id:
        raise TestnetSafetyError(
            f"RPC chain mismatch: expected {expected}, RPC reports {chain_id}"
        )
    return chain_id


# ── Wallet custody: the single wallet-loading path ───────────────────────────

def _check_restricted_file(path: str, label: str) -> None:
    """Presence + file type + restrictive permissions, fail closed."""
    if os.path.islink(path):
        raise TestnetSafetyError(
            f"agent {label} must be a regular file, not a symlink: {path}"
        )
    if not os.path.isfile(path):
        raise TestnetSafetyError(f"agent {label} missing or not a regular file: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & SECRET_MODE_MASK:
        raise TestnetSafetyError(
            f"agent {label} permissions too open ({oct(mode)}): group/world "
            "access must be removed (0600 expected)"
        )


def _check_secret_dir(path: str, label: str) -> None:
    if os.path.islink(path):
        raise TestnetSafetyError(
            f"agent {label} directory must not be a symlink: {path}"
        )
    if not os.path.isdir(path):
        raise TestnetSafetyError(f"agent {label} directory missing: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & SECRET_MODE_MASK:
        raise TestnetSafetyError(
            f"agent {label} directory accessible to other users ({oct(mode)}): "
            "0700 expected"
        )


def verify_secret_store(config: TestnetExecutionConfig) -> None:
    """Verify presence, type and permissions of every secret-store file
    before any live operation (DSS-WO-ENG-004 §2 req 7, §7).

    Fail closed on: missing files, non-regular files, symlinks, group/world
    access on keystore/passphrase, group/world access on their directory,
    or a malformed recorded address. Never reads secret contents.
    """
    for label, path in (
        ("keystore", config.keystore_path),
        ("passphrase", config.passphrase_path),
    ):
        _check_restricted_file(path, label)
        _check_secret_dir(os.path.dirname(path) or ".", label)
    addr = config.address_path
    if os.path.islink(addr) or not os.path.isfile(addr):
        raise TestnetSafetyError(
            f"agent recorded-address file missing, a symlink or not a regular "
            f"file: {addr}"
        )
    config.load_agent_address()


def decode_passphrase_file(path: str) -> bytes:
    """Decode the keystore passphrase file — THE central keystore format.

    The DSS secret store stores the ASCII hex encoding of the passphrase
    bytes (machine-generated at wallet creation; see
    scripts/dss_wallet_generate.py). This is the only supported decoding;
    there is no environment override and no raw-text fallback.
    """
    with open(path, "rb") as f:
        raw = f.read().strip()
    try:
        return bytes.fromhex(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TestnetSafetyError(
            "agent keystore passphrase file is not valid hex-encoded "
            "passphrase data (central keystore format)"
        ) from exc


def load_agent_wallet(config: TestnetExecutionConfig) -> "LocalAccount":
    """THE single wallet-loading path for every testnet signing operation.

    Sequence (fail closed):
      1. verify_secret_store (presence, types, symlinks, permissions);
      2. load the web3 secret-storage v3 keystore JSON (shape-validated);
      3. decode the hex-encoded passphrase file (central keystore format);
      4. decrypt via eth_account and build a proper LocalAccount with
         Account.from_key (the supported eth-account interface);
      5. BIND the derived address to the recorded public address file —
         a mismatch refuses to return the account at all.

    Returns the in-memory LocalAccount only. The key bytes and passphrase
    are dropped before returning and are never printed, logged or
    serialized.
    """
    from eth_account import Account

    verify_secret_store(config)

    with open(config.keystore_path) as f:
        keystore = json.load(f)
    if (
        not isinstance(keystore, dict)
        or int(keystore.get("version", 0)) != 3
        or not isinstance(keystore.get("crypto"), dict)
    ):
        raise TestnetSafetyError(
            "agent keystore is not a web3 secret-storage v3 JSON keystore"
        )

    passphrase = decode_passphrase_file(config.passphrase_path)
    key_bytes = Account.decrypt(keystore, passphrase)
    try:
        account = Account.from_key(key_bytes)
    finally:
        del key_bytes, passphrase

    recorded = config.load_agent_address()
    if account.address.lower() != recorded.lower():
        raise TestnetSafetyError(
            "agent wallet identity mismatch: keystore does not match the "
            "recorded agent address — refusing to sign"
        )
    return account


# ── Fee exposure policy (DSS-WO-ENG-004 §5) ─────────────────────────────────

def _uint_field(value: object, name: str) -> int:
    """Strict unsigned-integer field parsing: reject bools, floats, strings
    and anything negative — fail closed on malformed inputs."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TestnetSafetyError(
            f"malformed transaction field {name}: expected integer wei, "
            f"got {type(value).__name__}"
        )
    if value < 0:
        raise TestnetSafetyError(f"negative transaction field {name}")
    return value


def validate_fee_exposure(
    tx: dict,
    *,
    max_fee_per_gas_wei: int = MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
    max_priority_fee_wei: int = MAX_TESTNET_MAX_PRIORITY_FEE_WEI,
    max_total_fee_wei: int = MAX_TESTNET_TX_FEE_WEI,
) -> int:
    """Fail closed on the monetary fee exposure of one transaction.

    A gas-unit cap is not a monetary cap. This validates the ACTUAL fee
    inputs of the transaction that will be signed:

      * EIP-1559 (maxFeePerGas present): expenditure bound is
        gas × maxFeePerGas (the maximum the signer authorised); the
        priority fee must be an integer, ≤ maxFeePerGas and ≤ cap;
        a legacy gasPrice must NOT also be present.
      * Legacy (gasPrice present): expenditure bound is gas × gasPrice.
      * Neither present, both present, negative/malformed/over-cap values:
        rejected.

    Returns the worst-case expenditure in wei.
    """
    gas = _uint_field(tx.get("gas"), "gas")
    if gas <= 0:
        raise TestnetSafetyError(
            f"gas limit {gas} outside the permitted testnet range (1, …]"
        )

    has_1559 = tx.get("maxFeePerGas") is not None
    has_legacy = tx.get("gasPrice") is not None
    if has_1559 and has_legacy:
        raise TestnetSafetyError(
            "malformed fee inputs: transaction carries both EIP-1559 "
            "(maxFeePerGas) and legacy (gasPrice) fee fields"
        )
    if has_1559:
        price = _uint_field(tx["maxFeePerGas"], "maxFeePerGas")
        if price > max_fee_per_gas_wei:
            raise TestnetSafetyError(
                f"maxFeePerGas {price} wei/gas exceeds the testnet cap "
                f"{max_fee_per_gas_wei} wei/gas"
            )
        prio = tx.get("maxPriorityFeePerGas")
        if prio is not None:
            prio = _uint_field(prio, "maxPriorityFeePerGas")
            if prio > price:
                raise TestnetSafetyError(
                    "malformed fee inputs: maxPriorityFeePerGas exceeds "
                    "maxFeePerGas"
                )
            if prio > max_priority_fee_wei:
                raise TestnetSafetyError(
                    f"maxPriorityFeePerGas {prio} wei/gas exceeds the "
                    f"testnet cap {max_priority_fee_wei} wei/gas"
                )
    elif has_legacy:
        price = _uint_field(tx["gasPrice"], "gasPrice")
        if price > max_fee_per_gas_wei:
            raise TestnetSafetyError(
                f"gasPrice {price} wei/gas exceeds the testnet cap "
                f"{max_fee_per_gas_wei} wei/gas"
            )
    else:
        raise TestnetSafetyError(
            "missing fee input: transaction has neither maxFeePerGas "
            "(EIP-1559) nor gasPrice (legacy) — refusing to bound or sign"
        )

    expenditure = gas * price
    if expenditure > max_total_fee_wei:
        raise TestnetSafetyError(
            f"maximum fee expenditure {expenditure} wei (gas × fee price) "
            f"exceeds the testnet cap {max_total_fee_wei} wei — a gas-unit "
            "cap is not a monetary cap"
        )
    return expenditure


# ── Transaction policy ───────────────────────────────────────────────────────

def validate_transaction(
    tx: dict,
    *,
    sender: str,
    chain_id: int,
    max_value_wei: int = MAX_TESTNET_TX_VALUE_WEI,
    max_gas_units: int = MAX_TESTNET_GAS_UNITS,
    max_fee_per_gas_wei: int = MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
    max_priority_fee_wei: int = MAX_TESTNET_MAX_PRIORITY_FEE_WEI,
    max_total_fee_wei: int = MAX_TESTNET_TX_FEE_WEI,
    known_targets: Optional[dict[int, dict[str, str]]] = None,
) -> dict:
    """Enforce the transaction policy. Returns the tx unchanged if safe.

    Rules (each violation raises TestnetSafetyError):
      * no deployments (`to` must be a real recipient);
      * recipient must be the sender itself or a recorded known target
        for this chain (no arbitrary EOA transfers, no unverified targets);
      * value within the hard cap;
      * gas within the hard cap;
      * fee exposure within the monetary cap (gasLimit × fee price), with
        missing/negative/malformed/over-cap fee inputs rejected;
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

    gas = _uint_field(tx.get("gas"), "gas")
    if gas <= 0 or gas > max_gas_units:
        raise TestnetSafetyError(
            f"gas limit {gas} outside the permitted testnet range "
            f"(1, {max_gas_units}]"
        )

    validate_fee_exposure(
        tx,
        max_fee_per_gas_wei=max_fee_per_gas_wei,
        max_priority_fee_wei=max_priority_fee_wei,
        max_total_fee_wei=max_total_fee_wei,
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


def validate_bundle(
    unsigned_txs: Sequence[dict],
    *,
    sender: str,
    chain_id: int,
    max_value_wei: int = MAX_TESTNET_TX_VALUE_WEI,
    max_gas_units: int = MAX_TESTNET_GAS_UNITS,
    max_fee_per_gas_wei: int = MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
    max_priority_fee_wei: int = MAX_TESTNET_MAX_PRIORITY_FEE_WEI,
    max_total_fee_wei: int = MAX_TESTNET_TX_FEE_WEI,
    known_targets: Optional[dict[int, dict[str, str]]] = None,
) -> list[dict]:
    """Apply the same transaction policy to every transaction of a
    Flashbots bundle. One rejected transaction rejects the whole bundle."""
    validated: list[dict] = []
    for i, tx in enumerate(unsigned_txs):
        try:
            validated.append(
                validate_transaction(
                    dict(tx),
                    sender=sender,
                    chain_id=chain_id,
                    max_value_wei=max_value_wei,
                    max_gas_units=max_gas_units,
                    max_fee_per_gas_wei=max_fee_per_gas_wei,
                    max_priority_fee_wei=max_priority_fee_wei,
                    max_total_fee_wei=max_total_fee_wei,
                    known_targets=known_targets,
                )
            )
        except TestnetSafetyError as exc:
            raise TestnetSafetyError(f"bundle tx[{i}] rejected: {exc}") from exc
    return validated


# ── Audit log ────────────────────────────────────────────────────────────────

TX_LOG_FIELDS = (
    "timestamp", "purpose", "chain_id", "target", "function",
    "estimated_gas", "actual_gas_used", "tx_hash", "outcome", "error",
    "max_fee_exposure_wei",
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


# ── Simulation gate (DSS-WO-ENG-004 §4) ──────────────────────────────────────

def evaluate_bundle_simulation(
    simulation: object,
    *,
    expected_tx_count: Optional[int] = None,
) -> dict:
    """Check a Flashbots eth_callBundle simulation result for failure.

    Raises TestnetSafetyError unless the response proves that every
    transaction of the bundle would succeed:
      * a missing or unrecognised response fails closed;
      * a top-level `error` fails;
      * `firstRevert` fails;
      * empty per-transaction results fail;
      * any per-transaction `error` fails;
      * any transaction that consumed no gas (nothing executed) fails.
    """
    if not isinstance(simulation, dict) or not simulation:
        raise TestnetSafetyError(
            "relay returned no usable simulation response — failing closed"
        )
    if simulation.get("error"):
        raise TestnetSafetyError(
            f"bundle simulation failed: {simulation['error']}"
        )
    result = simulation.get("result")
    result = result if isinstance(result, dict) else simulation
    if result.get("firstRevert"):
        raise TestnetSafetyError(
            f"bundle simulation reports firstRevert: {result['firstRevert']}"
        )
    results = result.get("results")
    if not results or not isinstance(results, list):
        raise TestnetSafetyError(
            "bundle simulation returned no per-transaction results — "
            "failing closed"
        )
    if expected_tx_count is not None and len(results) != expected_tx_count:
        raise TestnetSafetyError(
            f"bundle simulation returned {len(results)} result entries for "
            f"{expected_tx_count} transactions — malformed response, "
            "failing closed"
        )
    for i, entry in enumerate(results):
        if not isinstance(entry, dict):
            raise TestnetSafetyError(
                f"simulation result entry {i} is malformed — failing closed"
            )
        if entry.get("error"):
            raise TestnetSafetyError(
                f"simulation: bundle tx[{i}] would fail: {entry['error']}"
            )
        gas_used = entry.get("gasUsed", 0)
        try:
            gas_used = int(gas_used, 16) if isinstance(gas_used, str) else int(gas_used)
        except (TypeError, ValueError) as exc:
            raise TestnetSafetyError(
                f"simulation: bundle tx[{i}] reported a malformed gasUsed — "
                "failing closed"
            ) from exc
        if gas_used <= 0:
            raise TestnetSafetyError(
                f"simulation: bundle tx[{i}] consumed no gas (nothing "
                "executed) — failing closed"
            )
    return simulation


def _payload_fingerprint(signed_txs: Sequence[bytes], target_block: int) -> str:
    """Order-sensitive fingerprint binding the exact signed payload to the
    exact target block. A submission is only authorised when a successful
    simulation exists for the identical fingerprint."""
    h = hashlib.sha256()
    h.update(int(target_block).to_bytes(8, "big"))
    for raw in signed_txs:
        b = bytes(raw)
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)
    return h.hexdigest()


# ── Flashbots transport session ──────────────────────────────────────────────

class FlashbotsBundleSession:
    """Production simulate/submit orchestration for the Flashbots transport.

    The relay is a TRANSPORT only. Every authorization decision is made by
    the gate: the session's payload was already policy-validated and signed
    through prepare_flashbots_bundle (wallet identity bound, chain verified,
    transaction policy enforced) before it can reach this class.

    ENFORCEMENT (in production code, never only in tests): submit() refuses
    to call the relay unless THIS session previously recorded a SUCCESSFUL
    simulation of the EXACT signed payload for the EXACT target block, the
    target block is still in the future, the execution gate flag is set,
    and the RPC chain ID still matches the expected allowlisted chain —
    re-verified immediately before the submission call.
    """

    # Not a pytest test class despite the Test prefix.
    __test__ = False

    def __init__(
        self,
        relay_w3: object,
        *,
        sender: str,
        config: TestnetExecutionConfig,
        chain_id: int,
        known_targets: Optional[dict[int, dict[str, str]]] = None,
    ):
        self._w3 = relay_w3
        self._sender = str(sender)
        self._config = config
        self._chain_id = int(chain_id)
        self._known_targets = (
            KNOWN_TARGETS if known_targets is None else known_targets
        )
        # fingerprint -> successful simulation summary
        self._authorized: dict[str, dict] = {}

    def verify_chain(self) -> int:
        """Verify the transport RPC's reported chain ID against the
        allowlist and the configured expectation."""
        reported = int(self._w3.eth.chain_id)
        return check_chain_id(
            reported, self._config.expected_chain_id, self._config.effective_allowlist()
        )

    def simulate(
        self,
        signed_txs: Sequence[bytes],
        target_block: int,
        *,
        purpose: str = "flashbots bundle simulation",
    ) -> dict:
        """Simulate the signed bundle via eth_callBundle (read-only relay
        operation; no execution gate required, every other check applies).

        A successful return stores the authorization that submit() will
        later require. Simulation and submission are distinct operations.
        """
        cfg = self._config
        if not cfg.rpc_url:
            raise TestnetSafetyError(
                "relay RPC URL is required for bundle simulation"
            )
        self.verify_chain()
        payload = [bytes(r) for r in signed_txs]
        if not payload:
            raise TestnetSafetyError("empty bundle refused")
        head = int(self._w3.eth.block_number)
        if int(target_block) <= head:
            raise TestnetSafetyError(
                f"target block {target_block} is not in the future "
                f"(head {head}) — refusing to simulate"
            )
        simulation = self._w3.flashbots.simulate(
            [{"signed_transaction": raw} for raw in payload],
            int(target_block),
        )
        evaluate_bundle_simulation(simulation, expected_tx_count=len(payload))
        total_gas = None
        try:
            total_gas = int(simulation.get("totalGasUsed"))
        except (TypeError, ValueError, AttributeError):
            total_gas = None
        self._authorized[_payload_fingerprint(payload, int(target_block))] = {
            "target_block": int(target_block),
            "chain_id": self._chain_id,
            "total_gas_used": total_gas,
            "timestamp": time.time(),
            "purpose": purpose,
        }
        record_testnet_tx(
            cfg.tx_log_path,
            purpose=purpose,
            chain_id=self._chain_id,
            target=self._sender.lower(),
            function="flashbots_callBundle",
            estimated_gas=total_gas,
            actual_gas_used=None,
            tx_hash=simulation.get("bundleHash"),
            outcome="simulated",
            error=None,
        )
        return simulation

    def submit(
        self,
        signed_txs: Sequence[bytes],
        target_block: int,
        *,
        purpose: str = "flashbots bundle submission",
    ) -> dict:
        """Submit the signed bundle via eth_sendBundle.

        Fail-closed sequence BEFORE any relay submission call:
          1. the testnet execution gate flag must be set;
          2. the RPC chain ID is re-verified (allowlist + expectation);
          3. the target block must still be in the future — a simulation
             for a reached block is stale and cannot authorize anything;
          4. a successful simulation of the EXACT signed payload for the
             EXACT target block must exist in this session.
        The authorization is consumed by the submission.
        """
        cfg = self._config
        if not cfg.enabled:
            raise TestnetSafetyError(
                "bundle submission requires the testnet execution gate "
                "(set DSS_TESTNET_EXEC=1); it is off by default"
            )
        if not cfg.rpc_url:
            raise TestnetSafetyError("relay RPC URL is required for submission")
        payload = [bytes(r) for r in signed_txs]
        if not payload:
            raise TestnetSafetyError("empty bundle refused")

        self.verify_chain()  # 2: immediately before submission
        head = int(self._w3.eth.block_number)
        if head >= int(target_block):  # 3: staleness
            raise TestnetSafetyError(
                f"stale submission refused: target block {target_block} "
                f"already reached (head {head}) — the simulation result is "
                "no longer valid"
            )
        fingerprint = _payload_fingerprint(payload, int(target_block))
        auth = self._authorized.get(fingerprint)
        if auth is None:  # 4: mandatory successful simulation
            raise TestnetSafetyError(
                "submission refused: no successful simulation recorded for "
                "this exact signed payload and target block — simulate() "
                "must succeed first, and the submitted payload and target "
                "block must match it exactly"
            )

        result = self._w3.flashbots.send_bundle(
            [{"signed_transaction": raw} for raw in payload],
            target_block_number=int(target_block),
        )

        bundle_hash = None
        try:
            bundle_hash = simulation_hash(result)
        except Exception:
            bundle_hash = None
        record = record_testnet_tx(
            cfg.tx_log_path,
            purpose=purpose,
            chain_id=self._chain_id,
            target=self._sender.lower(),
            function="flashbots_send_bundle",
            estimated_gas=auth.get("total_gas_used"),
            actual_gas_used=None,
            tx_hash=bundle_hash,
            outcome="submitted_to_relay",
            error=None,
        )
        del self._authorized[fingerprint]  # one simulation authorizes once
        return {"relay_result": result, "audit": record}


def simulation_hash(result: object) -> Optional[str]:
    """Best-effort public bundle-hash extraction from a relay result.
    Never touches credentials; unknown shapes return None."""
    if result is None:
        return None
    for candidate in (
        getattr(result, "bundle_hash", None),
        getattr(result, "bundleHash", None),
    ):
        if callable(candidate):
            try:
                return str(candidate())
            except Exception:
                continue
        if candidate:
            return str(candidate)
    if isinstance(result, dict):
        if result.get("bundleHash"):
            return str(result["bundleHash"])
    return None


# ── Shared transaction defaulting ────────────────────────────────────────────

def _fill_tx_defaults(w3: object, txs: list[dict], sender: str) -> None:
    """Fill nonce/gas/fee defaults from the verified RPC BEFORE policy
    validation, so validation always sees the values that will be signed.

    Fees default to an EIP-1559 envelope built from the latest base fee
    (×1.5 headroom + 1 gwei priority). Every filled value must then pass
    validate_fee_exposure — if the RPC suggests an over-cap price the
    transaction is refused, never silently clipped.
    """
    nonce = None
    latest = None
    for tx in txs:
        if tx.get("nonce") is None:
            if nonce is None:
                nonce = w3.eth.get_transaction_count(sender)
            else:
                nonce += 1
            tx["nonce"] = nonce
        if tx.get("gas") is None:
            tx["gas"] = 21000
        if tx.get("maxFeePerGas") is None and tx.get("gasPrice") is None:
            if latest is None:
                latest = w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
            priority = w3.to_wei(1, "gwei")
            tx["maxFeePerGas"] = int(base_fee) * 3 // 2 + priority
            tx["maxPriorityFeePerGas"] = priority
            tx["type"] = 2


def _raw_signed_bytes(signed) -> bytes:
    """eth-account <0.13 exposes .rawTransaction; newer versions expose
    .raw_transaction. Always returns raw bytes."""
    raw = getattr(signed, "raw_transaction", None) or getattr(
        signed, "rawTransaction", None
    )
    return bytes(raw)


# ── Signing / submission orchestration (direct RPC transport) ────────────────

def sign_and_submit_testnet_tx(
    tx: dict,
    *,
    purpose: str,
    config: Optional[TestnetExecutionConfig] = None,
    web3_factory: Optional[Callable[[str], object]] = None,
    known_targets: Optional[dict[int, dict[str, str]]] = None,
    receipt_timeout: int = 120,
) -> dict:
    """The ONLY sanctioned path from intention to a live direct-RPC testnet
    transaction.

    Sequence (every step fails closed):
      1. gate config must be enabled and fully provisioned (including the
         secret-store custody checks);
      2. connect to the configured RPC and verify its REPORTED chain ID
         against the allowlist and the configured expectation (pre-sign);
      3. fill nonce/gas/fee defaults from the RPC, then validate the FINAL
         transaction against the policy (including fee exposure);
      4. decrypt the agent wallet through the single custody path and bind
         it to the recorded agent address;
      5. re-verify the chain ID, sign;
      6. re-verify the RPC chain ID (pre-submission);
      7. submit, wait for the receipt, and record the outcome.

    Returns the audit record. Never returns or logs key material.
    """
    cfg = config or TestnetExecutionConfig.from_env()
    cfg.require_ready()  # 1 (enabled + provisioned + secret store verified)

    if web3_factory is None:
        def web3_factory(url: str):
            from web3 import Web3, HTTPProvider
            return Web3(HTTPProvider(url))

    w3 = web3_factory(cfg.rpc_url)  # 2
    if not getattr(w3, "is_connected", lambda: True)():
        raise TestnetSafetyError("testnet RPC is not reachable")
    reported = int(w3.eth.chain_id)
    check_chain_id(reported, cfg.expected_chain_id, cfg.effective_allowlist())

    chain_id = reported
    tx = dict(tx)
    tx["chainId"] = chain_id
    sender = cfg.load_agent_address()
    _fill_tx_defaults(w3, [tx], sender)  # 3a
    validate_transaction(
        tx, sender=sender, chain_id=chain_id,
        max_value_wei=cfg.max_value_wei, max_gas_units=cfg.max_gas_units,
        max_fee_per_gas_wei=cfg.max_fee_per_gas_wei,
        max_priority_fee_wei=cfg.max_priority_fee_wei,
        max_total_fee_wei=cfg.max_total_fee_wei,
        known_targets=known_targets,
    )  # 3b
    fee_exposure = validate_fee_exposure(
        tx,
        max_fee_per_gas_wei=cfg.max_fee_per_gas_wei,
        max_priority_fee_wei=cfg.max_priority_fee_wei,
        max_total_fee_wei=cfg.max_total_fee_wei,
    )

    # 4. Decrypt the agent wallet; bind it to the recorded identity.
    account = load_agent_wallet(cfg)

    check_chain_id(  # 5a: still the expected chain immediately before signing
        int(w3.eth.chain_id), cfg.expected_chain_id, cfg.effective_allowlist()
    )
    signed = account.sign_transaction(tx)  # 5b
    raw = _raw_signed_bytes(signed)

    check_chain_id(  # 6: pre-submission
        int(w3.eth.chain_id), cfg.expected_chain_id, cfg.effective_allowlist()
    )

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
        estimated_gas=tx["gas"],
        actual_gas_used=actual_gas_used,
        tx_hash=tx_hash_hex,
        outcome=outcome,
        error=error,
        max_fee_exposure_wei=fee_exposure,
    )
    return record


# ── Signing / submission orchestration (Flashbots transport) ─────────────────

def prepare_flashbots_bundle(
    unsigned_txs: Sequence[dict],
    *,
    purpose: str,
    config: Optional[TestnetExecutionConfig] = None,
    web3_factory: Optional[Callable[[str], object]] = None,
    relay_factory: Optional[Callable[[str, object, str], object]] = None,
    known_targets: Optional[dict[int, dict[str, str]]] = None,
) -> tuple["FlashbotsBundleSession", list[bytes], int]:
    """The ONLY sanctioned preparation path for Flashbots bundle operations.

    Gate → verify chain → policy-validate the FINAL transactions → bind the
    wallet identity → sign. NO relay call happens here; simulation and
    submission are separate, explicit operations on the returned session.

    Sequence (every step fails closed):
      1. config provisioned (RPC URL, allowlisted expected chain, secret
         store custody checks) — the execution flag is NOT required yet:
         simulation is read-only; submission enforces it in submit();
      2. connect to the configured RPC and verify its REPORTED chain ID;
      3. fill nonce/gas/fee defaults, then validate every transaction of
         the bundle against the policy (including fee exposure);
      4. decrypt the agent wallet through the single custody path and bind
         it to the recorded agent address;
      5. re-verify the chain ID, sign every transaction;
      6. build the relay transport (Flashbots middleware attached by the
         factory; the agent wallet is the relay auth signer) and return
         (session, signed_raw_txs, target_block).

    The Flashbots relay never becomes an independent authorization path:
    the session only transports payloads that already passed every gate
    above.
    """
    cfg = config or TestnetExecutionConfig.from_env()
    cfg.require_provisioned()  # 1

    if web3_factory is None:
        def web3_factory(url: str):
            from web3 import Web3, HTTPProvider
            return Web3(HTTPProvider(url))

    w3 = web3_factory(cfg.rpc_url)  # 2
    if not getattr(w3, "is_connected", lambda: True)():
        raise TestnetSafetyError("testnet RPC is not reachable")
    reported = int(w3.eth.chain_id)
    check_chain_id(reported, cfg.expected_chain_id, cfg.effective_allowlist())

    txs = [dict(t) for t in unsigned_txs]
    if not txs:
        raise TestnetSafetyError("empty bundle refused")
    sender = cfg.load_agent_address()
    for tx in txs:
        tx["chainId"] = reported
    _fill_tx_defaults(w3, txs, sender)  # 3a
    validate_bundle(
        txs, sender=sender, chain_id=reported,
        max_value_wei=cfg.max_value_wei, max_gas_units=cfg.max_gas_units,
        max_fee_per_gas_wei=cfg.max_fee_per_gas_wei,
        max_priority_fee_wei=cfg.max_priority_fee_wei,
        max_total_fee_wei=cfg.max_total_fee_wei,
        known_targets=known_targets,
    )  # 3b

    account = load_agent_wallet(cfg)  # 4

    check_chain_id(  # 5a: still the expected chain immediately before signing
        int(w3.eth.chain_id), cfg.expected_chain_id, cfg.effective_allowlist()
    )
    signed = [_raw_signed_bytes(account.sign_transaction(tx)) for tx in txs]  # 5b

    if relay_factory is None:
        def relay_factory(url: str, auth_account, relay_url: str):
            from web3 import Web3, HTTPProvider
            from flashbots import flashbot
            relay_w3 = Web3(HTTPProvider(url))
            flashbot(relay_w3, auth_account, relay_url)
            return relay_w3

    relay_w3 = relay_factory(cfg.rpc_url, account, cfg.relay_url)  # 6
    session = FlashbotsBundleSession(
        relay_w3,
        sender=sender,
        config=cfg,
        chain_id=reported,
        known_targets=known_targets,
    )
    target_block = int(w3.eth.block_number) + 1
    return session, signed, target_block
