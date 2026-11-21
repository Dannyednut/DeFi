# Installation and testing from a fresh clone

**Work order:** DSS-WO-ENG-001 · **Python:** 3.11 (CI-validated) · **No repository secrets or RPC credentials required.**

## 1. Create an isolated environment

```bash
git clone https://github.com/Dannyednut/DeFi.git
cd DeFi
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

## 2. Install the locked dependencies

```bash
# runtime stack (hash-verified, fully pinned)
python -m pip install --require-hashes -r requirements.txt

# development/test stack (superset of the runtime lock)
python -m pip install --require-hashes -r requirements-dev.txt
```

`--require-hashes` makes pip resolve exactly the pinned versions and verify
every downloaded artifact against the committed SHA-256 hashes. Scope note:
the locks pin versions and verify artifact integrity on each platform; they
do not imply identical binary artifacts across operating systems (wheels
are platform-specific).

## 3. Verify the import graph

```bash
python scripts/ci_import_smoke.py
```

Imports every repository module and asserts the Flashbots dependency status
explicitly (`flashbots 2.0.0` must provide `FlashbotsWeb3` + `flashbot` on
`web3 6.20.4`, i.e. the provider imports and attaches successfully). This
verifies dependency wiring only — relay execution (bundle submission) is
not exercised here and still requires separate validation. Expected output
ends with `IMPORT SMOKE: PASS` (91 modules).

## 4. Run the test suite

```bash
python -m pytest tests \
  --ignore=tests/test_multicall3_yieldout.py \
  -p no:cacheprovider -ra
```

- Expected result (DSS-WO-ENG-003, 2026-11-04): **129 passed, 0 failed,
  0 errors, 3 skipped** — the suite is fully green. The 3 skips are
  intentional opt-in live-network gates that skip visibly with their
  reason and can never run in CI (Flashbots Sepolia simulation +
  submission, optimizer live diagnostic). Historical baselines
  (ENG-002: 20 failed / 91 passed / 1 error; ENG-001: 22 failed /
  86 passed / 1 error) are kept in `docs/DEPENDENCIES.md` §6 for
  traceability and are no longer the current state.
- Any failure or error in a fresh run is a regression, not baseline
  noise; no failure is hidden by CI.
- The suite includes the Exec contract alignment tests
  (`tests/test_exec_contract_alignment.py`), which compile the authoritative
  Solidity sources. That requires the npm-pinned OpenZeppelin package and a
  network fetch of the checksum-verified solc binary on first use:

```bash
npm ci --prefix contracts   # pinned @openzeppelin/contracts 5.7.0
```

  `scripts/exec_build.py` provisions solc `0.8.28+commit.7893614a` and
  refuses to compile on SHA-256 mismatch (see `contracts/PROVENANCE.md`).
  Without `node_modules` installed the compilation tests fail with a clear
  setup message — they are never silently skipped.
- `tests/test_flashbots_integration.py` is now an **import-safe, opt-in
  integration test** (ENG-003): collecting it loads no keys and opens no
  connections. The live phases use only the DSS agent testnet wallet
  from the secure store and skip visibly unless
  `DSS_TESTNET_FLASHBOTS_LIVE=1` + `DSS_TESTNET_RPC_URL` are provided;
  submission additionally requires the `DSS_TESTNET_EXEC=1` gate. The
  old version — a module-level script that signed and broadcast at
  import time — is retired; do not reintroduce it.
- `tests/test_multicall3_yieldout.py` depends on a local runtime artifact and
  a live RPC endpoint; it cannot pass on a clean clone and stays excluded
  from CI.

## 5. Regenerating the lock files (after editing the `.in` files)

```bash
python -m pip install pip-tools==7.6.2
python -m piptools compile --generate-hashes --strip-extras \
    -o requirements.txt requirements.in
python -m piptools compile --generate-hashes --strip-extras \
    -o requirements-dev.txt requirements-dev.in
```

Locks were generated with **pip-tools 7.6.2** under CPython 3.11.18;
regenerate with that exact pip-tools version and the same Python major
version to keep marker resolution identical. Byte-identical regeneration
of both committed locks from the committed `.in` files was verified with
pip-tools 7.6.2 on 2026-11-04 (DSS-WO-ENG-001A).

## 6. Runtime configuration notes

- No `.env` file is required for installation, imports or the test suite.
- The system defaults to simulation/paper mode; `EXECUTE_ONCHAIN` remains
  disabled unless explicitly enabled by an operator through configuration
  (see `config.py`). DSS-WO-ENG-002 removed only the retired
  AtomicArbExecutor backend; the active Exec route, its fail-closed gates
  and the EXECUTE_ONCHAIN handling are unchanged.
- `executor/relay_signer.json` (Flashbots authentication identity) is no
  longer tracked in the repository; it is generated locally on first use of
  the relay path and is git-ignored.

## 7. Contract alignment (DSS-WO-ENG-002)

- The active contract is the authoritative `contracts/Exec.sol` sourced from
  `Dannyednut/EVM` at pinned commit `898fe82f29a5695a01278365eb442ab3a26811b5`
  (provenance and per-file blob SHAs: `contracts/PROVENANCE.md`).
- The separate `AtomicArbExecutor` contract is retired: its ABI, generic
  backend, `ATOMIC_EXECUTOR_*` configuration and the old variant's
  `swapFast`/extended selection were removed. Deployment addresses are read
  only from `ARB_EXEC_ADDRESS_*`; missing configuration fails closed.
- To regenerate the committed ABI artifact:
  `npm ci --prefix contracts && python scripts/exec_build.py`
  (`--check` verifies the committed `executor/exec_abi.json` against a fresh
  compilation without writing).

## 8. Shadow-mode collection runbook (DSS-WO-MI-104)

Passive shadow observation only. Every command below uses the EXISTING
runtime; no execution, signing or submission is involved in this phase.
The purpose is real, traceable observation — it is not evidence of profit.

### 8.1 Select and verify the chain (do not rely on the default)

`CHAIN_ID` defaults to `1` (Ethereum mainnet) when the variable is absent.
Configured chains: `1` (Ethereum), `56` (BSC), `137` (Polygon), `8453`
(Base), `42161` (Arbitrum), `324` (zkSync Era), `11155111` (Sepolia).
Set it explicitly in the process environment (or `.env`) and verify the
startup banner line `Chain: <NAME> (id=<CHAIN_ID>)` after starting — the
banner prints the resolved value, not the raw environment string.

### 8.2 RPC configuration and credential safety

Each chain has built-in `DEFAULT_RPCS` (multi-provider failover, WS+HTTP).
Overrides (evaluated for the selected `CHAIN_ID` only):

- `RPC_HTTP` — replaces the primary HTTP endpoint.
- `RPC_WS` — replaces the primary WS endpoint.
- `ALCHEMY_API_KEY` — used by the built-in Alchemy endpoints (default
  `demo`; a real key is required for meaningful throughput).

Credentials belong in the process environment or a `.env` file (`*.env` is
git-ignored). Startup and runtime logs never print RPC URLs or keys; do not
copy endpoint URLs with embedded keys into tickets, screenshots or commits.

### 8.3 Enable shadow recording explicitly

```bash
export SHADOW_VALIDATION_ENABLED=true      # default is true; set it explicitly anyway
export SHADOW_VALIDATION_PATH=             # unset: default is used
export LOG_DIR=./logs                      # recorder default file: $LOG_DIR/shadow_validation.jsonl
export CACHE_DIR=./cache
```

`SHADOW_VALIDATION_PATH` overrides the recorder file path when set. The
recorder is LAZY: it initializes when the first candidate reaches the
recording hook, so a missing/empty log is NEVER proof that no opportunities
exist (see 8.7).

### 8.4 Keep execution disabled explicitly

```bash
export EXECUTE_ONCHAIN=false
```

`config.execution_enabled()` reads the real process environment at call
time; anything other than an explicit `true` is disabled. Enforcement is
layered, not just the banner: the execution router refuses to arm, the
executor refuses submission at its boundaries, the signer refuses to sign
and the relay refuses to broadcast — each re-checks the switch
independently. The startup line
`Execution: DISABLED (EXECUTE_ONCHAIN) — monitoring only` confirms it.

### 8.5 Start the monitor

```bash
python main.py
```

Startup output confirms readiness in this order: the chain banner (8.1);
`Shadow validation: ENABLED — file <path>` (or `DISABLED` if so
configured); `Connected via multi-RPC manager | failover enabled with <N>
providers`; `Latest block: <n>` (live RPC reachable); `Price oracle
ready`; `Bootstrapping graph...`; `<N> detectors active: [...]`;
`Canonical OpportunityEngine ready`; `Execution: DISABLED
(EXECUTE_ONCHAIN) — monitoring only`; `Execution router ready`; `Mempool
pipeline ready`; `Pool crawler ready`; then per-block `[BLOCK] ...`
lines.

### 8.6 Stop cleanly

Send `SIGINT`/`SIGTERM` (Ctrl-C once). The handler drains the crawler and
mempool pipeline, stops the execution router, flushes log file handles and
saves the pool cache, ending with `Shutdown complete ✓`. Shadow evidence
is append-only; a clean stop loses nothing.

### 8.7 Locate outputs and interpret the shadow log

- Shadow detection log: `$LOG_DIR/shadow_validation.jsonl`
  (or `$SHADOW_VALIDATION_PATH`).
- Runtime text log + per-opportunity JSONL: under `$LOG_DIR`
  (`<opp_type>.jsonl`, `detailed/`, `executions.jsonl`, `reports/`).
- Pool cache: `$CACHE_DIR`.

Recorder states — how to distinguish them:

| State | Observable signature |
|---|---|
| Recorder disabled | Startup line `Shadow validation: DISABLED (SHADOW_VALIDATION_ENABLED=false)` |
| Enabled, no candidate reached the hook | Startup line `Shadow validation: ENABLED — file <path> (no collection file yet …)`; no file on disk. This is NOT a zero-candidate result |
| Candidates recorded | File exists and grows; detection records have monotone `seq` |
| Initialization / write failure | `shadow recorder ...` / `shadow recorder append failed` warnings in the runtime log; `append_failures` counter on the recorder |
| Runtime not actually running | No process; no new `recorded_at_unix` values; no `[BLOCK]` lines |

### 8.8 Replay and aggregate the captured collection (explicit paths)

The offline CLIs default to RELATIVE `logs/...` paths, so `--shadow-log`
must ALWAYS be passed explicitly so the tools read the same collection the
monitor wrote. The monitor's recorder resolves its effective collection
path itself: `SHADOW_VALIDATION_PATH` when set to a non-empty value,
otherwise `$LOG_DIR/shadow_validation.jsonl` (the override takes
precedence over the default). Resolve it ONCE with the same precedence —
unset OR empty `SHADOW_VALIDATION_PATH` falls back to the default:

```bash
SHADOW_LOG="${SHADOW_VALIDATION_PATH:-$LOG_DIR/shadow_validation.jsonl}"

python scripts/shadow_replay.py \
    --shadow-log "$SHADOW_LOG" \
    --evaluation-log "$LOG_DIR/shadow_evaluation.jsonl" \
    --cache-dir "$CACHE_DIR" --chain-id "$CHAIN_ID" \
    --catchup-window 1000 --no-rerun-detection

python scripts/shadow_aggregate.py \
    --shadow-log "$SHADOW_LOG" \
    --evaluation-log "$LOG_DIR/shadow_evaluation.jsonl" \
    --json-out "$LOG_DIR/shadow_aggregates.json" \
    --csv-out "$LOG_DIR/shadow_aggregates.csv"
```

The replay opens the source log read-only (byte-identical afterwards) and
writes only the evaluation log; aggregation reads both and writes the
JSON/CSV summaries.

### 8.9 First-run qualification and the initial operational smoke period

Before starting any collection run, verify offline readiness (temporary
files only, no network):

```bash
python scripts/qualify_shadow_readiness.py
```

It verifies: import smoke passes; the shadow test suite passes; the
recorder initializes with the intended configuration in a temporary
directory (and honours the enabled flag and `SHADOW_VALIDATION_PATH`);
detection and lifecycle hooks produce valid records; sequence ordering and
lifecycle reconstruction work across restart; the replay reads the source
log without modifying it; aggregation reads the evaluation records; and
`EXECUTE_ONCHAIN` remains disabled in the environment. Expected final
line: `QUALIFICATION: PASS`.

Once an owner-approved runtime is available, run a SHORT initial smoke
period (15–30 minutes) before any longer observation period and verify:
the process is running; the banner shows the intended chain id; the
recorder line shows the intended path; records appear when candidates
reach the hook (an empty file during the smoke window is recorded as
"no candidates reached the hook", not as "no opportunities"); no
`shadow recorder append failed` warnings appear; the execution-disabled
line is present; and the captured file replays cleanly. Only after that
qualification should the longer observation period begin.

### 8.10 Evidence limitations (keep these with the data)

- An empty or absent shadow log is NOT a zero-candidate or zero-opportunity
  result: the recorder initializes on the first candidate.
- A recorded detection is a candidate observation, not a proven profitable
  opportunity.
- Pool overlap on terminal records is observed heuristic evidence, NOT
  confirmed competitor capture.
- The number of recorded candidates says nothing about complete market
  coverage: the searched-universe denominator is explicitly unavailable.

### 8.11 Git hygiene

`logs/*.log`, `logs/*.jsonl`, `logs/detailed/`, `logs/reports/`, `cache/`
and `*.env` are git-ignored, and the shadow collection artifacts
(`shadow_validation.jsonl`, `shadow_evaluation.jsonl`,
`shadow_aggregates.json`, `shadow_aggregates.csv`) are ignored by basename
at any depth — including inside a custom `LOG_DIR` or
`SHADOW_VALIDATION_PATH` location. If you point any output directory at an
unusual location, add that path to `.gitignore` before starting the run.
