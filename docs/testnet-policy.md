# DSS Testnet Policy (DSS-WO-ENG-003 D; unified per DSS-WO-ENG-004;
capability-gated per DSS-WO-ENG-005)

Status: ACTIVE — enforced by `executor/testnet_gate.py`.
The gate is **disabled by default** (`DSS_TESTNET_EXEC` unset/empty). Nothing
in this repository signs or submits a transaction unless every condition in
this document is met.

## 1. Non-negotiable boundaries

| Rule | Enforcement |
| --- | --- |
| `EXECUTE_ONCHAIN` remains the production guard and stays `false` | `config.py`; the gate does not read or modify it (asserted by test) |
| Ethereum mainnet (chain ID 1) is always refused | hard-coded in `check_chain_id` (not configurable) |
| Unknown chains fail closed | `ALLOWED_TESTNET_CHAINS` is the only authority |
| Production allowlist is Sepolia-only and never environment-configurable | module constant; tests may inject a controlled allowlist explicitly (see §2); `from_env` cannot set it |
| Gate disabled unless `DSS_TESTNET_EXEC=1` | `TestnetExecutionConfig.from_env`; EVERY operation that decrypts the wallet and signs — direct RPC and Flashbots preparation alike — requires it (`require_ready`); bundle submission re-checks it inside `FlashbotsBundleSession.submit` (DSS-WO-ENG-005 §2) |
| Missing RPC / keystore / passphrase / address fails closed | `require_ready` (signing paths) / `require_provisioned` (read-only chain/RPC checks) |
| ONE wallet-loading path for every signing operation | `load_agent_wallet` (see §3); the Flashbots transport has no wallet logic of its own |
| Only the sanctioned preparation path creates a submission session | `PreparedFlashbotsBundle` capability minted exclusively by `prepare_flashbots_bundle`; `FlashbotsBundleSession` refuses forged, duck-typed or subclassed objects and re-verifies every binding (wallet identity via signer recovery, chain ID, validated intent, exact signed payload, target block) at construction and before every relay interaction (DSS-WO-ENG-005 §3) |
| Chain ID verified before signing AND immediately before submission | both transports: `sign_and_submit_testnet_tx` and `prepare_flashbots_bundle` + `FlashbotsBundleSession.verify_chain` |
| No arbitrary EOA transfers | `validate_transaction`: recipient = agent wallet or reviewed target |
| No unlimited token approvals | approval selectors decoded; amount capped at `MAX_TOKEN_APPROVAL_AMOUNT` |
| No unverified contract targets | `KNOWN_TARGETS` empty by default; entries require review below |
| No contract deployments | empty `to` rejected outright |
| Value, gas AND monetary fee caps per transaction | `MAX_TESTNET_TX_VALUE_WEI` (0.001 native), `MAX_TESTNET_GAS_UNITS` (500k), plus §4 fee-exposure policy |
| Aggregate bundle fee exposure within the same cap | `validate_bundle_fee_exposure`: the SUM of worst-case expenditure across a bundle must stay within `MAX_TESTNET_TX_FEE_WEI` (0.002 native), checked BEFORE wallet decryption; values never clipped (DSS-WO-ENG-005 §5) |
| A successful simulation precedes every bundle submission | `FlashbotsBundleSession` (production code — see §5) |
| Every simulation and submission audited — success, failure, refusal or uncertain outcome | `logs/testnet_tx_log.jsonl` (gitignored); error text sanitized; credentials and raw signed transaction bytes never recorded (DSS-WO-ENG-005 §4) |
| Agent wallet is DSS-owned, testnet-only | encrypted keystore in the DSS secret store; identity bound at loading |

## 2. Allowlisted networks

| Network | Chain ID | Added | Review record |
| --- | --- | --- | --- |
| Ethereum Sepolia | 11155111 | 2026-11-04 (WO-ENG-003 D) | RPC verified live on 2026-11-04: `eth_chainId` = 11155111, block height 12,062,812 (publicnode RPC); Flashbots Sepolia relay documented by Flashbots (docs updated 2025-12-16) |

**Adding a network** (e.g. Hoodi) requires, before `ALLOWED_TESTNET_CHAINS`
is touched: a documented review of (a) current network health, (b) RPC
availability and reported chain ID, (c) required contract/protocol
deployments for the strategies involved, and (d) Coordinator sign-off.

**Test-injected allowlists:** offline tests may pass a controlled allowlist
explicitly (e.g. Sepolia + Holesky) to exercise the mismatch branch — an
allowlisted RPC chain that differs from the expected one is rejected before
wallet signing, and a chain identity change between signing and submission
prevents sending. This mechanism exists ONLY as an explicit function or
config argument; the environment can never change the production map, and
mainnet (1) is rejected even on an injected allowlist.

## 3. Wallet custody (single path)

The agent wallet is generated inside the DSS runtime from OS CSPRNG entropy;
no mnemonic exists. The raw key is encrypted at creation into a web3
secret-storage v3 keystore (scrypt) with a machine-generated 256-bit
passphrase, stored as a hex-encoded passphrase file.

`executor/testnet_gate.load_agent_wallet` is the ONLY wallet-loading path
for every signing operation (direct RPC and Flashbots alike). Before any
key material is touched it verifies, fail-closed:

- presence of keystore, passphrase and recorded-address files;
- file types: regular files only, symlinks rejected;
- restrictive permissions: keystore and passphrase must carry no
  group/world bits (0600); their directory must not be accessible to
  other users (0700);
- keystore shape (v3 secret-storage JSON);
- passphrase decoding (hex-encoded — the central keystore format; there
  is no environment override and no raw-text fallback);
- identity binding: the derived `LocalAccount` address must equal the
  recorded public address, or the account is never returned.

There is deliberately no environment variable for the keystore path, the
passphrase or any alternative wallet; `from_env` cannot redirect custody.
Tests exercise this exact function with throwaway keys in temporary
directories — never the live wallet.

**Residual risk (documented per DSS-WO-ENG-004 §7):** the passphrase file
sits in the same DSS secret-store directory as the keystore. The runtime
provides no separate secret mechanism (no OS keyring service, no HSM/KMS),
so a single directory-level compromise of the agent filesystem exposes
both factors together. File (0600) and directory (0700) permissions confine
that exposure to the agent user; no live signing happens while this
residual risk is unremediated unless the Coordinator explicitly accepts it.

## 4. Fee-exposure policy (DSS-WO-ENG-004 §5)

A gas-unit cap is NOT a monetary cap. `validate_fee_exposure` bounds the
actual monetary exposure of the transaction that will be signed:

| Bound | Value | Applies to |
| --- | --- | --- |
| `MAX_TESTNET_TX_FEE_WEI` | 2×10¹⁵ wei (0.002 native) | worst-case expenditure `gasLimit × maxFeePerGas` (EIP-1559) or `gasLimit × gasPrice` (legacy) — per transaction AND as the aggregate over a whole bundle |
| `MAX_TESTNET_MAX_FEE_PER_GAS_WEI` | 50 gwei | fee price sanity ceiling (maxFeePerGas / gasPrice) |
| `MAX_TESTNET_MAX_PRIORITY_FEE_WEI` | 2 gwei | priority fee ceiling (must also be ≤ maxFeePerGas) |

Missing, negative, malformed (non-integer) or contradictory fee inputs
(both 1559 and legacy fields present) are rejected; over-cap values are
rejected — never silently clipped. The same policy applies to every
transaction of a Flashbots bundle (`validate_bundle`), plus the
aggregate bundle-level check described above.

**Approved ceiling calculation for the initial 0-value self-transfer
(DSS-WO-ENG-005 §5–6; one transaction only):**

- planned operation: 0-value self-transfer, 21,000 gas;
- Sepolia base fee is typically 0.01–30 gwei; the fill policy uses
  `1.5 × base fee + 1 gwei priority`, so ≤ 50 gwei covers even severe
  congestion;
- planned worst-case expenditure: 21,000 gas × 50 gwei = 1.05×10¹⁵ wei
  ≈ 0.00105 native;
- approved hard ceiling: 2×10¹⁵ wei (0.002 native).

**The 0.002 native ceiling is a ONE-TRANSACTION MAXIMUM for the initial
operation.** It does NOT absorb a second full worst-case retry:
21,000 × 50 gwei = 0.00105 native per transaction, so two full
worst-case transactions would total 0.0021 native, which EXCEEDS the
ceiling. Any retry therefore requires a NEW authorization and must not
be assumed to fit the same budget. The aggregate bundle check
(`validate_bundle_fee_exposure`) enforces exactly this: a bundle of two
worst-case transactions is rejected before wallet decryption.
Raising any cap is a reviewed code change, never an environment setting.

## 5. Simulation-before-submission and the capability boundary (DSS-WO-ENG-004 §4; DSS-WO-ENG-005 §3–§4)

`prepare_flashbots_bundle` performs the gate checks (it signs, so the
full execution gate is required — same as direct RPC), chain
verification, transaction policy including the aggregate bundle fee cap,
wallet custody binding and signing — it never calls the relay. It then
mints a `PreparedFlashbotsBundle` capability (the ONLY minting path;
external construction is refused at runtime) binding the verified wallet
identity, chain ID, validated transaction intent, EXACT signed payload,
EXACT target block and their order-sensitive fingerprint, and returns a
`FlashbotsBundleSession` built on that capability. The session
constructor refuses every other object (exact-type check) and
re-verifies every binding — including recovering the signer of each
signed transaction — at construction and before every relay
interaction.

1. `simulate()` takes NO payload: it simulates the exact prepared
   payload for the exact prepared target block via `eth_callBundle`
   (read-only relay operation). The response is checked for a top-level
   error, `firstRevert`, per-transaction errors, result-count
   mismatches and zero-gas entries; anything unsuccessful fails closed.
   A successful simulation stores a ONE-USE authorization keyed by the
   payload/target fingerprint.
2. `submit()` takes NO payload: an arbitrary signed bundle cannot gain
   submission authority by being passed to either method. It refuses to
   call `eth_sendBundle` unless ALL of the following hold, checked in
   this order BEFORE any relay submission call: the execution gate flag
   is set; every capability binding still holds (chain identity
   re-verified immediately before submission); the target block is
   still in the future (a reached block makes any simulation stale);
   and a successful simulation exists for the exact fingerprint. The
   authorization is consumed BEFORE the relay call — a relay timeout or
   any other submission failure cannot silently authorize unlimited
   retries; a retry requires a NEW successful simulation.
3. Outcomes are classified and audited for every attempt: simulations
   (`simulated`, `simulation_failed`, `simulation_refused`) and
   submissions (`accepted`, `rejected` for explicit relay errors,
   `uncertain` for timeouts/transport failures or hashless responses,
   `submission_refused` for pre-check refusals). Error text is
   sanitized: the exact signed payload hex is redacted and any long hex
   blob (potential raw transaction bytes or key material) never reaches
   the log.

The Flashbots relay therefore remains a transport only and never
constitutes an independent wallet-loading, authorization or
transaction-policy path. Simulation and submission are distinct
operations by construction.

## 6. First-use protocol

1. Principal funds the agent wallet's public address with Sepolia ETH.
2. DSS performs read-only balance/network checks.
3. Before the FIRST state-changing transaction or deployment, DSS reports
   target, purpose, estimated gas, maximum fee expenditure (§4) and
   intended transaction behaviour to the Coordinator for review.
4. Only after Coordinator approval is `DSS_TESTNET_EXEC=1` set in the agent
   runtime (never in CI, never committed) for that specific operation.
   Flashbots preparation signs, so the flag is required for BOTH the
   simulation and the submission phase (DSS-WO-ENG-005 §2).
5. For bundle operations, a successful simulation of the exact payload and
   target block is REQUIRED by the gate before any submission (§5).
6. The initial authorization covers ONE 0-value self-transfer with total
   fee exposure ≤ 0.002 native (§4). It is NOT a standing budget: any
   retry or further transaction requires a new authorization.

## 7. Audit

Each simulation and each submission — successful, failed, refused or of
uncertain outcome — appends a JSONL record with: timestamp, purpose,
chain ID, target, function, estimated gas, actual gas used, transaction
or bundle hash, outcome (`simulated` / `simulation_failed` /
`simulation_refused` / `accepted` / `rejected` / `uncertain` /
`submission_refused` / `submitted` / `mined` / `reverted` /
`receipt_unavailable`), maximum fee exposure (aggregate for bundles) and
a sanitized error field. No private credentials are ever recorded; the
exact signed payload bytes are redacted from error text by construction.
