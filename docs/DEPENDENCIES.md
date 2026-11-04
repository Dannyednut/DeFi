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
- `requirements-dev.txt`: 65 pinned distributions (adds pytest + its runner deps).
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

## 6. Classified failure register (24 units across 21 test functions + 1 uncollectable file)

Accounting note: 105 test functions; unittest `subTest` failures are counted individually by pytest 9, giving 110 counted units (86 passed / 23 failed / 1 error).

### A. Stale or invalid tests — API drift between test code and current production code (16 units / 13 functions; offline, deterministic)
| Test | Symptom | Root cause |
| --- | --- | --- |
| tests/test_relay.py — `TestRelayFlashbots` (8 functions) | `TypeError: _relay_flashbots() got multiple values for argument 'target_block'` | tests target a removed signature: `chain_id` positional + `relay_signer=` kwarg no longer exist; the fail-closed relay redesign removed the public-fallback behavior the tests assert |
| tests/test_relay.py — `TestRelayTransaction` (2 functions → 5 subTest units) | `TypeError: relay_transaction() got an unexpected keyword argument 'relay_signer'` | same signature drift |
| tests/test_shadow_audit.py::test_shadow_detection | `TypeError: OutcomeResolver._audit_shadow_trades() takes 3 positional arguments but 4 were given` | resolver API drift |
| tests/test_decoder.py::test_decode_v3_exact_input_single | `AttributeError: 'DecodedSwap' object has no attribute 'v3_fees'` | dataclass field removed/renamed |
| tests/test_liquidation.py::test_detect_at_risk_position | `TypeError: LiquidationScanner.__init__() missing 1 required positional argument: 'chain_id'` | chain-awareness refactor |
| tests/test_optimizer.py::test_get_optimal_amount (**error**) | `fixture 'mode' not found` | test references a pytest fixture that is defined nowhere (broken test infrastructure) |

### B. Dependency / artifact failures (1 unit; deterministic)
| Test | Symptom | Root cause |
| --- | --- | --- |
| tests/test_phase12_atomic_executor.py::test_atomic_executor_source_exists_and_is_fail_closed | `FileNotFoundError: contracts/AtomicArbExecutor.sol` | contract source absent from repository (identified in DSS-MISSION-001 as F2/F3); restoration is explicitly out of scope for ENG-001 (awaiting the contract-restoration work order) |

### C. Deterministic functional failures — detector/simulation logic on synthetic graphs (5 units)
| Test | Symptom |
| --- | --- |
| tests/test_cycle_detector.py::test_triangular_arbitrage | `AssertionError: False is not true` (no cycle found on synthetic graph) |
| tests/test_discovery.py::test_discover_v2 | `AssertionError: False is not true` |
| tests/test_integration_arb_discovery.py::test_pipeline_discovers_and_simulates | expected synthetic pool missing from graph |
| tests/test_multicall3_yieldout.py — *(reclassified, see D)* | — |
| tests/test_simulator_flow.py::test_cross_dex_token_returns_arb | simulator returned no opportunity (`assert None is not None`) |
| tests/test_phase13_arbexec_adapter.py::test_arbexec_abi_matches_supplied_four_argument_swap | adapter ABI parameter mismatch (DSS-MISSION-001 F6 calldata/ABI drift) |

### D. Network / environment-dependent tests — excluded from CI (2 files)
| Test | Why it cannot run in CI |
| --- | --- |
| tests/test_flashbots_integration.py (**uncollectable**; the "+1" of the baseline) | Lines 192–255 are a module-level manual script: loads `PRIVATE_KEY` from `.env`, signs a real transfer, then calls `w3.flashbots.simulate(...)` and **`w3.flashbots.send_private_transaction(...)`** against the Sepolia relay — a real broadcast. Without `flashbots` it fails at import; with it, it fails on the missing key. Excluded from all pytest runs; it must never be executed by automation. Flagged for rewrite as a proper skip-gated integration test in a future work order. |
| tests/test_multicall3_yieldout.py::test_run_multicall3_batch_yieldOut | Requires a local artifact from a live run (`logs/pending_opportunities.jsonl`) before it even dials `https://0xrpc.io/eth`; fails on a clean clone with "No cycles to test." Also counts toward the 23 baseline failures; excluded from CI scope only. |

Also inspected: `tests/test_mc3.py` defines no pytest-collectable tests (manual script under `__main__`) — contributes 0 tests, harmless.

### Reconciliation

- 105 test functions = 84 passing + 20 failing + 1 error.
- pytest 9 units = 86 passing + 23 failing + 1 error (the two `TestRelayTransaction` tests contribute 2 function-failures that expand to 5 subTest units).
- `tests/test_flashbots_integration.py` is the baseline's "1 uncollectable file" (excluded everywhere).
- Baseline scope: 23F/86P/1E — **matches DSS-MISSION-001 exactly**. CI scope: 22F/86P/1E.

## 7. Explicit non-goals honored

No detector, contract, execution or strategy logic was modified in this work order; dependency/CI changes only. `EXECUTE_ONCHAIN` handling untouched. Fixing the register entries above belongs to later work orders (contract restoration, test repair).
