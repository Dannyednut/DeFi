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
    in this module will decrypt the wallet, sign or submit anything —
    including Flashbots bundle preparation, which signs and therefore
    passes the SAME gate as direct-RPC submission (DSS-WO-ENG-005 §2).
    Bundle submission additionally requires a SUCCESSFUL simulation of
    the exact prepared payload (see FlashbotsBundleSession — enforced
    in production code, not in tests).
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
  * Fee exposure is capped in monetary terms, not just gas units, PER
    TRANSACTION and PER BUNDLE: gasLimit × maxFeePerGas (EIP-1559) or
    gasLimit × gasPrice (legacy) must be within MAX_TESTNET_TX_FEE_WEI
    for every transaction AND for the aggregate across a whole Flashbots
    bundle, checked before wallet decryption; fee-price inputs that are
    missing, negative, malformed or above MAX_TESTNET_MAX_FEE_PER_GAS_WEI
    are rejected (DSS-WO-ENG-005 §5).
  * Flashbots submission authority is a capability-gated boundary: only
    prepare_flashbots_bundle can mint a PreparedFlashbotsBundle, and the
    session re-verifies EVERY binding — wallet identity (recovered
    signers), chain ID, validated intent (field-by-field against the
    DECODED signed transaction), exact signed payload, target block — at
    construction and before every relay interaction (DSS-WO-ENG-005 §3;
    signed-intent field comparison per DSS-WO-ENG-006 §3).
  * Signed raw transaction bytes NEVER leave the gate-controlled
    implementation (DSS-WO-ENG-006 §2/§4): prepare_flashbots_bundle
    returns ONLY the controlled session; the capability has no
    signed-payload property; simulate()/submit() accept no payload and
    return relay responses scrubbed of raw-transaction material; the
    audit log never contains the payload.
  * Prepared-bundle state is FROZEN after creation (DSS-WO-ENG-006 §3):
    the capability wraps an immutable state record behind a single slot
    whose assignment is refused; the transaction intents are frozen
    dataclass records, never mutable dicts; the minting token is a
    name-mangled class-private object, not a module-level public name.
    HONEST BOUNDARY: this is an application-level authorization boundary
    only. Arbitrary hostile code executing inside the same interpreter
    can defeat Python-level protections (object.__setattr__, mangled-name
    lookup, monkeypatching) — no cryptographic unforgeability against
    same-process code is claimed. Stronger isolation, if ever required,
    is a future architecture requirement (docs/testnet-policy.md §5),
    not something invented here.
  * No arbitrary EOA transfers: the only permitted recipient without an
    entry in KNOWN_TARGETS is the agent wallet itself (self-transfer).
  * No unlimited token approvals: approve/increaseAllowance calldata is
    decoded and bounded amounts are enforced.
  * No unverified contract targets and no contract deployments: `to`
    must be a real recipient (self or a recorded known target);
    deployments (empty `to`) are rejected outright.
  * Every simulation and every submission — successful, failed, refused
    or of uncertain outcome — is recorded (purpose, chain ID, target,
    function, estimated/actual gas, hash, outcome, fee exposure) to a
    JSONL log that never contains credentials, raw signed transaction
    bytes or any other secret material (DSS-WO-ENG-005 §4). Refusals
    during preflight — including infrastructure/RPC failures — are
    audited as well: once an attempt has been initiated it cannot
    silently escape the audit path (DSS-WO-ENG-006 §6). Relay responses
    returned to callers are scrubbed of raw-transaction-shaped keys and
    long hex blobs before they leave the session (DSS-WO-ENG-006 §4).
  * Caller-visible EXCEPTIONS never carry secret material (DSS-WO-ENG-007
    Finding A): transport and evaluation failures can embed the exact
    signed payload hex (relays and HTTP libraries quote request bodies),
    so simulate()/submit() and the direct-RPC submission path never
    re-raise such an exception and never attach one as a visible cause or
    context. Callers receive a TestnetSafetyError carrying the failure
    classification (refused / failed / rejected / uncertain), the original
    exception CLASS NAME and a sanitized diagnostic; relay-provided error
    text is sanitized before it is incorporated into any caller-visible
    exception; gate-raised TestnetSafetyErrors keep their exact type and
    message with cause and context detached.
  * Access lists are OUT OF POLICY (DSS-WO-ENG-007 Finding B, Option A —
    fail closed): any supplied non-empty accessList is rejected by the
    transaction policy BEFORE signing, the signed-transaction decoder
    independently refuses a non-empty access list in the signed EIP-1559
    envelope, and no access-list field is bound into the intent records.
    An absent, None or empty accessList is accepted (eth_account signs an
    empty list either way). EIP-2930 envelopes remain refused outright.

This module holds NO key material. The agent wallet is decrypted from
the DSS secret store (encrypted keystore + passphrase file) only inside
load_agent_wallet, which every signing path must call.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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

# Maximum monetary fee exposure (DSS-WO-ENG-004 §5; aggregate per
# DSS-WO-ENG-005 §5). A gas-unit cap is NOT a monetary cap: these bounds
# fail closed on the actual fee inputs.
#
#   * MAX_TESTNET_MAX_FEE_PER_GAS_WEI — sanity ceiling on the fee price
#     itself (maxFeePerGas / gasPrice). Sepolia base fees are typically
#     0.01–30 gwei; 50 gwei covers even severe congestion.
#   * MAX_TESTNET_MAX_PRIORITY_FEE_WEI — priority-fee ceiling (2 gwei is
#     far above what Sepolia inclusion requires).
#   * MAX_TESTNET_TX_FEE_WEI — hard ceiling on the worst-case expenditure
#     of ONE transaction (gasLimit × maxFeePerGas for EIP-1559, gasLimit ×
#     gasPrice for legacy) AND on the AGGREGATE fee exposure of a whole
#     Flashbots bundle (sum over all its transactions), checked before
#     wallet decryption.
#
# APPROVED for the initial operation (DSS-WO-ENG-005 §5–6): the initial
# state-changing test is ONE 0-value self-transfer of 21,000 gas — worst
# case 21,000 × 50 gwei = 0.00105 native. The 0.002 native ceiling is a
# ONE-TRANSACTION MAXIMUM for that initial operation. It does NOT absorb
# a second full worst-case retry: two such transactions would total
# 2 × 0.00105 = 0.0021 native, which EXCEEDS the ceiling. Any retry
# therefore requires a NEW authorization and must not be assumed to fit
# the same budget. Raising these caps is a reviewed code change, never an
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


def validate_bundle_fee_exposure(
    txs: Sequence[dict],
    *,
    max_fee_per_gas_wei: int = MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
    max_priority_fee_wei: int = MAX_TESTNET_MAX_PRIORITY_FEE_WEI,
    max_total_fee_wei: int = MAX_TESTNET_TX_FEE_WEI,
) -> int:
    """Aggregate BUNDLE-level fee-exposure check (DSS-WO-ENG-005 §5).

    Per-transaction caps alone are not enough: a bundle of two
    transactions, each individually within its own cap, can still carry
    a combined worst-case expenditure above the approved budget. The sum
    of every transaction's worst-case expenditure (gasLimit × fee price)
    must stay within max_total_fee_wei. Values are never clipped and
    every transaction's fee inputs are re-validated strictly (missing,
    negative, malformed, contradictory or over-cap inputs are rejected).
    One rejected transaction rejects the whole bundle.

    Returns the aggregate worst-case expenditure in wei.
    """
    total = 0
    for i, tx in enumerate(txs):
        try:
            total += validate_fee_exposure(
                tx,
                max_fee_per_gas_wei=max_fee_per_gas_wei,
                max_priority_fee_wei=max_priority_fee_wei,
                max_total_fee_wei=max_total_fee_wei,
            )
        except TestnetSafetyError as exc:
            raise TestnetSafetyError(f"bundle tx[{i}] rejected: {exc}") from exc
    if total > max_total_fee_wei:
        raise TestnetSafetyError(
            f"aggregate bundle fee exposure {total} wei exceeds the "
            f"testnet cap {max_total_fee_wei} wei — a bundle of "
            "individually compliant transactions may not exceed the "
            "approved budget as a whole"
        )
    return total


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
      * a supplied (non-empty) EIP-1559 access list is REJECTED — access
        lists are out of policy at this execution boundary (DSS-WO-ENG-007
        Finding B, Option A — fail closed); an absent, None or empty
        accessList is accepted, and a malformed (non-list) accessList is
        rejected;
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

    # DSS-WO-ENG-007 Finding B (Option A — fail closed): access lists are
    # OUT OF POLICY at this execution boundary. The gate signs only plain
    # legacy / EIP-1559 value-and-calldata intents, so any supplied
    # non-empty accessList is rejected BEFORE signing; the signed-
    # transaction decoder independently refuses any non-empty access list
    # in the signed EIP-1559 envelope. An absent, None or EMPTY accessList
    # is accepted — eth_account signs an empty list either way, so the
    # only accepted form is exactly one: none.
    access_list = tx.get("accessList")
    if access_list is not None and not isinstance(access_list, (list, tuple)):
        raise TestnetSafetyError(
            "malformed accessList: expected a list — refusing "
            "(DSS-WO-ENG-007 Finding B)"
        )
    if access_list:
        raise TestnetSafetyError(
            "access lists are not supported at this execution boundary: "
            "the transaction policy rejects any supplied accessList "
            "(fail closed, DSS-WO-ENG-007 Finding B)"
        )

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
    Flashbots bundle. One rejected transaction rejects the whole bundle.

    The AGGREGATE fee exposure across all bundle transactions is
    additionally capped at max_total_fee_wei (DSS-WO-ENG-005 §5) — an
    over-limit bundle is rejected as a whole, before any wallet is
    decrypted or anything signed.
    """
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
    validate_bundle_fee_exposure(
        validated,
        max_fee_per_gas_wei=max_fee_per_gas_wei,
        max_priority_fee_wei=max_priority_fee_wei,
        max_total_fee_wei=max_total_fee_wei,
    )
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


# Raw signed transactions serialize to ≥ ~200 hex characters, while
# keystore/bundle/transaction hashes are exactly 64. Any hex blob of 100+
# characters inside an error string is therefore treated as potential
# secret material and redacted before it can reach the audit log.
_LONG_HEX_RE = re.compile(r"0x[0-9a-fA-F]{100,}")


def _sanitize_error(message: object, *forbidden: str) -> str:
    """Make an error message safe for the audit log (DSS-WO-ENG-005 §4).

    Replaces every occurrence of each explicitly forbidden string (the
    exact signed payload hex, with and without the 0x prefix) with a
    redaction marker, then redacts any remaining long hex blob that could
    be raw signed transaction bytes or other key material. Truncated to
    300 characters so a hostile error cannot bloat the log.
    """
    text = str(message)
    for secret in forbidden:
        if secret:
            text = text.replace(secret, "<redacted>")
    return _LONG_HEX_RE.sub("0x<redacted-hex>", text)[:300]


# Response keys that could carry raw signed transaction bytes out of the
# session. Any value stored under such a key is redacted before a relay
# response reaches a caller (DSS-WO-ENG-006 §2/§4).
_RESULT_RAW_TX_KEYS = frozenset({
    "signed_transaction", "signedtransaction", "signed_tx", "signedtx",
    "raw_transaction", "rawtransaction", "raw_tx", "rawtx", "raw",
})

_SCRUB_MAX_DEPTH = 24


def _scrub_transport_payload(obj: object, depth: int = 0):
    """Scrub a relay response before it leaves the session
    (DSS-WO-ENG-006 §2/§4): values under raw-transaction-shaped keys are
    replaced with a redaction marker, byte strings of broadcastable size
    (a signed transaction is ≥ ~100 bytes; 32-byte hashes pass) are
    redacted, and any long hex blob that could be raw signed bytes is
    redacted from strings. Legitimate relay metadata — hashes, gas
    figures, statuses — survives untouched."""
    if depth > _SCRUB_MAX_DEPTH:
        return "<redacted:too-deep>"
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in _RESULT_RAW_TX_KEYS:
                out[key] = "<redacted>"
            else:
                out[key] = _scrub_transport_payload(value, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        items = [_scrub_transport_payload(v, depth + 1) for v in obj]
        return tuple(items) if isinstance(obj, tuple) else items
    if isinstance(obj, (bytes, bytearray)):
        return "<redacted-bytes>" if len(obj) >= 100 else bytes(obj)
    if isinstance(obj, str):
        return _LONG_HEX_RE.sub("0x<redacted-hex>", obj)
    return obj


def _clean_raise_candidate(
    exc: BaseException, classification: str, *forbidden: str
) -> TestnetSafetyError:
    """Build the exception a caller may SEE after a failed operation
    (DSS-WO-ENG-007 Finding A).

    A transport or evaluation exception can embed the exact signed payload
    hex (relays and HTTP libraries routinely quote the request body in
    error messages), so the original exception object is NEVER re-raised
    to the caller and never attached as a visible cause or context:

      * a gate-constructed TestnetSafetyError (whose text is sanitized by
        construction) is re-raised as the SAME object — same type, same
        message, exact failure classification preserved — with any cause
        or context detached;
      * anything else is replaced by a fresh TestnetSafetyError carrying
        only the failure classification, the original exception CLASS
        NAME and the sanitized diagnostic (forbidden strings and long hex
        redacted, truncated).

    The returned exception must be raised OUTSIDE the failing except
    block so Python's implicit exception chaining cannot attach the
    unsanitized original as __context__ either. __cause__ and __context__
    are cleared and __suppress_context__ is set, so no formatted
    traceback and no attribute inspection can reach the original
    exception.
    """
    err: TestnetSafetyError
    if isinstance(exc, TestnetSafetyError) and not _LONG_HEX_RE.search(
        str(exc)
    ):
        # Gate-constructed refusal: text is sanitized by construction;
        # re-raising the same object preserves the exact type and message.
        err = exc
    else:
        detail = _sanitize_error(exc, *forbidden)
        err = TestnetSafetyError(
            f"{classification}: {type(exc).__name__}: {detail}"
            if detail
            else f"{classification}: {type(exc).__name__}"
        )
    # Detach EVERY reference to the original failure. These must be set
    # before the raise, and the raise itself must happen outside any
    # except block (implicit chaining would otherwise re-attach the
    # unsanitized original as __context__).
    err.__cause__ = None
    err.__context__ = None
    err.__suppress_context__ = True
    return err


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

    Relay-provided error text (top-level error, firstRevert,
    per-transaction error) is SANITIZED before it is embedded in the
    raised exception (DSS-WO-ENG-007 Finding A): a relay response can
    quote the submitted signed transactions, so the refusal must not
    propagate that text verbatim to the caller.
    """
    if not isinstance(simulation, dict) or not simulation:
        raise TestnetSafetyError(
            "relay returned no usable simulation response — failing closed"
        )
    if simulation.get("error"):
        raise TestnetSafetyError(
            f"bundle simulation failed: "
            f"{_sanitize_error(simulation['error'])}"
        )
    result = simulation.get("result")
    result = result if isinstance(result, dict) else simulation
    if result.get("firstRevert"):
        raise TestnetSafetyError(
            f"bundle simulation reports firstRevert: "
            f"{_sanitize_error(result['firstRevert'])}"
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
                f"simulation: bundle tx[{i}] would fail: "
                f"{_sanitize_error(entry['error'])}"
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


# ── Flashbots capability, intent records and signed-payload verification ─────
#    (DSS-WO-ENG-005 §3; DSS-WO-ENG-006 §2–§4)
#
# Containment (DSS-WO-ENG-006 §2): the signed raw transaction bytes exist
# ONLY inside this implementation. prepare_flashbots_bundle returns the
# controlled session ONLY; the capability exposes no signed-payload
# accessor; simulate()/submit() accept no payload and return scrubbed
# relay responses; nothing here writes signed bytes to the audit log.
#
# HONEST THREAT MODEL (DSS-WO-ENG-006 §3): this is an application-level
# authorization boundary, NOT independent hardware or process isolation.
# Python private attributes, name mangling and frozen dataclasses deter
# accidental and ordinary misuse; arbitrary hostile code executing inside
# the same interpreter can still defeat them (object.__setattr__,
# mangled-name lookup, monkeypatching). No cryptographic unforgeability
# against same-process hostile code is claimed. A stronger isolation
# model, if ever required, is a future architecture requirement — see
# docs/testnet-policy.md §5 — not something invented in this work order.


@dataclass(frozen=True)
class ValidatedTransactionIntent:
    """Immutable, normalized record of ONE policy-validated transaction
    intent (DSS-WO-ENG-006 §3).

    Built ONLY from an already-validated transaction dict at capability
    minting time; the mutable input dict is consumed and never retained.
    Every field needed to re-verify the SIGNED transaction against the
    validated intent is captured here (recipient, value, gas, calldata,
    nonce, chain ID and the exact fee envelope). Access lists are
    deliberately NOT captured (DSS-WO-ENG-007 Finding B, Option A — fail
    closed): they are out of policy, rejected before signing, and refused
    in the signed envelope by the signed-transaction decoder, so an empty
    decoded access list is the only accepted form. Instances are frozen
    dataclasses — attribute assignment raises — so the capability never
    exposes a mutable dictionary through its properties.
    """

    to: str                       # recipient, lowercase hex string
    value: int
    gas: int
    data: bytes
    nonce: int
    chain_id: int
    fee_kind: str                 # "eip1559" | "legacy"
    max_fee_per_gas: Optional[int]
    max_priority_fee_per_gas: Optional[int]
    gas_price: Optional[int]


def _intent_from_validated_tx(tx: dict, chain_id: int) -> ValidatedTransactionIntent:
    """Normalize ONE policy-validated transaction dict into an immutable
    intent record. Strict: missing, negative, malformed or contradictory
    fields fail closed. The input must be the FINAL validated form —
    nonce/gas/fee defaults are filled and the policy applied BEFORE this
    runs, never after."""
    # DSS-WO-ENG-007 Finding B (defense in depth): validate_transaction
    # already rejects a supplied access list before signing; the intent
    # minting refuses one too, so an access list can never enter the
    # capability even if validation were bypassed.
    if tx.get("accessList"):
        raise TestnetSafetyError(
            "validated intent carries an access list — access lists are "
            "out of policy at this execution boundary "
            "(DSS-WO-ENG-007 Finding B)"
        )
    to = tx.get("to")
    if not to or not str(to).strip():
        raise TestnetSafetyError("validated intent is missing a recipient")
    to = str(to).lower()
    tx_chain = tx.get("chainId")
    if tx_chain is not None and int(tx_chain) != int(chain_id):
        raise TestnetSafetyError(
            f"validated intent chain mismatch: transaction says {tx_chain}, "
            f"capability binds {chain_id}"
        )
    value = _uint_field(tx.get("value", 0), "value")
    gas = _uint_field(tx.get("gas"), "gas")
    if gas <= 0:
        raise TestnetSafetyError("validated intent has no positive gas limit")
    nonce = _uint_field(tx.get("nonce"), "nonce")
    data = tx.get("data") or b""
    if isinstance(data, str):
        try:
            data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        except ValueError as exc:
            raise TestnetSafetyError(
                "validated intent calldata is not valid hex"
            ) from exc
    if not isinstance(data, (bytes, bytearray)):
        raise TestnetSafetyError("validated intent calldata must be bytes")
    data = bytes(data)

    has_1559 = tx.get("maxFeePerGas") is not None
    has_legacy = tx.get("gasPrice") is not None
    if has_1559 and has_legacy:
        raise TestnetSafetyError(
            "malformed fee inputs: intent carries both EIP-1559 "
            "(maxFeePerGas) and legacy (gasPrice) fee fields"
        )
    if has_1559:
        max_fee = _uint_field(tx["maxFeePerGas"], "maxFeePerGas")
        prio = tx.get("maxPriorityFeePerGas")
        prio = _uint_field(prio, "maxPriorityFeePerGas") if prio is not None else None
        return ValidatedTransactionIntent(
            to=to, value=value, gas=gas, data=data, nonce=nonce,
            chain_id=int(chain_id), fee_kind="eip1559",
            max_fee_per_gas=max_fee, max_priority_fee_per_gas=prio,
            gas_price=None,
        )
    if has_legacy:
        return ValidatedTransactionIntent(
            to=to, value=value, gas=gas, data=data, nonce=nonce,
            chain_id=int(chain_id), fee_kind="legacy",
            max_fee_per_gas=None, max_priority_fee_per_gas=None,
            gas_price=_uint_field(tx["gasPrice"], "gasPrice"),
        )
    raise TestnetSafetyError(
        "missing fee input: validated intent has neither maxFeePerGas "
        "(EIP-1559) nor gasPrice (legacy) — refusing to bind"
    )


def _rlp_decode_item(buf: bytes):
    """Decode ONE RLP item from `buf`; return (item, consumed) where item
    is bytes (an RLP string) or a list of decoded items. Minimal strict
    decoder covering the full RLP grammar — exactly enough to decode the
    transactions this gate signs; short reads and trailing bytes fail
    closed. Implemented here (instead of relying on library internals) so
    the signed-intent comparison stays self-contained and version-stable."""
    if not buf:
        raise TestnetSafetyError("malformed signed transaction: empty RLP input")
    b0 = buf[0]
    if b0 <= 0x7F:  # single byte encodes itself
        return buf[0:1], 1
    if b0 <= 0xB7:  # short string
        n = b0 - 0x80
        if len(buf) < 1 + n:
            raise TestnetSafetyError(
                "malformed signed transaction: truncated RLP string"
            )
        return buf[1:1 + n], 1 + n
    if b0 <= 0xBF:  # long string
        ll = b0 - 0xB7
        if len(buf) < 1 + ll:
            raise TestnetSafetyError(
                "malformed signed transaction: truncated RLP length"
            )
        n = int.from_bytes(buf[1:1 + ll], "big")
        if len(buf) < 1 + ll + n:
            raise TestnetSafetyError(
                "malformed signed transaction: truncated RLP string"
            )
        return buf[1 + ll:1 + ll + n], 1 + ll + n
    if b0 <= 0xF7:  # short list
        n = b0 - 0xC0
        header = 1
    else:            # long list
        ll = b0 - 0xF7
        if len(buf) < 1 + ll:
            raise TestnetSafetyError(
                "malformed signed transaction: truncated RLP length"
            )
        n = int.from_bytes(buf[1:1 + ll], "big")
        header = 1 + ll
    if len(buf) < header + n:
        raise TestnetSafetyError(
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
        raise TestnetSafetyError(
            "malformed signed transaction: trailing RLP data"
        )
    return items, header + n


def _int_from_rlp(item: object, field: str) -> int:
    if isinstance(item, list):
        raise TestnetSafetyError(
            f"malformed signed transaction: {field} is not a scalar"
        )
    return int.from_bytes(bytes(item), "big")


def _decode_signed_tx(raw: bytes) -> dict:
    """Decode a SIGNED transaction into its consensus fields
    (DSS-WO-ENG-006 §3). The gate produces ONLY two envelopes — legacy
    EIP-155 and EIP-1559 typed — and decoding anything else fails closed.
    This is the decoding half of the signed-intent verification: the
    session compares these fields against the validated intent of the
    capability and any mismatch refuses simulation and submission. The
    EIP-1559 access list (field 8) is checked too: a non-empty access
    list is refused — access lists are out of policy at this boundary
    (DSS-WO-ENG-007 Finding B, Option A). Never includes the raw bytes
    in error messages."""
    raw = bytes(raw)
    if not raw:
        raise TestnetSafetyError("malformed signed transaction: empty payload")
    if raw[0] == 0x02:
        # EIP-1559: 0x02 || rlp([chainId, nonce, maxPriorityFeePerGas,
        # maxFeePerGas, gas, to, value, data, accessList, yParity, r, s])
        body, used = _rlp_decode_item(raw[1:])
        if used != len(raw) - 1:
            raise TestnetSafetyError(
                "malformed signed transaction: trailing bytes after "
                "EIP-1559 body"
            )
        if not isinstance(body, list) or len(body) != 12:
            raise TestnetSafetyError(
                "malformed signed transaction: unexpected EIP-1559 field count"
            )
        to = body[5]
        if not isinstance(to, (bytes, bytearray)) or (to and len(to) != 20):
            raise TestnetSafetyError(
                "malformed signed transaction: recipient is not an address"
            )
        access_list = body[8]
        if not isinstance(access_list, list):
            raise TestnetSafetyError(
                "malformed signed transaction: EIP-1559 access list is "
                "not a list"
            )
        if access_list:
            # DSS-WO-ENG-007 Finding B (Option A — fail closed): the gate
            # never signs an access list, so a signed envelope that
            # carries one is by definition not the validated intent —
            # refuse it before any simulation or relay interaction.
            raise TestnetSafetyError(
                "signed transaction carries a non-empty EIP-1559 access "
                "list; access lists are not supported at this execution "
                "boundary (DSS-WO-ENG-007 Finding B) — refusing"
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
        raise TestnetSafetyError(
            "unsupported signed transaction envelope (EIP-2930): the gate "
            "signs only legacy or EIP-1559 — refusing"
        )
    if raw[0] < 0xC0:
        raise TestnetSafetyError(
            f"unsupported signed transaction envelope type 0x{raw[0]:02x}: "
            "refusing"
        )
    # Legacy: rlp([nonce, gasPrice, gas, to, value, data, v, r, s])
    body, used = _rlp_decode_item(raw)
    if used != len(raw):
        raise TestnetSafetyError(
            "malformed signed transaction: trailing bytes after legacy body"
        )
    if not isinstance(body, list) or len(body) != 9:
        raise TestnetSafetyError(
            "malformed signed transaction: unexpected legacy field count"
        )
    to = body[3]
    if not isinstance(to, (bytes, bytearray)) or (to and len(to) != 20):
        raise TestnetSafetyError(
            "malformed signed transaction: recipient is not an address"
        )
    v = _int_from_rlp(body[6], "v")
    if v >= 35:  # EIP-155 replay protection
        chain_id: Optional[int] = (v - 35) // 2
    else:
        # pre-EIP-155 signature — no chain binding; the intent comparison
        # refuses (this gate always signs with a chain ID)
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


@dataclass(frozen=True)
class _PreparedBundleState:
    """The FROZEN content of one prepared Flashbots bundle
    (DSS-WO-ENG-006 §3). Created once by PreparedFlashbotsBundle.__init__
    under the minting authority and never mutated afterwards: the
    dataclass is frozen, the capability blocks attribute assignment, and
    every field is itself immutable (str/int/bytes, tuples, frozen intent
    records, the frozen TestnetExecutionConfig). The signed payload lives
    ONLY here — there is no public accessor for it."""

    sender: str
    chain_id: int
    intents: tuple                       # tuple[ValidatedTransactionIntent, ...]
    signed_payload: tuple                # tuple[bytes, ...] — gate-internal only
    target_block: int
    fingerprint: str
    total_fee_exposure_wei: int
    purpose: str
    config: "TestnetExecutionConfig"


class PreparedFlashbotsBundle:
    """The authorization capability for ONE prepared Flashbots bundle.

    Minted exclusively by prepare_flashbots_bundle (via _mint) AFTER the
    execution gate, chain verification, full transaction policy (including
    the aggregate bundle fee cap), wallet-custody binding and signing have
    all succeeded. It is IMMUTABLE after creation (DSS-WO-ENG-006 §3): the
    content is a frozen `_PreparedBundleState` held behind a single
    `__slots__` slot, attribute assignment and deletion on the capability
    are refused, the transaction intents are frozen
    ValidatedTransactionIntent records (never mutable dicts), and the
    minting token is a name-mangled class-private object — not a
    module-level public name. There is deliberately NO signed-payload
    property (DSS-WO-ENG-006 §2): the exact signed bytes stay inside the
    gate-controlled implementation.

    FlashbotsBundleSession re-verifies every binding at construction and
    again before every relay interaction — wallet identity (signer
    recovery), chain ID, exact-payload/target fingerprint, the
    field-by-field correspondence between each DECODED signed transaction
    and its validated intent, and the fee exposure — and refuses on any
    mismatch.

    HONEST BOUNDARY (DSS-WO-ENG-006 §3): this is an application-level
    authorization boundary. Same-process hostile code can defeat it
    (object.__setattr__, the mangled token name, monkeypatching); no
    cryptographic unforgeability is claimed. See the module docstring and
    docs/testnet-policy.md §5.
    """

    # Not a pytest test class despite the Test prefix.
    __test__ = False

    __slots__ = ("_state",)

    # Minting authority: a name-mangled CLASS-PRIVATE token
    # (DSS-WO-ENG-006 §3) — not a module-level public name. Its only
    # sanctioned use is the _mint classmethod, whose only sanctioned
    # caller is prepare_flashbots_bundle in this module. Honest boundary:
    # same-process code could still reach this attribute via its mangled
    # name; the session's independent re-verification (exact type, signer
    # recovery, fingerprint, signed-intent fields, fee exposure) is the
    # defense in depth that actually refuses forged content.
    __MINT_AUTHORITY = object()

    def __init__(
        self,
        *,
        _authority: object,
        sender: str,
        chain_id: int,
        validated_txs: Sequence[dict],
        signed_payload: Sequence[bytes],
        target_block: int,
        fingerprint: str,
        total_fee_exposure_wei: int,
        purpose: str,
        config: "TestnetExecutionConfig",
    ):
        if _authority is not type(self).__MINT_AUTHORITY:
            raise TestnetSafetyError(
                "PreparedFlashbotsBundle is an internal capability minted "
                "EXCLUSIVELY by prepare_flashbots_bundle(); external "
                "construction is refused"
            )
        validated = tuple(dict(t) for t in validated_txs)
        payload = tuple(bytes(r) for r in signed_payload)
        if len(validated) != len(payload):
            raise TestnetSafetyError(
                "capability minting refused: transaction count does not "
                "match signed payload count"
            )
        if int(target_block) <= 0 or not str(fingerprint):
            raise TestnetSafetyError(
                "capability minting refused: malformed target block or "
                "fingerprint"
            )
        # Normalize the validated transactions into IMMUTABLE intent
        # records (DSS-WO-ENG-006 §3): the mutable dicts are consumed here
        # and never stored on the capability.
        intents = tuple(
            _intent_from_validated_tx(t, int(chain_id)) for t in validated
        )
        object.__setattr__(self, "_state", _PreparedBundleState(
            sender=str(sender).lower(),
            chain_id=int(chain_id),
            intents=intents,
            signed_payload=payload,
            target_block=int(target_block),
            fingerprint=str(fingerprint),
            total_fee_exposure_wei=int(total_fee_exposure_wei),
            purpose=str(purpose),
            config=config,
        ))

    def __setattr__(self, name: str, value: object) -> None:
        raise TestnetSafetyError(
            "PreparedFlashbotsBundle is immutable after creation "
            "(DSS-WO-ENG-006 §3): attribute assignment is refused"
        )

    def __delattr__(self, name: str) -> None:
        raise TestnetSafetyError(
            "PreparedFlashbotsBundle is immutable after creation "
            "(DSS-WO-ENG-006 §3): attribute deletion is refused"
        )

    @classmethod
    def _mint(cls, **fields) -> "PreparedFlashbotsBundle":
        """The ONLY minting path — the minting token is a class-private,
        name-mangled object held by this class; prepare_flashbots_bundle
        (same module) is its only sanctioned caller."""
        return cls(_authority=cls.__MINT_AUTHORITY, **fields)

    # Read-only, non-sensitive views. There is deliberately NO
    # signed_payload or raw-payload property and NO validated-tx-dict
    # property (DSS-WO-ENG-006 §2/§3): the exact signed bytes stay inside
    # the gate-controlled implementation and the intents are exposed as
    # frozen records only.

    @property
    def sender(self) -> str:
        return self._state.sender

    @property
    def chain_id(self) -> int:
        return self._state.chain_id

    @property
    def intents(self) -> tuple:
        """The immutable normalized transaction-intent records."""
        return self._state.intents

    @property
    def target_block(self) -> int:
        return self._state.target_block

    @property
    def fingerprint(self) -> str:
        return self._state.fingerprint

    @property
    def total_fee_exposure_wei(self) -> int:
        return self._state.total_fee_exposure_wei

    @property
    def purpose(self) -> str:
        return self._state.purpose

    @property
    def config(self) -> "TestnetExecutionConfig":
        return self._state.config


def _recover_signer(raw: bytes) -> str:
    """Recover the signer address from a raw signed transaction."""
    from eth_account import Account

    return Account.recover_transaction(bytes(raw))


class FlashbotsBundleSession:
    """Production simulate/submit orchestration for the Flashbots transport.

    The relay is a TRANSPORT only. A session can ONLY be created from a
    PreparedFlashbotsBundle minted by prepare_flashbots_bundle (the
    sanctioned preparation path): the constructor refuses every other
    object — forged, hand-rolled, duck-typed or subclassed (exact-type
    check) — so session construction can never bypass transaction policy
    or wallet-identity validation (DSS-WO-ENG-005 §3).

    The PUBLIC SURFACE is minimal (DSS-WO-ENG-006 §4): simulate() and
    submit() accept NO arguments, operate exclusively on the privately
    held prepared payload, and return SCRUBBED relay responses — no raw
    signed transaction bytes can leave through them, through a result
    object, or through any other public method or property. An arbitrary
    bundle can never gain submission authority merely by being passed
    around, and callers cannot substitute payloads or target blocks.

    ENFORCEMENT (in production code, never only in tests): every binding —
    wallet identity (recovered signers), chain ID, validated intent
    (DECODED signed transaction compared field-by-field against the
    frozen intent records), exact signed payload fingerprint, target
    block, and the per-transaction and aggregate fee exposure — is
    re-verified at construction and again before every relay interaction
    (DSS-WO-ENG-006 §3/§5). submit() refuses to call the relay unless
    THIS session previously recorded a SUCCESSFUL simulation of the exact
    prepared payload for the exact target block, the target block is
    still in the future, the execution gate flag is set, and the RPC
    chain ID still matches the expected allowlisted chain — re-verified
    immediately before the submission call. The one-use authorization is
    consumed BEFORE the relay call so a relay timeout cannot silently
    authorize unlimited retries.
    """

    # Not a pytest test class despite the Test prefix.
    __test__ = False

    _SIMULATION_OUTCOMES = ("simulated", "simulation_failed", "simulation_refused")

    def __init__(self, relay_w3: object, prepared: PreparedFlashbotsBundle):
        if type(prepared) is not PreparedFlashbotsBundle:
            raise TestnetSafetyError(
                "FlashbotsBundleSession must be created from a "
                "PreparedFlashbotsBundle minted by prepare_flashbots_bundle() "
                "— directly constructed, forged or subclassed authorization "
                "objects are refused"
            )
        self._w3 = relay_w3
        self._prepared = prepared
        self._config = prepared.config
        self._sender = prepared.sender
        self._chain_id = int(prepared.chain_id)
        # fingerprint -> successful simulation summary (one-use)
        self._authorized: dict[str, dict] = {}
        # Runtime binding verification — enforced behavior, not trust.
        self._verify_bindings()

    # ── binding verification ────────────────────────────────────────────────

    def _payload_hexes(self) -> tuple:
        """Exact signed payload hex (with and without 0x) for error
        redaction — these strings must never reach the audit log."""
        hexes = []
        for raw in self._prepared._state.signed_payload:
            h = bytes(raw).hex()
            hexes.extend((h, "0x" + h))
        return tuple(hexes)

    def _verify_payload_signers(self) -> None:
        """Wallet-identity binding: every signed transaction in the
        capability must have been signed by the verified agent wallet.
        The refusal is raised OUTSIDE the recovery except block so no
        library exception (which may embed the signed bytes it was
        parsing) can surface as this refusal's cause or context
        (DSS-WO-ENG-007 Finding A)."""
        for i, raw in enumerate(self._prepared._state.signed_payload):
            refusal: Optional[TestnetSafetyError] = None
            signer: Optional[str] = None
            try:
                signer = _recover_signer(raw)
            except Exception:
                # The library exception may embed the signed bytes it was
                # parsing — it is DROPPED, never chained or re-raised.
                signer = None
                refusal = TestnetSafetyError(
                    f"prepared bundle tx[{i}] is not a recoverable signed "
                    "transaction — refusing"
                )
            if refusal is not None:
                raise refusal  # outside the except block: clean chaining
            if signer.lower() != self._prepared.sender:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] was signed by "
                    f"{signer.lower()}, "
                    "not the verified agent wallet — refusing"
                )

    def _verify_signed_intents(self) -> None:
        """Signed-intent correspondence (DSS-WO-ENG-006 §3): decode every
        signed transaction and compare its consensus fields against the
        validated intent record it was prepared from — recovered sender
        is checked by _verify_payload_signers; this comparison covers
        chain ID, recipient, nonce, value, calldata, gas limit and the
        exact EIP-1559/legacy fee envelope. ANY mismatch fails closed
        before simulation or submission."""
        st = self._prepared._state
        for i, (raw, intent) in enumerate(zip(st.signed_payload, st.intents)):
            try:
                decoded = _decode_signed_tx(raw)
            except TestnetSafetyError as exc:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] failed signed-intent "
                    f"verification: {exc}"
                ) from exc
            if decoded["kind"] != intent.fee_kind:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] fee envelope mismatch: signed "
                    f"transaction is {decoded['kind']}, validated intent is "
                    f"{intent.fee_kind} — refusing"
                )
            if decoded["chain_id"] != intent.chain_id:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] chain mismatch: signed "
                    f"transaction targets chain {decoded['chain_id']}, "
                    f"validated intent binds {intent.chain_id} — refusing"
                )
            recipient = decoded["to"]
            if recipient is None or ("0x" + recipient.hex()) != intent.to:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] recipient mismatch: signed "
                    f"transaction does not match the validated intent — "
                    "refusing"
                )
            if decoded["nonce"] != intent.nonce:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] nonce mismatch: signed "
                    f"transaction does not match the validated intent — "
                    "refusing"
                )
            if decoded["value"] != intent.value:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] value mismatch: signed "
                    "transaction does not match the validated intent — "
                    "refusing"
                )
            if decoded["data"] != intent.data:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] calldata mismatch: signed "
                    "transaction does not match the validated intent — "
                    "refusing"
                )
            if decoded["gas"] != intent.gas:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] gas limit mismatch: signed "
                    "transaction does not match the validated intent — "
                    "refusing"
                )
            if intent.fee_kind == "eip1559":
                if (
                    decoded["max_fee_per_gas"] != intent.max_fee_per_gas
                    or decoded["max_priority_fee_per_gas"]
                    != intent.max_priority_fee_per_gas
                ):
                    raise TestnetSafetyError(
                        f"prepared bundle tx[{i}] fee fields mismatch: signed "
                        "transaction does not match the validated intent — "
                        "refusing"
                    )
            else:
                if decoded["gas_price"] != intent.gas_price:
                    raise TestnetSafetyError(
                        f"prepared bundle tx[{i}] gas price mismatch: signed "
                        "transaction does not match the validated intent — "
                        "refusing"
                    )

    def _recheck_fee_exposure(self) -> None:
        """Fee-exposure recheck (DSS-WO-ENG-006 §5): before EVERY relay
        interaction, re-derive the per-transaction and AGGREGATE
        worst-case fee exposure from the frozen signed intents and hold
        them against the configured caps AND the recorded total. Fail
        closed — never clip, never re-price."""
        st = self._prepared._state
        cfg = self._config
        total = 0
        for i, intent in enumerate(st.intents):
            price = (
                intent.max_fee_per_gas
                if intent.fee_kind == "eip1559"
                else intent.gas_price
            )
            if price is None:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] carries no fee price — refusing"
                )
            if price > cfg.max_fee_per_gas_wei:
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] fee price exceeds the testnet "
                    "cap — refusing before relay interaction"
                )
            if (
                intent.fee_kind == "eip1559"
                and intent.max_priority_fee_per_gas is not None
                and intent.max_priority_fee_per_gas > cfg.max_priority_fee_wei
            ):
                raise TestnetSafetyError(
                    f"prepared bundle tx[{i}] priority fee exceeds the "
                    "testnet cap — refusing before relay interaction"
                )
            total += intent.gas * price
        if total > cfg.max_total_fee_wei:
            raise TestnetSafetyError(
                f"aggregate bundle fee exposure {total} wei exceeds the "
                f"testnet cap {cfg.max_total_fee_wei} wei — refusing before "
                "relay interaction"
            )
        if total != st.total_fee_exposure_wei:
            raise TestnetSafetyError(
                "recorded fee exposure does not match the frozen signed "
                "intents — refusing"
            )

    def _verify_bindings(self) -> None:
        """Re-verify EVERY capability binding before any relay interaction:
        chain identity (allowlist + expectation + capability match),
        wallet identity (signer recovery), the exact-payload/target
        fingerprint, the field-by-field signed-intent correspondence and
        the fee-exposure recheck (DSS-WO-ENG-006 §3/§5)."""
        p = self._prepared
        reported = self.verify_chain()  # allowlist + expected chain (live RPC)
        if reported != p.chain_id:
            raise TestnetSafetyError(
                f"chain binding mismatch: the bundle was prepared for chain "
                f"{p.chain_id} but the transport now reports {reported}"
            )
        self._verify_payload_signers()
        if _payload_fingerprint(
            p._state.signed_payload, p.target_block
        ) != p.fingerprint:
            raise TestnetSafetyError(
                "payload binding mismatch: the prepared signed payload does "
                "not match its recorded fingerprint — refusing"
            )
        self._verify_signed_intents()
        self._recheck_fee_exposure()

    def verify_chain(self) -> int:
        """Verify the transport RPC's reported chain ID against the
        allowlist and the configured expectation."""
        reported = int(self._w3.eth.chain_id)
        return check_chain_id(
            reported, self._config.expected_chain_id, self._config.effective_allowlist()
        )

    # ── audit ───────────────────────────────────────────────────────────────

    def _record(self, outcome: str, *, error=None, tx_hash=None, estimated_gas=None) -> dict:
        """Append one audit record for this bundle; error text must already
        be sanitized by the caller. The bundle hash is relay-supplied, so
        it is also held against the long-hex rule (DSS-WO-ENG-007 Finding
        A): a hostile relay cannot use a hash field to smuggle the signed
        payload into the audit log. Legitimate 32-byte hashes pass."""
        if isinstance(tx_hash, str) and _LONG_HEX_RE.search(tx_hash):
            tx_hash = "0x<redacted-hex>"
        return record_testnet_tx(
            self._config.tx_log_path,
            purpose=self._prepared.purpose,
            chain_id=self._prepared.chain_id,
            target=self._prepared.sender,
            function=(
                "flashbots_callBundle"
                if outcome in self._SIMULATION_OUTCOMES
                else "flashbots_send_bundle"
            ),
            estimated_gas=estimated_gas,
            actual_gas_used=None,
            tx_hash=tx_hash,
            outcome=outcome,
            error=error,
            max_fee_exposure_wei=self._prepared.total_fee_exposure_wei,
        )

    def simulate(self) -> dict:
        """Simulate the PREPARED bundle via eth_callBundle (read-only relay
        operation on the exact prepared payload and target block — there
        is no parameter to substitute another payload).

        A successful return stores the one-use authorization that submit()
        later requires. Simulation and submission are distinct operations.
        Every outcome — success, failure or refusal — is audited; error
        text is sanitized and never contains the signed payload.

        CALLER-SAFE FAILURES (DSS-WO-ENG-007 Finding A): a transport or
        evaluation exception can embed the exact signed payload hex, so
        the original exception is never re-raised and never attached as a
        visible cause or context. Callers receive a TestnetSafetyError
        carrying the classification ("bundle simulation refused" for
        preflight refusals, "bundle simulation failed" for relay/evaluation
        failures), the original exception class name and a sanitized
        diagnostic; gate-raised TestnetSafetyErrors keep their exact type
        and message. Sanitized failures are raised OUTSIDE the failing
        except block so implicit chaining cannot leak the original.
        """
        p = self._prepared
        st = p._state
        pending: Optional[TestnetSafetyError] = None
        try:
            if not self._config.rpc_url:
                raise TestnetSafetyError(
                    "relay RPC URL is required for bundle simulation"
                )
            self._verify_bindings()  # chain + wallet + payload + intents + fees
            head = int(self._w3.eth.block_number)
            if p.target_block <= head:
                raise TestnetSafetyError(
                    f"stale simulation refused: target block {p.target_block} "
                    f"already reached (head {head}) — refusing to simulate"
                )
        except Exception as exc:
            # Every refusal is audited — including infrastructure/RPC
            # failures raised during preflight: once an attempt has been
            # initiated it must not silently escape the audit path
            # (DSS-WO-ENG-006 §6). The caller-visible exception is a
            # SANITIZED replacement (DSS-WO-ENG-007 Finding A): the
            # original transport exception can embed the signed payload,
            # so it is never re-raised and never attached as a visible
            # cause or context.
            self._record(
                "simulation_refused",
                error=_sanitize_error(exc, *self._payload_hexes()),
            )
            pending = _clean_raise_candidate(
                exc, "bundle simulation refused", *self._payload_hexes()
            )
        if pending is not None:
            # Raised OUTSIDE the except block: no implicit chaining.
            raise pending
        try:
            simulation = self._w3.flashbots.simulate(
                [{"signed_transaction": raw} for raw in st.signed_payload],
                p.target_block,
            )
            evaluate_bundle_simulation(
                simulation, expected_tx_count=len(st.signed_payload)
            )
        except Exception as exc:
            # Failed simulations are audited too: top-level relay errors,
            # reverts, firstRevert, malformed responses, missing results
            # and result-count mismatches (DSS-WO-ENG-005 §4). Same
            # caller-safe exception replacement (DSS-WO-ENG-007 Finding A).
            self._record(
                "simulation_failed",
                error=_sanitize_error(exc, *self._payload_hexes()),
            )
            pending = _clean_raise_candidate(
                exc, "bundle simulation failed", *self._payload_hexes()
            )
        if pending is not None:
            raise pending
        total_gas = None
        try:
            total_gas = int(simulation.get("totalGasUsed"))
        except (TypeError, ValueError, AttributeError):
            total_gas = None
        self._authorized[p.fingerprint] = {
            "target_block": p.target_block,
            "chain_id": self._chain_id,
            "total_gas_used": total_gas,
            "timestamp": time.time(),
        }
        self._record(
            "simulated",
            tx_hash=simulation.get("bundleHash"),
            estimated_gas=total_gas,
        )
        # The relay response is scrubbed before it leaves the session: no
        # signed bytes (or anything shaped like them) can leak through a
        # result object (DSS-WO-ENG-006 §2/§4).
        return _scrub_transport_payload(simulation)

    def submit(self) -> dict:
        """Submit the PREPARED bundle via eth_sendBundle.

        Fail-closed sequence BEFORE any relay submission call:
          1. the testnet execution gate flag must be set;
          2. EVERY capability binding is re-verified — including the RPC
             chain identity, immediately before submission;
          3. the target block must still be in the future — a simulation
             for a reached block is stale and cannot authorize anything;
          4. a successful simulation of the EXACT prepared payload for the
             EXACT target block must exist in this session.

        The one-use authorization is consumed BEFORE the relay call, so a
        relay timeout or any other submission failure cannot silently
        authorize unlimited retries (DSS-WO-ENG-005 §4): a retry requires
        a NEW successful simulation. Every attempt — accepted, rejected,
        uncertain or refused — is audited; error text is sanitized and
        never contains the signed payload or any other secret material.

        CALLER-SAFE FAILURES (DSS-WO-ENG-007 Finding A): transport
        exceptions from eth_sendBundle can embed the exact signed payload
        hex, so the original exception is never re-raised and never
        attached as a visible cause or context. Callers receive a
        TestnetSafetyError carrying the classification ("bundle submission
        refused" / "bundle submission rejected" / "bundle submission
        uncertain"), the original exception class name and a sanitized
        diagnostic; relay-provided rejection text is sanitized before it
        is incorporated into the raised exception; gate-raised
        TestnetSafetyErrors keep their exact type and message. Sanitized
        failures are raised OUTSIDE the failing except block so implicit
        chaining cannot leak the original.
        """
        p = self._prepared
        st = p._state
        auth = None
        pending: Optional[TestnetSafetyError] = None
        try:
            if not self._config.enabled:
                raise TestnetSafetyError(
                    "bundle submission requires the testnet execution gate "
                    "(set DSS_TESTNET_EXEC=1); it is off by default"
                )
            if not self._config.rpc_url:
                raise TestnetSafetyError(
                    "relay RPC URL is required for submission"
                )
            self._verify_bindings()  # 2: incl. chain, immediately pre-submission
            head = int(self._w3.eth.block_number)
            if head >= p.target_block:  # 3: staleness
                raise TestnetSafetyError(
                    f"stale submission refused: target block {p.target_block} "
                    f"already reached (head {head}) — the simulation result is "
                    "no longer valid"
                )
            auth = self._authorized.pop(p.fingerprint, None)  # consumed FIRST
            if auth is None:  # 4: mandatory successful simulation
                raise TestnetSafetyError(
                    "submission refused: no successful simulation recorded "
                    "for this exact prepared payload and target block — "
                    "simulate() must succeed first, and the submitted "
                    "payload and target block must match it exactly"
                )
        except Exception as exc:
            # Every refusal is audited — including infrastructure/RPC
            # failures raised during preflight: once an attempt has been
            # initiated it must not silently escape the audit path
            # (DSS-WO-ENG-006 §6). The one-use authorization is NOT
            # consumed here: the relay submission attempt never started.
            # The caller-visible exception is a SANITIZED replacement
            # (DSS-WO-ENG-007 Finding A).
            self._record(
                "submission_refused",
                error=_sanitize_error(exc, *self._payload_hexes()),
            )
            pending = _clean_raise_candidate(
                exc, "bundle submission refused", *self._payload_hexes()
            )
        if pending is not None:
            # Raised OUTSIDE the except block: no implicit chaining.
            raise pending

        submission_failure: Optional[TestnetSafetyError] = None
        try:
            result = self._w3.flashbots.send_bundle(
                [{"signed_transaction": raw} for raw in st.signed_payload],
                target_block_number=p.target_block,
            )
        except Exception as exc:
            # Submission attempt failed: classify explicitly. A JSON-RPC
            # error response is a deterministic REJECTION; anything else
            # (timeout, transport failure) leaves the outcome UNCERTAIN —
            # the bundle may still have landed. Either way the consumed
            # authorization is NOT restored: retries need a new
            # successful simulation. The original exception — which can
            # embed the exact signed payload — is audited sanitized and
            # then REPLACED by a caller-safe exception (DSS-WO-ENG-007
            # Finding A); it is never re-raised or chained.
            outcome = "rejected" if isinstance(exc, ValueError) else "uncertain"
            self._record(
                outcome,
                error=_sanitize_error(exc, *self._payload_hexes()),
                estimated_gas=(auth or {}).get("total_gas_used"),
            )
            submission_failure = _clean_raise_candidate(
                exc, f"bundle submission {outcome}", *self._payload_hexes()
            )
        if submission_failure is not None:
            # Raised OUTSIDE the except block: no implicit chaining.
            raise submission_failure

        bundle_hash = simulation_hash(result)
        relay_error = _relay_error(result)
        if relay_error is not None:
            # The relay explicitly rejected the bundle (error in a 200
            # response body instead of a raised error). The relay-provided
            # error text is SANITIZED before it reaches the caller
            # (DSS-WO-ENG-007 Finding A, item 3): a relay error can quote
            # the submitted signed transactions. This raise is not inside
            # any except block, so no implicit chaining occurs.
            self._record(
                "rejected",
                error=_sanitize_error(
                    f"relay error: {relay_error}", *self._payload_hexes()
                ),
                tx_hash=bundle_hash,
                estimated_gas=auth.get("total_gas_used"),
            )
            raise TestnetSafetyError(
                "relay rejected the bundle submission: "
                f"{_sanitize_error(relay_error, *self._payload_hexes())}"
            )
        if bundle_hash is None:
            self._record(
                "uncertain",
                error=(
                    "relay returned no bundle hash — submission outcome "
                    "unknown, failing closed"
                ),
                estimated_gas=auth.get("total_gas_used"),
            )
            raise TestnetSafetyError(
                "relay submission outcome is uncertain: no bundle hash in "
                "the relay response — treating the submission as failed"
            )
        record = self._record(
            "accepted",
            tx_hash=bundle_hash,
            estimated_gas=auth.get("total_gas_used"),
        )
        # Scrubbed result object: no signed bytes leave the session
        # (DSS-WO-ENG-006 §2/§4).
        return {
            "relay_result": _scrub_transport_payload(result),
            "audit": record,
        }


def _relay_error(result: object) -> Optional[str]:
    """Explicit error field on a relay result — some relays return errors
    inside a 200 response body instead of raising. Unknown shapes return
    None; this function never touches credentials."""
    if isinstance(result, dict):
        err = result.get("error")
        return str(err) if err else None
    err = getattr(result, "error", None)
    return str(err) if err else None


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

    selector = "transfer"
    data = tx.get("data") or b""
    if isinstance(data, str):
        data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    if len(data) >= 4:
        selector = "0x" + data[:4].hex()
    elif int(tx.get("value", 0)) > 0:
        selector = "eth_transfer"

    # The exact signed payload must never appear in the audit log, even if
    # a transport error embeds it (DSS-WO-ENG-005 §4), and never in a
    # caller-visible exception (DSS-WO-ENG-007 Finding A).
    raw_hexes = (raw.hex(), "0x" + raw.hex())
    submission_failure: Optional[TestnetSafetyError] = None
    try:
        tx_hash = w3.eth.send_raw_transaction(raw)  # 7
    except Exception as exc:
        # The submission attempt failed and is audited (DSS-WO-ENG-005 §4):
        # a JSON-RPC error response is a deterministic REJECTION by the
        # node; anything else (timeout, transport failure) leaves the
        # outcome UNCERTAIN — the transaction may still have been received.
        # The original exception can embed the exact signed payload hex,
        # so the caller receives a SANITIZED replacement raised OUTSIDE
        # this except block — no bare re-raise, no cause/context chaining
        # (DSS-WO-ENG-007 Finding A).
        outcome = "rejected" if isinstance(exc, ValueError) else "uncertain"
        record_testnet_tx(
            cfg.tx_log_path,
            purpose=purpose,
            chain_id=chain_id,
            target=str(tx.get("to", "")).lower(),
            function=selector,
            estimated_gas=tx["gas"],
            actual_gas_used=None,
            tx_hash=None,
            outcome=outcome,
            error=_sanitize_error(exc, *raw_hexes),
            max_fee_exposure_wei=fee_exposure,
        )
        submission_failure = _clean_raise_candidate(
            exc, f"testnet submission {outcome}", *raw_hexes
        )
    if submission_failure is not None:
        raise submission_failure
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
        error = _sanitize_error(exc, *raw_hexes)

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
) -> "FlashbotsBundleSession":
    """The ONLY sanctioned preparation path for Flashbots bundle operations.

    Gate → verify chain → policy-validate the FINAL transactions (including
    the aggregate bundle fee cap) → bind the wallet identity → sign → mint
    the authorization capability → build the session. NO relay call happens
    here; simulation and submission are separate, explicit operations on
    the returned session.

    CONTAINMENT (DSS-WO-ENG-006 §2): returns the CONTROLLED SESSION ONLY.
    The signed raw transaction bytes are NEVER returned, exposed through a
    property, iterator, debug helper or result object, or written to any
    log — they stay inside the gate-controlled implementation for
    simulation and relay submission. A caller can request preparation
    (this call), simulation (session.simulate()) and controlled,
    authorization-checked submission (session.submit()); a caller CANNOT
    obtain the raw signed payload from the preparation API.

    Sequence (every step fails closed):
      1. the testnet execution gate must be ENABLED and the config fully
         provisioned (DSS_TESTNET_EXEC=1, RPC URL, allowlisted expected
         chain, secret-store custody checks). Preparation DECRYPTS the
         wallet and SIGNS, so it passes the SAME explicit gate as
         direct-RPC submission — it must refuse to sign when
         DSS_TESTNET_EXEC is unset or false (DSS-WO-ENG-005 §2);
      2. connect to the configured RPC and verify its REPORTED chain ID;
      3. fill nonce/gas/fee defaults, then validate every transaction of
         the bundle against the policy — including the per-transaction AND
         the AGGREGATE bundle fee-exposure caps. An over-limit bundle is
         refused BEFORE the wallet is decrypted or anything signed;
      4. decrypt the agent wallet through the single custody path and bind
         it to the recorded agent address;
      5. re-verify the chain ID, sign every transaction;
      6. build the relay transport (Flashbots middleware attached by the
         factory; the agent wallet is the relay auth signer) and MINT the
         PreparedFlashbotsBundle capability — binding wallet identity,
         chain ID, the frozen normalized transaction-intent records, the
         exact signed payload, the target block and their fingerprint —
         then build the session on it. No other code path can mint the
         capability or create a session.

    The Flashbots relay never becomes an independent authorization path:
    the session only transports the exact payload that already passed
    every gate above.
    """
    cfg = config or TestnetExecutionConfig.from_env()
    cfg.require_ready()  # 1 — SAME explicit gate as direct RPC (ENG-005 §2)

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
    validated = validate_bundle(
        txs, sender=sender, chain_id=reported,
        max_value_wei=cfg.max_value_wei, max_gas_units=cfg.max_gas_units,
        max_fee_per_gas_wei=cfg.max_fee_per_gas_wei,
        max_priority_fee_wei=cfg.max_priority_fee_wei,
        max_total_fee_wei=cfg.max_total_fee_wei,
        known_targets=known_targets,
    )  # 3b — includes the AGGREGATE bundle fee cap, before wallet decryption
    total_fee_exposure = validate_bundle_fee_exposure(
        validated,
        max_fee_per_gas_wei=cfg.max_fee_per_gas_wei,
        max_priority_fee_wei=cfg.max_priority_fee_wei,
        max_total_fee_wei=cfg.max_total_fee_wei,
    )

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

    relay_w3 = relay_factory(cfg.rpc_url, account, cfg.relay_url)  # 6a
    target_block = int(w3.eth.block_number) + 1
    prepared = PreparedFlashbotsBundle._mint(  # 6b: the ONLY minting path
        sender=sender,
        chain_id=reported,
        validated_txs=validated,
        signed_payload=signed,
        target_block=target_block,
        fingerprint=_payload_fingerprint(signed, target_block),
        total_fee_exposure_wei=total_fee_exposure,
        purpose=purpose,
        config=cfg,
    )
    # 6c — CONTAINMENT (DSS-WO-ENG-006 §2): return the CONTROLLED SESSION
    # ONLY. No tuple, no signed raw transaction bytes, no capability
    # object, no target block. The signed payload stays inside the
    # gate-controlled implementation; the session constructor independently
    # re-verifies every binding (signer recovery, chain, fingerprint,
    # signed-intent fields, fee exposure) before returning it.
    return FlashbotsBundleSession(relay_w3, prepared)
