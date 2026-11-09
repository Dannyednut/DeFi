# DSS Execution Policy (DSS-WO-ENG-015 R4)

Status: ACTIVE — enforced by the retained multi-chain execution
architecture: `execution_router.py`, `executor/executor.py`,
`executor/builder.py`, `executor/signer.py`, `executor/relay.py`.

This document supersedes the DSS-WO-ENG-003…015-R3 *testnet gate* policy.
The Coordinator's R4 decision (DSS-WO-ENG-015 R4) retired the parallel
testnet authorization architecture — `executor/authorization.py` and
`executor/testnet_gate.py`, the `DSS_TESTNET_EXEC` switch, the testnet-only
policy objects — and moved every security control that survived R0–R3 into
the EXISTING components, on ONE application execution path:

```
ExecutionRouter → ArbExecutor → Builder → Signer → Relay → Receipt/Result
```

The router is an operational gate. The signer and the relay independently
re-enforce the master switch and their own safety checks, so no single
component is load-bearing alone.

## 1. Non-negotiable boundaries

| Rule | Enforcement |
| --- | --- |
| `EXECUTE_ONCHAIN` is the SINGLE master execution switch; disabled by default | `config.execution_enabled()` — the authoritative read of the REAL process environment at operation time; there is no second switch and no parameter that can override it |
| The decision is checked at the actual operation boundaries | router (`execute_onchain` property re-evaluated at every access), executor (`submit`/`submit_batch`), signer (before any signing), relay (before any broadcast) |
| `CHAIN_ID` is the configured network identity | `config.CHAIN_ID`; the executor is constructed per configured chain |
| Chain identity verified before signing AND immediately before broadcasting | `executor.signer.check_chain_identity` (twice: pre-sign and immediately pre-sign) and `executor.relay` (entry + immediate pre-broadcast recheck on BOTH routes — direct send and Flashbots bundle submission, DSS-WO-ENG-016 §3C) |
| The unsigned transaction's `chainId` must equal the expected chain | signer refuses otherwise |
| The signed payload must originate from the approved signing process | relay decodes the signed envelope (strict self-contained RLP decoder), checks the consensus chainId and recovers the signer — a payload not signed by the recorded agent identity is refused |
| ONE wallet-loading path | `executor.signer.load_wallet` — v3 keystore + hex-encoded passphrase + recorded-address identity binding; no second signer, no fallback, no wallet generation |
| Credentials stay outside the repository | the DSS runtime provisions the secret store directory and exports it as `DSS_SECRET_STORE` (fail-closed when unset); 0600 files / 0700 directory, symlink and permission checks before every load |
| No arbitrary recipients, no deployments | builder: `to` must be EXACTLY the chain's configured `arb_exec_address` (`ChainConfig` is the authority); empty recipient refused |
| Zero native value on the arbitrage path | builder refuses any unexpected native value (flash-loan funded) |
| Gas AND monetary fee caps, per chain | `execution_profile.FEE_POLICIES` (§2 below) — a gas-unit cap is not a monetary cap |
| Batch execution is not a weaker alternative | `build_execute_batch_tx` runs the SAME `validate_transaction_policy` as `build_execute_tx` |
| Unsupported chain / missing profile / missing fee policy / missing recipient → fail closed | no permissive default is ever invented |
| A failed private submission never falls back to the public mempool | `executor.relay` private path is fail-closed; the chain-specific MEV policy (`mev.policy.ChainMEVPolicy`) gates atomic backruns |
| No transaction access lists | builder refuses any supplied accessList; the signed-payload decoder independently refuses a non-empty EIP-1559 access list |
| Every attempt audited — success, refusal, failure, uncertain or preflight skip | `logs/execution_audit.jsonl` (gitignored); error text sanitized; credentials and raw signed transaction bytes never recorded; ordinary preflight skips are recorded as the distinct outcome `skipped` in enabled contexts (DSS-WO-ENG-016 §3B) |
| Shared RPC provider stays read/write | `utils/rpc_manager.py` is NOT restricted: the executor uses its supplied provider for legitimate operations; all signing/submission flows exclusively through signer + relay (structurally tested) |

## 2. Chain-aware fee-exposure policy

Limits live in `execution_profile.FEE_POLICIES` (one frozen
`ChainFeePolicy` per reviewed chain; `fee_policy_for()` fails closed for
anything else). Basis for the selected values (details in the module
source):

* **Fee price ceilings** are set well above each chain's observed
  operating range, so ordinary congestion never blocks execution while a
  pathological outlier is refused: Ethereum 750 gwei (~7× historical
  p99.9), BSC 30 gwei, Polygon 2000 gwei, Base 2 gwei, Arbitrum 10 gwei,
  zkSync 1 gwei, Sepolia 50 gwei (R2-approved unit caps preserved).
* **Priority-fee ceilings** sit above each chain's fast-inclusion band.
* **Gas ceilings** sit below each chain's practical block limit with
  headroom over `GAS_MULTIPLIER`-inflated batch builds.
* **Per-transaction monetary cap** = gas ceiling × fee-price ceiling
  (worst-case expenditure of ONE transaction). Sepolia note: R2's 0.002
  native ceiling was the approved ONE-OPERATION budget for the initial
  21k-gas self-transfer, not an engine-wide cap; the R4 entry is the
  internally consistent 500k × 50 gwei = 0.025 native.
* **Aggregate bundle cap** = 2 × per-transaction cap (a backrun bundle
  carries our searcher transaction(s) plus a third-party trigger whose gas
  we do not pay).
* **Native value cap = 0** for the current arbitrage execution path.

Raising or adding any entry is a reviewed code change, never an
environment setting.

## 3. Wallet custody and provisioning arrangement

The engine never creates, funds, imports or auto-generates a wallet. The
DSS runtime provisions an out-of-band secret store — a directory OUTSIDE
the repository containing `dss_wallet.keystore.json` (web3 secret-storage
v3), `dss_wallet.pass` (hex-encoded passphrase, the central keystore
format produced by `scripts/dss_wallet_generate.py`) and
`dss_wallet.address` (recorded public identity) — and exports its location
as `DSS_SECRET_STORE`. The path is never hardcoded and never resolved
inside the repository; with the variable unset the signer fails closed.

Identity binding: the address derived from the keystore must equal the
recorded public address, or the wallet load refuses. In the current DSS
agent deployment the provisioned store still uses the retired
`dss_testnet_wallet.*` file names; enabling live execution requires a
one-time operator rename to the names above (reported separately in the
R4 completion report as a provisioning requirement).

## 4. What was retired by R4 (and where its protections live now)

| Retired (R1–R3) | Retained equivalent (R4) |
| --- | --- |
| `DSS_TESTNET_EXEC` switch | `EXECUTE_ONCHAIN` master switch (`config.execution_enabled()`) |
| `ALLOWED_TESTNET_CHAINS` allowlist | `CHAIN_ID` + chain configuration + fail-closed profiles/fee policies (`get_chain`, `profile_for`, `fee_policy_for`) |
| `KNOWN_TARGETS` (empty) target map | the chain's configured `arb_exec_address` is the ONLY recipient |
| `ExecutionAuthorizationBoundary` / `AuthorizedExecution` | direct enforcement in signer + relay (+ router operational gating) |
| Gate session/capability model for Flashbots | relay-internal transport construction from `config.FLASHBOTS_RPCS`; signed-payload provenance verification; no public fallback |
| Testnet-only fee caps | chain-aware `FEE_POLICIES` (§2) |
| `logs/testnet_tx_log.jsonl` | `logs/execution_audit.jsonl` (same JSONL shape, sanitized) |

The R1–R3 structural guarantees that survive unchanged: execution disabled
by default; chain-identity verification before signing and before
broadcasting; a single authoritative wallet-loading path with identity
binding; secret-file permission and symlink checks; transaction value/gas
and monetary fee validation; approved-recipient checks; audit records for
attempts and outcomes; credential redaction on every error surface;
fail-closed behaviour for missing configuration.
