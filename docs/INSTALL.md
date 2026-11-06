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
