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
  --ignore=tests/test_flashbots_integration.py \
  --ignore=tests/test_multicall3_yieldout.py \
  -p no:cacheprovider -ra
```

- Expected result: **22 failed, 86 passed, 1 error** — the pre-existing
  baseline failures are classified in `docs/DEPENDENCIES.md` §6 and are being
  addressed by later work orders. They are not hidden by CI.
- `tests/test_flashbots_integration.py` is a module-level manual script that
  **signs and broadcasts a real transaction**; it must never be run by
  automation (see the failure register).
- `tests/test_multicall3_yieldout.py` depends on a local runtime artifact and
  a live RPC endpoint; it cannot pass on a clean clone.
- For the full baseline comparison, run without the second `--ignore`
  (expected: 23 failed, 86 passed, 1 error).

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
  (see `config.py`). This work order did not change any execution behavior.
- `executor/relay_signer.json` (Flashbots authentication identity) is no
  longer tracked in the repository; it is generated locally on first use of
  the relay path and is git-ignored.
