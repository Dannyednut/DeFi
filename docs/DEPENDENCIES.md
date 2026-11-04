# Dependency inventory, compatibility matrix and failure register

**Work order:** DSS-WO-ENG-001 (Phase A) · **Python target:** 3.11 · **Lock files:** `requirements.txt` (runtime), `requirements-dev.txt` (dev/test, superset)

## 1. Python version decision

**Target: Python 3.11** (first compatibility target per the work order). Validated end-to-end on a standalone CPython 3.11.18 environment: hash-verified lock install, full module import graph (91 modules), and the complete test suite. All locked distributions publish CPython 3.11 wheels.

## 2. Import audit result (AST scan of every repository module)

Every declared runtime dependency is actually imported (no unused declarations). Three HTTP transports coexist by design (each serves different subsystems) — noted, not deduplicated, to avoid behavior changes in this work order.

| Distribution | Locked version | Before (requirements.txt) | Imported by | Verdict |
| --- | --- | --- | --- | --- |
| web3 | 6.20.4 | `>=7.0,<8` | 29 modules (core) | **range changed 7.x→6.x** — see §3 |
| flashbots | 2.0.0 | *(absent)* | executor/executor.py (guarded), tests/test_flashbots_integration.py (manual script) | **added** — see §3 |
| eth-account | 0.11.3 | `>=0.13,<1` | 4 modules | range widened to `>=0.8,<0.13` (web3 6 cap) |
| eth-abi | 6.0.0 | `>=5.0,<6` | 14 modules | range widened to `>=4` (web3 6 allows 4–6) |
| eth-utils | 4.1.1 | `>=5.0,<6` | 6 modules | range narrowed to `>=2.1,<5` (web3 6 cap) |
| hexbytes | 0.3.1 | `>=1.2,<2` | utils/serializer.py | range narrowed to `<0.4` (web3 6 cap); only `HexBytes` isinstance/serialization used |
| rlp | 5.0.0 | *(undeclared)* | utils/serializer.py | **declared explicitly** (direct import; transitive via eth-account) |
| python-dotenv | 1.2.4 | `>=1.0,<2` | 3 modules | kept |
| httpx | 0.28.1 | `>=0.27,<1` | 3 modules | kept |
| aiohttp | 3.14.5 | `>=3.9,<4` | prices.py | kept |
| websockets | 13.1 | `>=12,<16` | mempool/watcher.py | range narrowed to `<14` (web3 6 cap) |
| requests | 2.34.2 | `>=2.31,<3` | 3 modules | kept |
| nest-asyncio | 1.6.0 | `>=1.6,<2` | cache.py (deferred) | kept |
| pandas | 2.3.3 | `>=2.0,<3` | reporter.py | kept |
| matplotlib | 3.11.2 | `>=3.8,<4` | reporter.py | kept |
| pytest | 9.2.0 | *(undeclared)* | tests/ | **added** (dev lock only) |

Test/development dependencies beyond pytest: none required — the suite uses `unittest` (stdlib) plus pytest as the runner. Optional integrations (Flashbots relay path) are runtime-guarded in `executor/executor.py` and explicitly asserted by `scripts/ci_import_smoke.py`.

## 3. Flashbots dependency decision (evidence trail)

**Finding:** the code imports `flashbots` (`executor/executor.py`: `from flashbots import FlashbotsWeb3, flashbot`, behind an ImportError guard; `executor/relay.py` and `executor/executor.py` call `w3.flashbots.{send_bundle, simulate, cancel_bundles}`; the guarded path also handles the relay keystore lifecycle). The previous `web3>=7.0,<8` pin made this import **structurally impossible**:

- PyPI distribution **`flashbots`** (official Flashbots provider for web3.py) publishes: `<=0.4.3` → `web3>=5.17,<5.18`; `1.x` → `web3>=5.22,<6`; **`2.x` → `web3>=6,<7`**. Resolver proof: requiring `web3>=7` + `flashbots` is unsatisfiable ("No solution found").
- `flashbots 2.0.0` on Python 3.11 exports **exactly the two symbols the executor guard imports** (`FlashbotsWeb3`, `flashbot`) and attaches `w3.flashbots` with every method the relay path uses (`send_bundle`, `simulate`, `cancel_bundles`, `send_private_transaction`, `call_bundle`, bundle stats) — verified by runtime probe (no network needed for attach).

**Decision:** pin `web3>=6.20,<7` + `flashbots>=2.0,<3`. This is *not* a blind downgrade:

| Evidence | web3 7.16.0 (old pin, no flashbots) | web3 6.20.4 + flashbots 2.0.0 (new lock) |
| --- | --- | --- |
| Full test suite (105 test functions / 110 counted units) | 23 failed, 86 passed, 1 error | **identical: 23 failed, 86 passed, 1 error** |
| Failure list (test-by-test) | — | **byte-identical failure set** (JUnit diff) |
| Import graph | 91/91 modules | 91/91 modules |
| Flashbots relay path | structurally dead (guard ImportError → "bundle relay disabled") | **installable, importable, attached; guard succeeds** |
| Repo code vs web3 6 | — | no web3-7-only API in use (audited: no `solidityKeccak` alias, no `geth_poa_middleware`, no `AsyncWeb3`, no `strict_bytes_type_checking`; `to_checksum_address` exists since 6.2) |

Residual risk: the test suite is heavily mocked; web3 6-vs-7 differences on *live* RPC paths are not fully covered by tests. Mitigations: exact pins (reproducible), the change is revertible by editing `requirements.in`, and EXECUTE_ONCHAIN remains off by default. This trade-off is accepted deliberately: with web3 7 the Flashbots integration the code explicitly targets can never function; with web3 6 it is installed, imported and testable, with zero test regressions.

## 4. Deterministic lock files

- Generated with **pip-tools 7.6.2** (`pip-compile --generate-hashes --strip-extras`) running under CPython 3.11.18. Regeneration must use this exact pip-tools version; byte-identical reproduction of both committed locks from the committed `.in` files was verified with pip-tools 7.6.2 on 2026-11-04 (DSS-WO-ENG-001A).
- `requirements.txt`: 61 pinned distributions, every one with `--hash=sha256:` entries (2,410 hash lines).
- `requirements-dev.txt`: 66 pinned distributions (adds pytest + its runner deps + py-solc-x 2.1.0 for the Exec contract compilation tests, DSS-WO-ENG-002; runtime lock unchanged).
- Install with hash enforcement: `pip install --require-hashes -r requirements-dev.txt`.
- What the locks guarantee: exact pinned versions plus SHA-256 artifact verification at install time (`--require-hashes`). They do **not** guarantee identical binary artifacts across operating systems — wheels are platform-specific; reproducibility is scoped to the resolved versions and hash-verified artifacts on each platform.
- pytest pinned to the 9.x line because the DSS-MISSION-001 baseline register was produced under pytest 9.x accounting (unittest subTest failures counted individually); keeps CI counts directly comparable.
- Regeneration commands are documented at the top of both `.in` files and in docs/INSTALL.md.

## 5. Clean-environment validation (Phase B)

Fresh CPython 3.11.18 venv, pip wheel cache disabled (`--no-cache-dir`), both locks installed with `--require-hashes`:

1. Runtime lock install: OK (61 distributions, hash-verified).
2. Dev lock install: OK (65 distributions, hash-verified).
3. Import smoke test (`scripts/ci_import_smoke.py`): **91/91 modules import; 0 unexpected failures**; Flashbots status asserted OK (`flashbots 2.0.0 provides FlashbotsWeb3 + flashbot (web3 6.20.4)`).
4. Full suite, baseline scope (`--ignore=tests/test_flashbots_integration.py`): **23 failed, 86 passed, 1 error** — exact reproduction of the DSS-MISSION-001 baseline (its "+1 uncollectable file" is that excluded file).
5. Full suite, CI scope (additionally `--ignore=tests/test_multicall3_yieldout.py`): **22 failed, 86 passed, 1 error** (109 counted units).

No previously cached packages or local state were involved (standalone interpreter + `--no-cache-dir`).

**DSS-WO-ENG-002 update (2026-11-04):** the separate AtomicArbExecutor contract and the old in-repo Exec variant were retired by the Principal, and the repository was aligned with the authoritative `Exec.sol` from Dannyednut/EVM at pinned commit `898fe82f29a5695a01278365eb442ab3a26811b5`. The suite was re-run in this environment after the alignment: **20 failed, 91 passed, 1 error** (112 counted units, CI scope) — see §8. Items 4–5 above are the historical ENG-001 records.

## 6. Classified failure register

### ENG-002 register (current; 21 units across 18 test functions, CI scope: 20 failed + 1 error = 21 of 112 units)

| Category | Tests | Root cause |
| --- | --- | --- |
| Stale relay tests — API drift (13 units) | `tests/test_relay.py` `TestRelayFlashbots` (8) + `TestRelayTransaction` (2 → 5 subTest units) | tests target a removed `relay_signer=`/`chain_id` signature and public-fallback behavior the fail-closed relay redesign removed |
| Stale detector/forensics tests — API drift (3) | `test_shadow_audit.py::test_shadow_detection`, `test_decoder.py::test_decode_v3_exact_input_single`, `test_liquidation.py::test_detect_at_risk_position` | resolver/decoder/scanner API drift |
| Broken test infrastructure (1 **error**) | `test_optimizer.py::test_get_optimal_amount` | references a pytest fixture (`mode`) defined nowhere |
| Deterministic functional failures (4) | `test_cycle_detector.py::test_triangular_arbitrage`, `test_discovery.py::test_discover_v2`, `test_integration_arb_discovery.py::test_pipeline_discovers_and_simulates`, `test_simulator_flow.py::test_cross_dex_token_returns_arb` | detector/simulation logic on synthetic graphs (DSS-MISSION-001 findings) |

Changes vs the ENG-001 register, all caused by the ENG-002 retirement/alignment:

- **Fixed:** `tests/test_phase13_arbexec_adapter.py::test_arbexec_abi_matches_supplied_four_argument_swap` — `config.ARB_EXEC_ABI` is now generated-aligned with the authoritative 4-argument `swap(arb, lender, builderFeeBps, validUntilBlock)` (the old entry encoded a 3-argument variant).
- **Removed with the retired contract** (`AtomicArbExecutor` + old Exec variant): the Phase 12 contract-existence tests (including the B-category `FileNotFoundError` failure), the Phase 14 string-presence tests (`swapFast`/`flashLoanAave`/`executeAtomic`/`multiCallAtomic`/operator allow-lists), and three Phase 15 string-presence tests asserting the old variant's fee-aware V2 internals. Replaced by `tests/test_exec_contract_alignment.py` (15 tests: real compilation, ABI conformance, Python encoding, fail-closed behaviour).
- **Unchanged:** all remaining A/C-category failures above (pre-existing, owned by later work orders).

### Historical ENG-001 register (kept for traceability; superseded by the ENG-002 register above)

Baseline scope then: 23 failed / 86 passed / 1 error; CI scope: 22 failed / 86 passed / 1 error (109 units). Category B (the `AtomicArbExecutor` contract-existence failure) and the `test_phase13` ABI mismatch are gone for the reasons above.

### Excluded from CI (updated by DSS-WO-ENG-004)
| Test | Status |
| --- | --- |
| tests/test_flashbots_integration.py | **No longer excluded** (ENG-004). The module-level broadcast script described below was REPLACED on this branch by an import-safe, gate-routed integration test: it collects cleanly, its offline tests run in CI, and its two live phases skip visibly without `DSS_TESTNET_FLASHBOTS_LIVE=1` + RPC + the DSS agent keystore (CI provides none of these). The gate (executor/testnet_gate.py) is the single wallet/authorization path; the relay is a transport only. |
| tests/test_multicall3_yieldout.py::test_run_multicall3_batch_yieldOut | Still excluded (unchanged): requires a local artifact from a live run (`logs/pending_opportunities.jsonl`) before it even dials `https://0xrpc.io/eth`; fails on a clean clone with "No cycles to test." |

Also inspected: `tests/test_mc3.py` defines no pytest-collectable tests (manual script under `__main__`) — contributes 0 tests, harmless.

### Reconciliation (ENG-002 actuals)

- CI scope: 112 pytest units = **91 passing + 20 failing + 1 error**; 0 skipped. Baseline scope (without the second `--ignore`) remains 1 unit higher via the excluded environment-dependent test.
- `tests/test_flashbots_integration.py` remains the uncollectable file excluded everywhere.
- Historical ENG-001 reconciliation: 105 test functions = 84 passing + 20 failing + 1 error; pytest 9 units = 86 + 23 + 1.

## 7. Explicit non-goals honored (ENG-001)

No detector, contract, execution or strategy logic was modified in ENG-001; dependency/CI changes only. `EXECUTE_ONCHAIN` handling untouched. Fixing the register entries above belongs to later work orders (contract restoration, test repair).

## 8. DSS-WO-ENG-002 — authoritative Exec alignment

Per the Principal's direction, the separate `AtomicArbExecutor` contract is **retired** (not restored), and the active contract is the authoritative `Exec.sol` sourced from `Dannyednut/EVM`:

- **Pinned provenance:** commit `898fe82f29a5695a01278365eb442ab3a26811b5`; all seven contract files (Exec.sol, Helper.sol, five interfaces) copied byte-for-byte and re-verified by git blob SHA (`contracts/PROVENANCE.md`, mechanically checked by tests). The DeFi Exec copy (5-argument constructor `(_w, _q, _b, _aave, initialOperator)`) was replaced by the authoritative 3-argument `constructor(_w, _q, _b)` version — the variants were never combined.
- **Removed:** `executor/atomic_executor_abi.json`, `ATOMIC_EXECUTOR_ABI_PATH`, `_atomic_contract()`, `build_atomic_executor()`, the generic `executeAtomic` backend and its `backend="generic"` branches, `config.get_atomic_executor_address()` and all `ATOMIC_EXECUTOR_*` environment configuration, `config.get_extended_arb_exec_address()` and the `swapFast` extended selection (`ARB_EXEC_EXTENDED_ADDRESS_*`), and `executor/exec_extended_abi.json` (unused by any code; referenced only by a removed test).
- **Preserved:** `execution/atomic.py` plan/route compilation, preflight simulation, exact envelope construction, chain-ID validation and fail-closed behaviour; `ArbExecRoute`/`build_arbexec_route` adapter; all `ARB_EXEC_ADDRESS_*` deployment configuration (no addresses invented).
- **ABI:** `executor/exec_abi.json` is compiler-generated from the pinned source with solc `0.8.28+commit.7893614a` (SHA-256 verified against the official solc-bin checksum) and OpenZeppelin 5.7.0 exactly (`contracts/package-lock.json`). `config.ARB_EXEC_ABI` conforms to the generated interface, enforced by tests.
- **Solidity versions:** source pragmas `^0.8.19` (Exec) / GPL-2.0-or-later Helper; compiler pinned as above; OZ 5.7.0 (required for the OZ-v5 `Ownable(initialOwner)` constructor style).
- **New baseline (CI scope):** **20 failed / 91 passed / 1 error** (112 units) — see §6.

## 9. DSS-WO-ENG-004 — unified signing, Flashbots gate and fee caps (this branch)

`executor/testnet_gate.py` is the single authorization boundary for every
testnet signing operation; the Flashbots relay is a transport only:

- **Single wallet path:** `load_agent_wallet` (secret-store presence, file
  types/symlinks, 0600/0700 permissions, v3 keystore shape, hex passphrase
  decoding, `LocalAccount` construction, recorded-address binding). No
  environment variable can redirect the wallet or passphrase.
- **Fee exposure caps (§5 of the work order):** worst-case monetary
  expenditure `gasLimit × maxFeePerGas` / `gasLimit × gasPrice` capped at
  2×10¹⁵ wei, fee price capped at 50 gwei, priority fee at 2 gwei;
  missing/negative/malformed/contradictory inputs rejected. Applied per
  transaction and per bundle transaction (`validate_bundle`). Calculation
  and Coordinator-review note in docs/testnet-policy.md §4.
- **Simulation-before-submission (§4):** `FlashbotsBundleSession.simulate`
  evaluates the eth_callBundle response fail-closed (error, firstRevert,
  per-tx errors, result-count mismatch, zero-gas) and authorizes an
  order-sensitive fingerprint of (exact signed payload, exact target
  block); `submit` re-checks the execution gate, re-verifies the chain ID
  immediately before the relay call, refuses stale targets (block reached)
  and refuses any submission without a matching successful simulation;
  the authorization is consumed. `prepare_flashbots_bundle` signs only
  after gate + chain + policy + custody checks and never calls the relay.
- **Allowlist injection (§6):** offline tests may inject a controlled
  allowlist explicitly to exercise the real mismatch branch (allowlisted
  but unexpected RPC chain rejected before signing) and the
  chain-identity-change-between-signing-and-submission branch; the
  production map stays Sepolia-only and is never environment-settable;
  mainnet is rejected even on injected allowlists.
- **The old broadcast script is gone:** tests/test_flashbots_integration.py
  is now import-safe, gate-routed and collected by CI (its live phases
  skip visibly). The old workflow exclusion was removed.
- **Baseline note:** the 20 failures / 1 error from the ENG-002 scope are
  still present on this branch (fixed on work/dss-eng-001-deps-ci, PR #1;
  this branch forked before those fixes). All ENG-004 additions pass.
