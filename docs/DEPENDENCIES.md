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

### ENG-003 register (CURRENT — resolved: 0 failed, 0 errors)

**DSS-WO-ENG-003 (2026-11-04) repaired every unit in the ENG-002 register.** The offline CI suite now passes completely: **129 passed, 0 failed, 0 errors, 3 visibly skipped** (intentionally gated live-network tests, never runnable in CI). Failures in any new run are regressions, not baseline noise.

| ENG-002 register entry | Resolution in ENG-003 |
| --- | --- |
| Stale relay tests (13 units) | `tests/test_relay.py` rewritten for the current fail-closed interface. **Production defect found and fixed:** the bundle-not-included path in `_relay_flashbots` fell through and returned `None`, crashing callers reading `RelayResult` attributes. Legacy public-fallback / `relay_signer=` / unknown-chain→mainnet behaviour intentionally NOT restored (security removals). |
| Detector/forensics drift (3) | Interfaces reconciled (block-payload `_audit_shadow_trades`, `DecodedSwap.fee`, `LiquidationScanner` cache arg). **Production defect found and fixed:** `BaseDetector._estimate_gas_usd` declared `float` but returned a tuple on success — the liquidation scanner crashed on every real position (silently swallowed by `except: pass`), making the detector permanently dead. Tuple contract documented, scanner unpacks correctly. |
| Broken optimizer test (1 error) | The file was a live-network manual script with a `mode` parameter pytest read as a missing fixture. Replaced by deterministic offline tests of the borrow math (verified against an independent float profit model) and entrypoint routing; the manual diagnostic is preserved behind `DSS_LIVE_OPTIMIZER_DIAG=1` and skips visibly. |
| Synthetic graph failures (4) | **Two production defects found and fixed:** (1) `utils/multicall3.py` used `UNISWAP_V2_FACTORY_ABI`/`UNISWAP_V3_FACTORY_ABI` without importing them — every batched swap-path discovery raised `NameError` and was silently swallowed, disabling V2/V3 pool discovery entirely; (2) `estimate_tvl_from_edge` recomputed TVL from token USD prices, returning $0.00 for any pool containing an unpriced token and making the simulator's TVL guard drop it, even when the edge carried a good precomputed TVL. Regression coverage added (`tests/test_multicall3_discovery.py`); discovery tests rewritten for the current async batched architecture. |

### ENG-002 register (HISTORICAL — superseded by ENG-003; kept for traceability; 21 units across 18 test functions, CI scope: 20 failed + 1 error = 21 of 112 units)

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

### Excluded / gated tests (current state after ENG-003 + PR #2 gate work)
| Test | Status |
| --- | --- |
| tests/test_flashbots_integration.py | **Rewritten in ENG-003 (Workstream B, PR #1)** and NO LONGER excluded: the old module-level `PRIVATE_KEY`/signing/broadcast-at-import script is gone. The file now collects safely, never touches keys or network at import, and both live phases skip visibly without `DSS_TESTNET_FLASHBOTS_LIVE=1`. Since DSS-WO-ENG-015 R4 the test is routed through the retained Builder → Signer → Relay path gated by the EXECUTE_ONCHAIN master switch (the retired `executor/testnet_gate.py` / `DSS_TESTNET_EXEC` no longer exist). Chain-ID gate (mainnet always refused, unknown chains fail closed) is unit-tested offline. CI can never sign, broadcast or submit through it. |
| tests/test_multicall3_yieldout.py::test_run_multicall3_batch_yieldOut | Still excluded from CI: requires a local artifact from a live run (`logs/pending_opportunities.jsonl`) before it even dials `https://0xrpc.io/eth`; fails on a clean clone with "No cycles to test." Classified network/environment-dependent; candidate for the same gated rewrite later. |
| tests/test_optimizer.py (live diagnostic portion) | The offline tests always run; the preserved manual live diagnostic skips visibly unless `DSS_LIVE_OPTIMIZER_DIAG=1`. |

Also inspected: `tests/test_mc3.py` defines no pytest-collectable tests (manual script under `__main__`) — contributes 0 tests, harmless.

### Reconciliation (ENG-003 actuals)

- CI scope (with the single remaining `--ignore` for `test_multicall3_yieldout.py`): **129 passed, 0 failed, 0 errors, 3 skipped** (all three skips are intentional opt-in live-network gates with visible reasons: Flashbots Sepolia simulation, Flashbots Sepolia submission, optimizer live diagnostic). `tests/test_flashbots_integration.py` is now collected (import-safe) instead of excluded.
- The ENG-002-era unit counts (112 units) are historical: the relay suite was restructured (21 → 16 units), the broken optimizer script became 10 offline tests + 1 gated diagnostic, discovery gained regression coverage (`tests/test_multicall3_discovery.py`, 4 units) and the chain-ID gate gained offline tests (4 units).
- Historical ENG-002 reconciliation: 112 pytest units = 91 passing + 20 failing + 1 error; 0 skipped. Historical ENG-001 reconciliation: 105 test functions = 84 passing + 20 failing + 1 error; pytest 9 units = 86 + 23 + 1.

## 7. Explicit non-goals honored (ENG-001)

No detector, contract, execution or strategy logic was modified in ENG-001; dependency/CI changes only. `EXECUTE_ONCHAIN` handling untouched. Fixing the register entries above belongs to later work orders (contract restoration, test repair).

## 8. DSS-WO-ENG-002 — authoritative Exec alignment

Per the Principal's direction, the separate `AtomicArbExecutor` contract is **retired** (not restored), and the active contract is the authoritative `Exec.sol` sourced from `Dannyednut/EVM`:

- **Pinned provenance:** commit `898fe82f29a5695a01278365eb442ab3a26811b5`; all seven contract files (Exec.sol, Helper.sol, five interfaces) copied byte-for-byte and re-verified by git blob SHA (`contracts/PROVENANCE.md`, mechanically checked by tests). The DeFi Exec copy (5-argument constructor `(_w, _q, _b, _aave, initialOperator)`) was replaced by the authoritative 3-argument `constructor(_w, _q, _b)` version — the variants were never combined.
- **Removed:** `executor/atomic_executor_abi.json`, `ATOMIC_EXECUTOR_ABI_PATH`, `_atomic_contract()`, `build_atomic_executor()`, the generic `executeAtomic` backend and its `backend="generic"` branches, `config.get_atomic_executor_address()` and all `ATOMIC_EXECUTOR_*` environment configuration, `config.get_extended_arb_exec_address()` and the `swapFast` extended selection (`ARB_EXEC_EXTENDED_ADDRESS_*`), and `executor/exec_extended_abi.json` (unused by any code; referenced only by a removed test).
- **Preserved:** `execution/atomic.py` plan/route compilation, preflight simulation, exact envelope construction, chain-ID validation and fail-closed behaviour; `ArbExecRoute`/`build_arbexec_route` adapter; all `ARB_EXEC_ADDRESS_*` deployment configuration (no addresses invented).
- **ABI:** `executor/exec_abi.json` is compiler-generated from the pinned source with solc `0.8.28+commit.7893614a` (SHA-256 verified against the official solc-bin checksum) and OpenZeppelin 5.7.0 exactly (`contracts/package-lock.json`). `config.ARB_EXEC_ABI` conforms to the generated interface, enforced by tests.
- **Solidity versions:** source pragmas `^0.8.19` (Exec) / GPL-2.0-or-later Helper; compiler pinned as above; OZ 5.7.0 (required for the OZ-v5 `Ownable(initialOwner)` constructor style).
- **New baseline (CI scope, ENG-002, HISTORICAL):** **20 failed / 91 passed / 1 error** (112 units) — fully repaired in ENG-003; see §6. Current expectation: fully green (see ENG-003 reconciliation).

## 9. DSS-WO-ENG-004 — unified signing, Flashbots gate and fee caps (PR #2)

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
- **Baseline note (updated by the ENG-012 reconciliation):** the 20
  failures / 1 error from the ENG-002 scope — still present on this
  branch while it was forked before the PR #1 fixes — were resolved by
  PR #1 and are gone after merging `main`; the integrated branch passes
  the corrected baseline. All PR #2 additions (ENG-004 through ENG-011)
  pass.

## 10. DSS-WO-ENG-016 — canonical runtime path reconciliation (PR #3)

One coherent, correctly validated execution path for ordinary and
mempool-backrun opportunities; the unused alternative orchestration is
retired with call-site evidence (repository-wide import/call audit):

- **Retired — `executor/mev_execution.py`** (MEVExecutionService): zero
  production callers (only a structural signature test referenced it).
  It was an alternative orchestration layer that signed a
  caller-supplied unsigned transaction dict directly, bypassing the
  builder's transaction policy. Promotion, validation, simulation and
  routing already happen in the canonical path
  (OpportunityEngine → ExecutionRouter → ArbExecutor → Builder → Signer
  → Relay).
- **Retired — `mev/models.py`, `mev/simulator.py`, `mev/inclusion.py`:**
  after the orchestration retirement these had no production consumers
  (tests + package re-exports only). The canonical simulation paths are
  `validation/opportunity.py` (eth_call/estimateGas), the adversarial
  simulator and `executor/relay.simulate_bundle`. The orphaned
  inclusion-bid arithmetic (`BidConfig`/`BidResult`/`BidOptimizer` in
  `optimizer/execution_economics.py`) was removed with its only
  consumer.
- **Retained — `mev/policy.py`:** production consumer is
  `executor/relay.py` (ChainMEVPolicy gates private bundle submission).
- **Retired — `mempool/engine.py`:** the pipeline instantiated
  MempoolOpportunityEngine but never called any method; classification
  and execution gating happen in the canonical path. The unused
  `PendingObservation` import went with it.
- **Retained (inactive, out of scope):** `mempool/reorg.py`
  (TriggerTracker), `executor/relay_registry.py`, `executor/nonce_manager.py`
  have no current production callers but are not part of the named
  reconciliation scope; disposition deferred to a future work order.
- **Corrected runtime defects (regression-covered in
  `tests/test_eng016_backrun_correctness.py` and `tests/test_relay.py`):**
  mempool execution profit handling (validated-profit fail-closed + the
  contract-level minProfit guard), preflight-skip audit handling
  (distinct `skipped` audit outcome, no AttributeError in enabled
  contexts), and the Flashbots final chain-identity recheck immediately
  before `send_bundle` (parity with the direct route).
- **Lower-priority opportunities (§6):** non-streaming opportunities are
  not queued and not batch-processed — they remain tracked only. The
  misleading router comment claiming batch pickup was corrected; the
  dormant `Exec.multiCall` batch path (independent per-call failure —
  not atomic) was NOT re-enabled.

## 11. DSS-WO-ENG-017 — final architecture reconciliation (PR #3)

This section is the post-reconciliation architecture baseline. It
describes what the repository ACTUALLY implements. §10's deferral note
("reorg/relay_registry/nonce_manager retained inactive") is superseded by
the final dispositions below.

### 11.1 Authoritative production call graph

    PoolCrawler ──► TokenGraph (pool/reserve state) ──┐
    BlockInformationCollector (per-block state) ──────┤
    MempoolWatcher ─► MempoolPipeline ─► MempoolSimulator
                              │  (PendingOpportunity)
                              ▼
              OpportunityEngine.process_candidates  ◄── block detectors
                      │   (dedupe by executable-route identity,
                      │    validate_many, adversarial sim, enrich, rank)
                      ▼
              PipelineResult.ready ──► ExecutionRouter.submit_opportunity
                      │                  (serial single worker; fingerprint
                      │                   dedup; streaming priority rules)
                      ▼
              ArbExecutor.submit (EXECUTE_ONCHAIN checked here)
                      │   _validated_profit_wei → lender → profit gate →
                      │   build_arb_data (validated floor = ArbData.minProfit)
                      ▼
              Builder.build_execute_tx (transaction policy)
                      ▼
              Signer.sign_transaction (switch/chain/custody)
                      ▼
              Relay.relay_transaction (switch/chain/provenance; direct
                                       or Flashbots bundle + simulation)
                      ▼
              OpportunityTracker lifecycle (OPEN/UPDATE/CLOSE) + audit log

Mempool backruns ride the SAME engine/validation/router path
(MempoolPipeline._promote_and_route → OpportunityEngine.process_candidates
→ ExecutionRouter.submit_opportunity(source=BACKRUN, priority=2.0)); no
mempool component can promote a candidate to execution-ready on its own.

### 11.2 Active component responsibilities

| Component | Actual responsibility |
| --- | --- |
| `opportunity_engine.py` | Single canonical pipeline: detector scheduling, executable-route dedup, validation, adversarial simulation, economics enrichment, ranking, bounded ready queue (`max_ready`). |
| `validation/opportunity.py` | Execution-grade validation: on-chain `yieldOut` quote + full state-changing `eth_call`/`estimateGas` at a pinned block; stamps `quote_profit_wei`, `validated_profit_floor_wei` (90 % quote retention), `validated_gas_units`, lender mode. |
| `validation/adversarial.py` | Backrun adversarial simulation against the trigger transaction. |
| `optimizer/opportunity_economics.py` | Canonical economics enrichment: converts gas to profit-token units, computes net profit via `ExecutionEconomics`, stamps `economics_profitable`. |
| `execution_router.py` | Operational gate + serial execution queue (one worker, priority streaming for backruns/high profit, per-block execution cap, fingerprint dedup). |
| `executor/executor.py` | Per-opportunity execution orchestration: validated-floor resolution (fail closed), lender selection, profit gate, ArbData construction, simulation, sign, relay. |
| `executor/builder.py` | ArbData + transaction construction; full transaction policy at the construction boundary (single AND batch). |
| `executor/signer.py` | The only signing path (custody, chain binding, switch recheck). |
| `executor/relay.py` | The only submission path (direct RPC or Flashbots bundle; switch/chain/provenance rechecks; bundle simulation). |
| `executor/lender.py` | Flash-loan source selection (Aave → Balancer → pool flash swap). |
| `executor/audit.py` | Execution audit records (success/refused/failed/skipped). |
| `mev/policy.py` | `ChainMEVPolicy`: chain MEV capability profile, enforced by the relay at private submission. |
| `detectors/*` | One thesis each (cycles, new_pool, cross_proto, stable_imbalance, split_route, liquidation); quote/discovery only. |
| `mempool/*` | Watcher (pending tx stream), decoder, impact simulator, outcome resolver (mined/dropped/accuracy), deduper. |
| `crawler.py` | Pool discovery workers (V2/V3 log workers, Curve/Balancer/SyncSwap/Aerodrome bootstraps, event discovery, stale refresh, authoritative TVL, GC). |
| `tracker.py` | Opportunity lifecycle by executable-route fingerprint (OPEN/UPDATE/CLOSE, FILLED_BY_COMPETITOR/EXPIRED). |

### 11.3 Final disposition table (every ENG-017 candidate)

| Candidate | Disposition | Evidence |
| --- | --- | --- |
| `executor/mev_execution.py`, `mempool/engine.py`, `mev/models.py`, `mev/simulator.py`, `mev/inclusion.py`, inclusion-bid classes | **Retired (ENG-016, verified)** | Deleted in commit `0dd6525`/`b2e76f8`; zero live imports; only absence-asserting guard tests + docs + explanatory docstrings remain. |
| `executor/nonce_manager.py` | **Retired (ENG-017 §3B)** | Execution is deliberately SERIAL: `ExecutionRouter` runs exactly one worker (now clamped to 1 with a loud warning) and the executor fetches a fresh `pending` nonce per tx at build time (`build_execute_tx`). No concurrent nonce race exists; an unintegrated reservation utility would only suggest unsupported concurrency. Module deletion + symbol ban enforced in `tests/test_r4_execution_boundary.py`. |
| `executor/relay_registry.py` | **Retired (ENG-017 §3C)** | Relay selection = chain `FLASHBOTS_RPCS` config + `ChainMEVPolicy` + fail-closed `profile_for` checks in `executor/relay.py`. No multi-relay requirement exists; the registry had zero production importers (test-only). |
| `mempool/reorg.py` (`TriggerTracker`) | **Retired (ENG-017 §3D)** | `mempool/outcome.py` resolves every pending trigger against the mined block (receipt-based outcome; not-mined ages out as `dropped` after `MAX_PENDING_BLOCKS`); a REPLACED trigger re-enters the pipeline as a fresh pending tx (new hash → decode → simulate → promote). Backrun validity is enforced fail-closed at the execution boundary (`execution_simulation` fetches the trigger tx and bundles its raw payload; a replaced/dropped trigger fails the sim or the bundle is never included). Sole importer was a test. |
| `ProtocolOpportunityDetector` facade | **Retired (ENG-017 §3E)** | Zero references repo-wide. `main.py` registers `CrossProtocolDetector` + `StableImbalanceDetector` individually; the canonical engine owns orchestration. The two real detector classes remain in `detectors/protocol_opportunities.py`. |
| Sharpe detector (external-API two-pool subclass) | **Retired (ENG-017 §3F)** | Never registered (`main.py` had it commented out), no test, no docs; detector name collided with its parent (`"cycles"`), which would have corrupted detector provenance; network-bound at detection time; produced the SAME executable two-pool cross-DEX routes CycleDetector already finds from local graph state. An external-API signal source is a future strategy-research option, not an active detector. |
| `execution/` (`route_ir`, `route_compiler`, `protocol_builders`, `arbexec_adapter`, `atomic`) + `simulation/exact_route.py` | **Research-only (ENG-017 §3G)** | Zero production consumers (only intra-package edges + `simulation/exact_route.py → execution/route_ir.py`). Fully offline-tested (`test_phase7/10/11/13`, `test_exec_contract_alignment`). Retained as the unique route-IR → multi-protocol calldata → Exec-adapter research surface. ISOLATION IS EXPLICIT: compiling/adapting a route does NOT make a protocol executable through the deployed Exec.sol (V2-style pairs + V3 pools only). Known fidelity limits documented in `arbexec_adapter` (fixed 997/1000 V2 fee math; ≥2-pool BadPath floor now enforced). NOT wired into the runtime — that would manufacture the appearance of capability the contract does not have. |
| `simulation/economics.py`, `simulation/state_transition.py` | **Retired (ENG-017 §3G)** | Zero consumers beyond their own re-exports; pure duplicates (economics delegation to `optimizer.execution_economics`; bundle simulation already in `executor/relay.py`). |
| `RelayTarget` (mev/policy.py); `EconomicsInput` + `ExecutionEconomics.from_input` | **Retired (ENG-017 §4)** | Zero consumers; `from_input` also hid a unit assumption (gas in the same unit as every other term) that the canonical enrichment deliberately avoids. |
| `main.py` dead decoder imports; `detectors/base.py` unused imports/dead method/commented blocks; cycles.py orphaned imports; `_v2_worker` | **Retired (ENG-017 §4)** | Text-match leads verified against real usage first: decoder setup lives in `MempoolPipeline`; `_v2_worker` was a strictly more expensive duplicate of the started `_v2_log_worker` that nothing could activate. |
| `PoolCrawler._metadata_backfill_loop` | **Retained, deliberately NOT started (documented)** | The only token-metadata backfill utility (symbols/decimals/prices for tokens discovered without them). Slow, subgraph-bound maintenance job; activation path documented at the commented launch line in `crawler.start()`. |

### 11.5 Protocol execution-capability matrix

The deployed contract (Dannyednut/EVM @ 898fe82f, `contracts/Exec.sol`) has
exactly one swap dispatch (`_swap`): a `fee()`-selector probe routes to
Uniswap-V3 pool logic, everything else must answer the standard
Uniswap-V2 pair interface (`token0()/token1()/getReserves()/swap()`),
with V2 outputs computed at the FIXED 997/1000 fee factor
(`Helper.getAmountOutV2`). Balancer is a flash-loan SOURCE only, never a
swap venue. Capability requires the chain's `arb_exec_address`
(configured for chains 1, 56, 11155111 only — every other chain fails
closed at the builder).

| Protocol label (edge.protocol) | Discovery | Quote/simulation | Route compilation (research/) | Canonical validation | Exec.sol native execution | Production execution eligibility |
| --- | --- | --- | --- | --- | --- | --- |
| `uniswap_v2` (incl. SushiswapV2/PancakeV2/RingswapV2 forks — same label by `DexConfig.__post_init__`) | ✅ V2 log worker + factories | ✅ reserves / yieldOut | ✅ route_compiler + adapter | ✅ OpportunityValidator | ✅ standard pair interface — executed at 30 bps math regardless of configured fee (fidelity gap for 25 bps forks) | **YES** (`ARB_EXEC_NATIVE`) |
| `uniswap_v3` (incl. SushiswapV3/PancakeV3 forks) | ✅ V3 worker + subgraphs | ✅ Quoter / yieldOut | ✅ route_compiler + adapter | ✅ OpportunityValidator | ✅ `fee()`-probe V3 path | **YES** (`ARB_EXEC_NATIVE`) |
| `aerodrome_v2` | ✅ (Base log worker) | ✅ adapter `getAmountOut` | ✅ `protocol_builders.aerodrome_v2` | ❌ gated out by `execution_capability` | ❌ no Solidly/stable branch in Exec.sol | **NO** — discovery/quote/research only |
| `curve` | ✅ (bootstrap/API) | ✅ adapter `get_dy` | ✅ pool + Router-NG builders | ❌ | ❌ | **NO** — discovery/quote/research only |
| `balancer_v2` | ⚠️ vault env config only | ✅ `queryBatchSwap` | ✅ batchSwap builder | ❌ | ❌ as swap venue (flash-loan source only) | **NO** — discovery/quote/research only |
| `syncswap` | ✅ (zkSync log worker) | ✅ adapter `getAmountOut` | ✅ router swap builder | ❌ | ❌ | **NO** — discovery/quote/research only |

`routing.execution_capability` (single source of truth at detection time)
keeps every non-`uniswap_v2`/`uniswap_v3` route out of validation and
execution with an explicit reason string; adapters existing in
`protocols/` or `execution/protocol_builders.py` do NOT change this.

### 11.6 Canonical opportunity validation, economics and deduplication

* **Validation evidence.** `OpportunityValidator` quotes the route through
  the real contract (`yieldOut`) and then simulates the full state-changing
  `swap` at a pinned block. `validated_profit_floor_wei` = 90 % of the
  validated quote (controlled quote-drift allowance); `quote_profit_wei`
  is the raw contract quote. Both are TOKEN-denominated (the borrowed
  `tokenIn`'s base units).
* **Missing vs invalid (fail closed).** The executor resolves the floor
  with `optimizer.opportunity_economics.enrich`'s exact precedence:
  floor key present → must be a positive integer; genuinely absent →
  permitted positive quote fallback; detector simulation profit as the
  last evidence tier. An explicitly present zero/negative/non-numeric
  value is INVALID and fails closed before any build/sign/submit — it is
  never silently replaced. `enrich` likewise refuses to attach economics
  to non-positive or malformed evidence.
* **Contract-level guard.** `ArbData.minProfit` = max(validated floor,
  retained stricter detector value). The one retained stricter value is
  the CycleDetector's estimated gas cost, written in tokenIn units ONLY
  when tokenIn is the chain's wrapped native (otherwise 0) — origin and
  denomination verified. Native-currency gas figures are NEVER assigned
  to the token-denominated guard; a successful Flashbots simulation never
  overwrites or weakens it (simulation runs the real contract, so success
  itself proves the simulated profit reaches the floor; only the gas
  LIMIT is refined from measured `gas_used`, and gas economics stay in
  the off-chain profitability assessment). Ordinary, mempool and batch
  preparation all run the SAME policy.
* **Executable-opportunity identity.** Engine dedup and tracker
  fingerprints share one identity: chain + ordered token path + ordered
  pool path + dex set. Detector theses (arb_kind/opp_type/detector) are
  semantic classification, never identity. Equivalent routes from
  different detectors consolidate into ONE canonical candidate (strongest
  validated quote wins) with merged `detector_provenance`/
  `consolidated_theses`; the router's queue dedup rides the same
  fingerprint, so a duplicate cannot enter execution twice through
  different detector sources. Methodology note: no representative
  historical production data exists in the repository; the consolidation
  and identity rules are proven with deterministic detector-shaped
  fixtures in `tests/test_eng017_opportunity_identity.py` (3 detector
  theses over one route → 1 candidate; no production frequency statistics
  are claimed).

### 11.7 Lower-priority (non-streaming) opportunity disposition

Unchanged from ENG-016 and re-confirmed: non-streaming opportunities are
NOT queued and NOT batch-processed — they remain tracked only
(lifecycle via `tracker.process_block`, honest `False` return from
`submit_opportunity`). The dormant `Exec.multiCall` batch path is NOT a
safe substitute (independent per-call failure — not atomic) and stays
disabled; `submit_batch_opportunities` has no production caller.
Backrun/high-priority candidates retain their streaming priority
(priority 2.0, backrun boost), the per-block execution cap
(`MAX_EXECUTIONS=3`) and bounded queue (`QUEUE_SIZE=10`) are respected,
and no candidate is ever reported queued/executed when it was not (the
mempool dashboard now records every significant candidate with its
actual promoted count). Broader opportunity allocation and capital
prioritisation belong to the upcoming strategy-development phase —
execution volume and master policy were NOT changed here.

### 11.8 Execution authorization boundary and disabled default

* `EXECUTE_ONCHAIN` (process environment) is the single master switch,
  default DISABLED. It is read live (`config.execution_enabled()`) at
  every boundary: router arming/queueing, executor submission (single
  and batch), signer, and relay (independently re-checked immediately
  before broadcast and again right before `send_bundle`).
* The transaction policy at the construction boundary enforces: chain
  identity, recipient = configured `arb_exec_address`, zero native
  value, gas/fee ceilings, monetary fee-exposure cap, no access lists —
  identical for single and batch.
* Wallet custody remains the single signer path; no key material exists
  outside `executor/signer.py`. All development and CI runs use offline
  fixtures and mocked RPC/relay/builder/signer boundaries; no
  transaction was signed or submitted during this reconciliation.
