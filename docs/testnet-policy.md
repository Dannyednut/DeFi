# DSS Testnet Policy (DSS-WO-ENG-003 D; unified per DSS-WO-ENG-004;
capability-gated per DSS-WO-ENG-005; payload-containment and
capability-integrity hardened per DSS-WO-ENG-006; exception containment
and access-list fail-closure per DSS-WO-ENG-007; provider-credential
containment and access-list type consistency per DSS-WO-ENG-008;
complete RPC/relay preflight containment and relay-response secret
redaction per DSS-WO-ENG-009; unified execution authorization boundary
per DSS-WO-ENG-015)

Status: ACTIVE — enforced by `executor/testnet_gate.py` through the unified
execution authorization boundary `executor/authorization.py`.
The gate is **disabled by default** (`DSS_TESTNET_EXEC` unset/empty). Nothing
in this repository signs or submits a transaction unless every condition in
this document is met.

## 1. Non-negotiable boundaries

| Rule | Enforcement |
| --- | --- |
| `EXECUTE_ONCHAIN` remains the production guard and stays `false` | `config.py`; the gate does not read or modify it (asserted by test) |
| Ethereum mainnet (chain ID 1) is always refused | hard-coded in `check_chain_id` (not configurable) |
| Unknown chains fail closed | `ALLOWED_TESTNET_CHAINS` is the only authority |
| Production allowlist is Sepolia-only and never configurable | module constant `ALLOWED_TESTNET_CHAINS`, read directly by every check; NO interface accepts an alternative map (R1); tests controlled-mock the module map (see §2) |
| Gate disabled unless the REAL process environment carries `DSS_TESTNET_EXEC=1` | `gate_opted_in()` — the single authoritative read of `os.environ` (R1); EVERY operation that decrypts the wallet and signs — direct RPC and Flashbots preparation alike — requires it (`require_ready`); bundle submission re-checks it inside `FlashbotsBundleSession.submit` (DSS-WO-ENG-005 §2). No configuration object, API parameter or injected mapping can enable the gate |
| Missing RPC / keystore / passphrase / address fails closed | `require_ready` (signing paths) / `require_provisioned` (read-only chain/RPC checks) |
| ONE wallet-loading path for every signing operation | `load_agent_wallet` (see §3); the Flashbots transport has no wallet logic of its own |
| Only the sanctioned preparation path creates a submission session | `PreparedFlashbotsBundle` capability minted exclusively by `prepare_flashbots_bundle`; `FlashbotsBundleSession` refuses forged, duck-typed or subclassed objects and re-verifies every binding (wallet identity via signer recovery, chain ID, validated intent, exact signed payload, target block) at construction and before every relay interaction (DSS-WO-ENG-005 §3; DSS-WO-ENG-006 §3) |
| Signed raw transaction bytes never leave the gate-controlled implementation | `prepare_flashbots_bundle` returns ONLY the session; the capability has no signed-payload property; `simulate()`/`submit()` accept no payload and return scrubbed relay responses; audit records never contain the payload (DSS-WO-ENG-006 §2/§4) |
| Prepared-bundle state is immutable after creation; every signed transaction must match its validated intent field-by-field | frozen state dataclass behind a single slot with refused `__setattr__`/`__delattr__`; frozen `ValidatedTransactionIntent` records; decoded signed transactions compared against intents (sender, chain ID, recipient, nonce, value, calldata, gas limit, fee envelope) at construction and before every relay interaction (DSS-WO-ENG-006 §3) |
| Chain ID verified before signing AND immediately before submission | both transports: `sign_and_submit_testnet_tx` and `prepare_flashbots_bundle` + `FlashbotsBundleSession.verify_chain` |
| No arbitrary EOA transfers | `validate_transaction`: recipient = agent wallet or reviewed target |
| No unlimited token approvals | approval selectors decoded; amount capped at `MAX_TOKEN_APPROVAL_AMOUNT` |
| No unverified contract targets | `KNOWN_TARGETS` empty by default; entries require a separately reviewed, explicit code change; NO execution interface accepts a target override (R1) |
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

**Allowlist isolation (R1).** There is no allowlist parameter anywhere:
`check_chain_id`, `TestnetExecutionConfig` and the orchestrations take no
alternative map, so a caller can never expand the live allowlist.
Deterministic offline testing is preserved by controlled mocks — tests
patch the module map (`patch.dict(ALLOWED_TESTNET_CHAINS, {...})`) to
exercise the allowlisted-but-unexpected branch: an allowlisted RPC chain
that differs from the expected one is rejected before wallet signing, and a
chain identity change between signing and submission prevents sending.
Mainnet (1) is rejected even when the mocked map contains it. Map patching
is a test technique, not an operational interface.

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

## 5. Simulation-before-submission and the capability boundary (DSS-WO-ENG-004 §4; DSS-WO-ENG-005 §3–§4; DSS-WO-ENG-006 §2–§5)

`prepare_flashbots_bundle` performs the gate checks (it signs, so the
full execution gate is required — same as direct RPC), chain
verification, transaction policy including the aggregate bundle fee cap,
wallet custody binding and signing — it never calls the relay. It then
mints a `PreparedFlashbotsBundle` capability (the ONLY minting path;
external construction is refused at runtime) binding the verified wallet
identity, chain ID, the frozen normalized transaction-intent records,
the EXACT signed payload, the EXACT target block and their
order-sensitive fingerprint, and returns the `FlashbotsBundleSession`
built on that capability — and NOTHING else (DSS-WO-ENG-006 §2): no
tuple, no signed raw transaction bytes, no capability object, no target
block. The session constructor refuses every other object (exact-type
check) and re-verifies every binding — including recovering the signer
of each signed transaction and comparing each DECODED signed transaction
field-by-field against its validated intent (recovered sender, chain ID,
recipient, nonce, value, calldata, gas limit, EIP-1559/legacy fee
envelope) — at construction and before every relay interaction. Any
mismatch fails closed before simulation or submission.

**Signed-payload containment (DSS-WO-ENG-006 §2/§4).** The exact signed
transaction bytes exist ONLY inside the gate-controlled implementation:

1. the preparation API returns the controlled session ONLY;
2. the capability exposes NO signed-payload property (and no
   validated-transaction-dict property — intents are exposed as frozen
   records only);
3. `simulate()` and `submit()` accept NO arguments, operate only on the
   privately held prepared payload, and return relay responses SCRUBBED
   of raw-transaction material (raw-transaction-shaped keys redacted,
   byte strings of broadcastable size redacted, long hex blobs redacted)
   before they leave the session;
4. nothing in the module writes signed bytes to the audit log, test
   output, JSON artifacts or console output.

A caller can request preparation, simulation and controlled,
authorization-checked submission — but cannot obtain the raw signed
payload from the preparation API or any public session surface.

**Caller-visible exception containment (DSS-WO-ENG-007 Finding A;
provider-credential containment per DSS-WO-ENG-008 Finding A).**
Relay and HTTP transports routinely quote the request body in exception
messages, so a transport failure during simulate/submit would otherwise
carry the exact signed payload hex — AND credential material — to the
CALLER even though the audit record is sanitized. The session therefore
never re-raises an original transport or evaluation exception and never
attaches one as a visible cause or context (`__cause__` and
`__context__` are detached and the sanitized failure is raised outside
the failing except block, so implicit chaining cannot re-attach it
either).

Per DSS-WO-ENG-008 (preferred containment), a FOREIGN (non-gate)
exception's TEXT is never forwarded to the caller or the audit log at
all: provider/HTTP exception text can embed credential-bearing RPC URLs,
provider API keys, URL user information, query-string tokens, Basic/
Bearer authorization values and signed transaction bytes in shapes no
redaction heuristic can reliably recognize. Callers — and the audit
record — receive only the failure classification (refused / failed /
rejected / uncertain), the original exception CLASS NAME and the safe
stage (e.g. `bundle submission uncertain: RuntimeError during relay
submission call`).

The only detail ever RETAINED is relay-provided error text (the
top-level simulation error, `firstRevert`, per-transaction errors and
the explicit `eth_sendBundle` rejection body), because it is needed to
diagnose relay refusals. Before it reaches any caller-visible exception
or the audit log it is sanitized in layers: (1) exact-match redaction of
the registered credential material — the configured RPC and relay URLs
verbatim plus their user information, path-embedded provider keys and
query-string token values; (2) structural rules — any `scheme://…` URL
replaced wholesale, Bearer/Basic scheme tokens redacted,
credential-shaped assignments (api key / token / secret / password /
passphrase / authorization / credentials = value) redacted; (3) any
100+ character hex blob — with a lower- or uppercase `0x`/`0X` prefix
or none — redacted as potential raw signed transaction bytes.
Relay-supplied hash fields are held against the same long-hex rule
before they reach the audit log. **Generic long-hex redaction alone is
explicitly NOT credential protection** — provider credentials are
typically short and non-hex, which is why the foreign-text drop and the
registered-secrets/structural layers exist. Sanitized audit coverage,
failure-path logging and the one-use submission authorization are
unchanged. Gate-raised `TestnetSafetyError` refusals keep their exact
type and message with cause and context detached. Custody and signing
failures in both orchestration paths are contained the same way
(classification + class name + stage; gate-raised custody refusals keep
their exact message). Python exceptions are an in-process interface
only: this is containment against accidental leakage through error
paths, not a barrier against hostile same-process code (see the honest
threat model above).

**Complete preflight containment (DSS-WO-ENG-009 Findings A/B).** The
ENG-008 boundary covered the signing, simulation, submission and receipt
stages. ENG-009 closes the remaining surface: EVERY external RPC/relay
operation of BOTH orchestrations now runs under the same
foreign-exception containment — provider construction (`RPC provider
initialization`), the connectivity check (`RPC connectivity check`), the
initial chain-ID read (`RPC chain-ID read`), the RPC-backed
transaction-default fill (`RPC transaction-default read`), the pre-sign
and pre-submission chain-ID rechecks (`pre-sign chain verification`,
`pre-submission chain verification`), the block-number read for the
bundle's target block (`RPC block-number read`), relay transport
construction (`relay transport construction`) and session construction
with its live RPC verification (`session construction verification`). A
provider or relay failure at any of those points therefore cannot expose
a credential-bearing RPC URL, a relay URL, a provider API key or any
other provider diagnostic text: the caller receives only the failure
classification, the exception class name and the fixed safe stage,
raised outside the failing except block. The contained READS are
separated from the policy verdicts: chain-identity and
transaction-policy refusals are gate decisions, not transport failures,
and keep their exact gate messages. Nothing about the success paths
changed — preparation still returns only the controlled session, and
the direct-RPC path still distinguishes policy refusals from uncertain
submission outcomes.

**Per-transaction error and relay-response redaction (DSS-WO-ENG-009
Findings C/D).** The per-transaction simulation-error branch now
receives the SAME registered credential material as the top-level error
and `firstRevert` branches, so a relay can no longer surface a bare,
unlabelled provider key (no URL, no `token=`/`apikey=` label) through an
individual transaction result. The recursive relay-response scrubber
likewise receives the registered exact-match secret list: every returned
string — including nested metadata and otherwise unlabelled values — is
stripped of every configured secret value, and a KEY that itself embeds
an exact configured secret is redacted along with its value. The
pre-existing redactions (raw-transaction-shaped keys, broadcastable byte
strings, long hex blobs) are unchanged, and legitimate relay metadata —
hashes, gas figures, statuses — is preserved: responses are redacted,
never discarded.

**Registered-secrets scope (DSS-WO-ENG-010 Findings A–C; DSS-WO-ENG-011).**
The registered credential material is widened honestly, not blanketly.
The complete URL user-information pair AND the password itself are
registered regardless of length (an ordinary short username such as
`agent` is NOT registered individually). Any query parameter whose NAME
clearly indicates credential material — `token`, `api_key`/`api-key`/
`apikey`, `access_key`, `secret`, `password`/`passwd`/`pwd`,
`passphrase`, `authorization`, `auth` (exact match only),
`credential(s)`, and compound names ending in an unambiguous credential
noun — has its value registered regardless of length. Parameter names
are percent-decoded safely BEFORE classification (DSS-WO-ENG-011), so an
encoded name (`access%5Ftoken`, `api%2Dkey`, a mixed-case encoded name)
is recognized exactly like its decoded form, while an ordinary name
containing encoded characters (`net%77ork` → `network`, `%76%32` →
`v2`) still matches nothing and its value stays available. Registered
values are held in their raw, percent-decoded and form-decoded (`+` as
space) forms, so either echo shape is contained. Long path-embedded
provider keys keep the >=16-character rule; ordinary short query values
(network names, environment labels, project identifiers) and short
route components (`v2`) remain unregistered — legitimate relay metadata
is preserved, not discarded, and benign diagnostics are not over-
redacted.

**Access-list policy (DSS-WO-ENG-007 Finding B — Option A, fail closed;
type consistency per DSS-WO-ENG-008 Finding B).** The supported
transaction policy covers plain legacy and EIP-1559 value-and-calldata
intents only; access lists are OUT OF POLICY at this execution
boundary. Any supplied non-empty `accessList` is rejected by
`validate_transaction()` BEFORE anything is signed (and before wallet
decryption for bundles; one rejected transaction rejects the whole
bundle), a malformed (non-list — including tuples, empty or not)
`accessList` is rejected, and the intent minting refuses an access list
independently as defense in depth. The accepted types are exactly:
absent, `None` and an empty LIST. The signed-transaction decoder
independently refuses any non-empty access list in the signed EIP-1559
envelope (field 8) and refuses EIP-2930 envelopes outright, so a signed
transaction that differs from its validated intent by an access list
fails closed before any relay interaction. No access-list field is
captured in the intent records: an absent, `None` or EMPTY accessList
is accepted — eth_account signs an empty list either way, so the only
accepted form is exactly one: none. Full binding (Option B: normalizing
the access list into the intent and comparing it exactly) was
considered and rejected as the strictly larger change with no policy
gain — the gate's own preparation path can never mint an access-list
intent, so there is no legitimate access-list payload to bind.

**Capability immutability (DSS-WO-ENG-006 §3).** Prepared-bundle state
is a frozen dataclass (`_PreparedBundleState`) held behind a single
`__slots__` slot on the capability; attribute assignment and deletion on
the capability are refused; the transaction intents are frozen
`ValidatedTransactionIntent` records (never mutable dictionaries); the
minting token is a name-mangled class-private object (`__MINT_AUTHORITY`
→ `_PreparedFlashbotsBundle__MINT_AUTHORITY`), not a module-level public
name. Exact-type and signer-recovery checks are retained, and the
per-transaction and AGGREGATE fee exposure is re-derived from the frozen
intents and held against the configured caps and the recorded total
before every relay interaction (DSS-WO-ENG-006 §5).

**Honest threat model (DSS-WO-ENG-006 §3).** This boundary is an
APPLICATION-LEVEL authorization boundary. It is NOT independent hardware
or process isolation. Python private attributes, name mangling, frozen
dataclasses and the runtime authority check deter accidental and
ordinary misuse — but arbitrary hostile code executing inside the same
interpreter can defeat all of them (`object.__setattr__`, mangled-name
lookup, monkeypatching, reading module globals). The gate therefore does
NOT claim cryptographic unforgeability against same-process hostile
code, and its defense in depth (exact-type checks, signer recovery,
fingerprint binding, decoded signed-intent comparison, fee rechecks,
fail-closed defaults) is what actually refuses forged or tampered
content. If a stronger isolation model is ever required — a separate
signing process with a narrow IPC interface, OS sandboxing, or
HSM/KMS-backed keys — that is a FUTURE ARCHITECTURE REQUIREMENT to be
specified and reviewed separately; it is deliberately NOT invented here,
and no keyring/KMS/HSM mechanism is claimed to exist in the current
runtime (see §3 residual risk).

1. `simulate()` takes NO payload: it simulates the exact prepared
   payload for the exact prepared target block via `eth_callBundle`
   (read-only relay operation). The response is checked for a top-level
   error, `firstRevert`, per-transaction errors, result-count
   mismatches and zero-gas entries; anything unsuccessful fails closed.
   A successful simulation stores a ONE-USE authorization keyed by the
   payload/target fingerprint. The response returned to the caller is
   scrubbed (see containment above).
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
   retries; a retry requires a NEW successful simulation. The returned
   result object is scrubbed (see containment above).
3. Outcomes are classified and audited for every attempt: simulations
   (`simulated`, `simulation_failed`, `simulation_refused`) and
   submissions (`accepted`, `rejected` for explicit relay errors,
   `uncertain` for timeouts/transport failures or hashless responses,
   `submission_refused` for pre-check refusals). Preflight
   infrastructure/RPC failures are audited too: once an attempt has
   been initiated it cannot silently escape the audit path
   (DSS-WO-ENG-006 §6). Error text is contained (DSS-WO-ENG-008
   Finding A): foreign transport/HTTP/provider exceptions contribute
   ONLY the classification, the exception class name and the safe
   stage; retained relay-provided text is sanitized against the
   configured credential material plus the structural rules, and any
   long hex blob (potential raw transaction bytes or key material)
   never reaches the log.

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
a contained error field. No private credentials are ever recorded: for
foreign transport/HTTP/provider exceptions the error field carries only
the classification, the exception class name and the safe stage
(DSS-WO-ENG-008 Finding A); retained relay-provided text is sanitized
against the configured credential material and the structural rules; the
exact signed payload bytes are redacted by construction and relay
responses are scrubbed of raw-transaction material AND credential-shaped
strings before they are returned to callers (DSS-WO-ENG-006 §4/§6;
DSS-WO-ENG-008 Finding A). Preflight refusals — including
infrastructure/RPC failures — are recorded with the contained cause once
an attempt has been initiated.

## 8. Unified execution authorization boundary (DSS-WO-ENG-015)

DSS-WO-ENG-014 identified a split-signing architecture: the safeguards in
this document protected the gate-controlled testnet transports, but the
ordinary production executor signed transactions with an import-time
`PRIVATE_KEY` signer (`executor/signer.py`) and provisioned its own
Flashbots relay identity (`relay_signer.json` + middleware attach),
completely outside this boundary. ENG-015 eliminates that separation.

**One authoritative boundary.** `executor/authorization.py` is the single
mandatory execution authorization boundary. It selects the applicable
execution policy and hands out the ONLY two signing/submission
capabilities, both of which are the gate orchestrations documented above:

- `AuthorizedExecution.sign_and_submit` → `sign_and_submit_testnet_tx`
  (direct-RPC transport);
- `AuthorizedExecution.prepare_flashbots_bundle` → the capability/session
  model (Flashbots transport; simulate-before-submit, payload containment).

There is no second authorization implementation: the boundary performs
policy SELECTION only, and every actual check (explicit opt-in, allowlist,
chain identity, wallet custody, transaction policy, fee exposure, audit)
remains in `executor/testnet_gate.py`.

**Policy selection.** `resolve_execution_policy` is the single authoritative
selection point. `CHAIN_ID` (network selection) is never read there and can
never authorize execution. The ONLY operationally selectable policy is the
explicit Sepolia testnet policy (`DSS_TESTNET_EXEC=1` + full provisioning,
checked by `require_ready` at every authorization). A named
`ProductionExecutionPolicy` class exists to make the architecture explicit
and is PERMANENTLY DISABLED — enabling production/mainnet execution requires
a separately reviewed authorization decision recorded as a code change.
`EXECUTE_ONCHAIN` stays false by default and is NOT an execution authority:
setting it true cannot enable, select or bypass any policy (it only produces
an explicit warning).

**Every transport covered.** All execution routes go through the boundary:

| Route | Enforcement |
| --- | --- |
| Ordinary executor `submit` (detector + mempool + backrun opportunities) | `ArbExecutor.submit` authorizes via `authorize_execution` before building any transaction; submission goes through `sign_and_submit` (direct) or the gate session (Flashbots transport) |
| Batch execution | `ArbExecutor.submit_batch` authorizes once for the batch ("arb_batch_execution") and submits the combined transaction through the same orchestrations |
| Mempool-triggered / backrun execution (`MEVExecutionService`) | authorizes first, refuses mixed-signer bundles (the wallet-bound session model cannot carry a third-party trigger transaction), submits single private transactions through the gate session; accepts UNSIGNED intents only — no pre-signed bytes can be handed in |
| Execution router | armed ONLY by `boundary.execution_available()`; the `EXECUTE_ONCHAIN` boolean is not consulted for authorization |
| Validation sender (`validation/opportunity.py`, `validation/adversarial.py`) | the simulation `from` address comes from `authorized_sender_address` (the recorded PUBLIC agent address); no execution policy → validation refuses (fail closed, as the legacy signer did) |

**Removed legacy paths.** The following no longer exist, so bypassing the
boundary is structurally impossible for agent code (not merely checked):

- `executor/signer.py` (import-time `PRIVATE_KEY` loading, direct signing,
  `get_sender`) — module deleted; `config.PRIVATE_KEY` removed;
- the executor's private relay-identity provisioning
  (`relay_signer.json`, `Account.create()`, `flashbot()` middleware attach);
- `relay_transaction` / `_relay_direct` / `_relay_flashbots` broadcast
  helpers in `executor/relay.py` — the module keeps only read-only receipt
  handling. The ONLY `send_raw_transaction` / `send_bundle` call sites in
  the repository are the gate orchestrations (asserted by test).

**Fail-closed semantics.** A refused execution (disabled policy, chain
mismatch, policy violation, custody failure, RPC mismatch or failure at any
verification point) raises `TestnetSafetyError` and produces an explicit
failed `ExecutionResult` (`method="refused"`) — never a success, never a
silent skip, and there is no legacy fallback. Missing wallet material, a
missing/unverifiable RPC and policy violations all refuse.

**Audit completeness (§7 extension).** Refusals raised before the first
relay/submission call — policy, chain and custody refusals in BOTH
orchestrations — are now audited as `refused` records whenever the gate is
enabled, so successful, refused, failed and uncertain attempts are all
accounted for. Refusals under a disabled gate write nothing (no execution
context exists); the boundary likewise audits refused `authorize_execution`
attempts only when a policy with an audit log is selected.

**Known consequences (intentional, fail-closed).**

- `KNOWN_TARGETS` remains EMPTY: an arb execution transaction targets the
  ArbExec contract, which is neither the agent wallet nor a reviewed target,
  so the ordinary executor is refused at the transaction policy until the
  Coordinator authorizes a specific target in a separate change. Offline
  tests controlled-mock the module map (same technique as the allowlist).
- Atomic-backrun bundles that front a third-party (victim) transaction are
  refused: the session model binds every bundle transaction to the verified
  agent wallet. Extending the capability model to mixed-signer bundles is a
  future, separately reviewed architecture decision.
- Batch members are no longer individually relay-simulated: per-member
  prepared payloads were never submitted (the batch is one combined
  transaction), so preparing them would mint one-use authorizations for
  unused payloads. Batch gas estimates use the detector/optimizer values.

### 8.1 Override isolation (DSS-WO-ENG-015 R1)

The Coordinator's independent review of ENG-015 identified two runtime
authorization weaknesses caused by test-only configuration overrides being
exposed through production execution interfaces. R1 eliminates both
structurally — the override routes no longer exist rather than being
checked.

**Testnet policy configuration is isolated.** The authorization decision is
re-derived from the authoritative sources at EVERY check; configuration
objects carry no authorization state:

- `TestnetExecutionConfig` no longer has an `enabled` field. The explicit
  opt-in has exactly ONE authority: the REAL process environment, read by
  `gate_opted_in()` (the only read of `DSS_TESTNET_EXEC` in the repository)
  inside `require_ready()` and `FlashbotsBundleSession.submit()`. A
  manually constructed configuration — however it is built — can never
  authorize execution; replaying the old `TestnetExecutionConfig(enabled=
  True)` bypass fails at construction with `TypeError`.
- The allowlist field is gone. `require_provisioned`, `check_chain_id` and
  both orchestrations read the code-controlled `ALLOWED_TESTNET_CHAINS` map
  directly; no function, constructor or environment variable accepts an
  alternative map, so the live allowlist cannot be expanded by a caller.
- `resolve_execution_policy()` takes no argument: there is no
  caller-supplied environment injection. `ExecutionAuthorizationBoundary`
  accepts no `env` either. The optional `policy` wiring argument exists for
  offline tests only and confers no authorization authority: every policy's
  `authorize` re-runs the gate's environment-anchored checks.
- Direct calls to the gate orchestrations cannot bypass the central policy:
  `config` arguments carry operational endpoints only (RPC URL, secret-store
  file locations, audit-log path, relay URL) and every authorization-relevant
  input is re-read from the authoritative sources inside the gate.
- Offline determinism is preserved through environment patching
  (`patch.dict(os.environ, ...)`) and controlled mocks of module maps —
  test techniques, never operational interfaces.

**Known-target injection is removed.** The `known_targets` parameters on
`AuthorizedExecution.sign_and_submit`, `AuthorizedExecution.prepare_
flashbots_bundle`, `MEVExecutionService.submit`, the gate orchestrations and
`validate_transaction`/`validate_bundle` no longer exist: normal execution
ALWAYS validates recipients against the code-controlled `KNOWN_TARGETS` map
(still EMPTY). A contract target can only be approved through a separately
reviewed, explicit code change to that map. Offline tests patch the module
map as a controlled mock.

**Regression coverage (R1 §3).** `tests/test_execution_authorization.py`
adds `TestR1EnvironmentAuthority`, `TestR1AllowlistIsolation` and
`TestR1TargetInjection`: a hand-built fully-provisioned configuration
refuses without the real-environment opt-in (boundary, gate orchestration
and `require_ready`); Base/Arbitrum/OP/BNB/Holesky/Polygon are refused even
when a caller points a configuration at them; Sepolia is pinned as the only
production-allowlisted testnet; direct gate-orchestration calls cannot
bypass the central policy (including with `EXECUTE_ONCHAIN=true`);
runtime callers cannot pass target maps into `AuthorizedExecution` or
`MEVExecutionService.submit` (`TypeError`) and unapproved recipients are
refused against the empty production map; the standard application route is
fully disabled without the real opt-in; and no refusal ever decrypts the
wallet or broadcasts anything.

**Submission-path audit (R1 §4).** The structural invariant test now greps
the repository for EVERY broadcast mechanism — `send_raw_transaction`,
`send_transaction`, `eth_sendRawTransaction`, `eth_sendBundle`,
`send_bundle` — and asserts only `executor/testnet_gate.py` matches, plus
asserts no production module issues a send-shaped JSON-RPC method through
`make_request`. Wallet custody, chain verification, transaction
restrictions and fee-exposure checks are unchanged.

### 8.2 Security policy is not caller-configurable (DSS-WO-ENG-015 R2)

The R1 review confirmed the authorization inputs (opt-in, allowlist,
targets) are isolated from callers; R2 closes the remaining gap the
Coordinator identified: `TestnetExecutionConfig` still exposed
security-critical fields, and the gate orchestrations trusted them.

**The security policy is the code-controlled `SECURITY_POLICY` singleton**
(`executor/testnet_gate.TestnetSecurityPolicy`). It is an instance WITHOUT
state (`__slots__ = ()`); every property reads a reviewed module constant
at access time, so no per-instance override exists:

| Policy decision | Authoritative constant |
| --- | --- |
| Allowed chain IDs | `ALLOWED_TESTNET_CHAINS` (Sepolia only) |
| Execution opt-in | `DSS_TESTNET_EXEC=1` in the REAL process environment (`gate_opted_in()`) |
| Max transaction value | `MAX_TESTNET_TX_VALUE_WEI` |
| Max gas units | `MAX_TESTNET_GAS_UNITS` |
| Max fee per gas | `MAX_TESTNET_MAX_FEE_PER_GAS_WEI` |
| Max priority fee | `MAX_TESTNET_MAX_PRIORITY_FEE_WEI` |
| Max per-tx AND aggregate bundle fee | `MAX_TESTNET_TX_FEE_WEI` |
| Approved contract targets | `KNOWN_TARGETS` (deliberately EMPTY) |
| Wallet custody locations | `DEFAULT_KEYSTORE_PATH` / `DEFAULT_PASSPHRASE_PATH` / `DEFAULT_ADDRESS_PATH` (the DSS secret store) |
| Audit destination | `DEFAULT_TX_LOG_PATH` |
| Reviewed relay | `FLASHBOTS_SEPOLIA_RELAY` |

**Operational configuration is strictly separated.**
`TestnetExecutionConfig` now carries EXACTLY `rpc_url` and
`expected_chain_id` — genuinely operational selections. The security
fields (`max_value_wei`, `max_gas_units`, `max_fee_per_gas_wei`,
`max_priority_fee_wei`, `max_total_fee_wei`, `keystore_path`,
`passphrase_path`, `address_path`, `tx_log_path`, `relay_url`) are GONE:
constructing a config with any of them raises `TypeError`, and no
environment variable is read for any of them. The policy-validation
primitives (`validate_transaction`, `validate_bundle`,
`validate_fee_exposure`, `validate_bundle_fee_exposure`) accept no limit
override parameter at all — both orchestrations (direct RPC and
Flashbots) enforce the SAME immutable constants, including the aggregate
bundle fee ceiling re-checked before every relay interaction.

**Wallet custody** always uses the designated DSS secret-store paths:
`verify_secret_store()` and `load_agent_wallet()` take no configuration —
there is no parameter through which a caller could substitute a keystore,
passphrase or recorded address. All existing secret-file type, permission,
symlink, directory-exposure, keystore-format and identity-binding checks
are unchanged.

**Audit** always goes to the designated destination. Refused, failed,
uncertain and successful outcomes remain accounted for; an audit-write
failure never rewrites a verdict — the original refusal is still raised
and the write failure is logged.

**Relay** is always the reviewed Sepolia Flashbots relay; the gate hands
exactly `FLASHBOTS_SEPOLIA_RELAY` to the relay transport — a caller
cannot redirect the signing/submission session.

**Policy injection cannot weaken the gate.** The optional `policy`
argument on `ExecutionAuthorizationBoundary` selects WHICH operational
config governs a request; the gate re-enforces every security control
from `SECURITY_POLICY` (and the real environment) at every authorization
and every signing/submission call, so an injected policy — or a
hand-minted `AuthorizedExecution` — cannot produce an authorized handle
with weakened gate settings.

**Test isolation.** Offline tests redirect the custody/audit (and, for
credential-containment coverage, relay) constants with
`tests/gate_r2_fixtures.throwaway_store` — `unittest.mock.patch.object`
scopes on the module constants, the same controlled-mock technique used
for the allowlist and target maps. `IsolatedTestnetConfig` is a test-only
operational config whose security-named attributes are live reads of the
policy and whose constructor REFUSES divergent values; it is immutable
and never imported by production code. Test injection is not an
operational interface.

**Regression coverage (R2 §5).** `tests/test_r2_policy_isolation.py`
pins: enormous configured value/gas limits cannot weaken the real caps;
enormous fee-per-gas/priority ceilings cannot weaken the fee caps; an
enormous aggregate ceiling cannot weaken the Flashbots bundle cap;
caller-supplied custody paths cannot replace the designated store;
caller-supplied audit paths cannot redirect or suppress records and an
audit-path failure keeps the refusal fail-closed; caller-supplied relay
URLs cannot redirect the session; direct orchestration calls, injected
policies and hand-minted handles cannot bypass the central policy; both
transports enforce identical constants; every refusal path leaves the
wallet undecrypted, nothing signed and nothing broadcast; and the
standard application route stays disabled without the real opt-in.
Structural invariants pin the orchestration signatures, the
operational-only config fields, the `SECURITY_POLICY` destinations in
the orchestration sources, and — repo-wide — that the ONLY broadcast
markers in non-test code live in `executor/testnet_gate.py`.
