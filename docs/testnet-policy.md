# DSS Testnet Policy (DSS-WO-ENG-003, Workstream D)

Status: ACTIVE — enforced by `executor/testnet_gate.py`.
The gate is **disabled by default** (`DSS_TESTNET_EXEC` unset/empty). Nothing
in this repository signs or submits a transaction unless every condition in
this document is met.

## 1. Non-negotiable boundaries

| Rule | Enforcement |
| --- | --- |
| `EXECUTE_ONCHAIN` remains the production guard and stays `false` | `config.py`; the gate does not read or modify it |
| Ethereum mainnet (chain ID 1) is always refused | hard-coded in `check_chain_id` (not configurable) |
| Unknown chains fail closed | `ALLOWED_TESTNET_CHAINS` is the only authority |
| Gate disabled unless `DSS_TESTNET_EXEC=1` | `TestnetExecutionConfig.from_env` |
| Missing RPC / keystore / passphrase / address fails closed | `TestnetExecutionConfig.require_ready` |
| Chain ID verified before signing AND before submission | `sign_and_submit_testnet_tx` steps 2 and 6 |
| No arbitrary EOA transfers | `validate_transaction`: recipient = agent wallet or reviewed target |
| No unlimited token approvals | approval selectors decoded; amount capped at `MAX_TOKEN_APPROVAL_AMOUNT` |
| No unverified contract targets | `KNOWN_TARGETS` empty by default; entries require review below |
| No contract deployments | empty `to` rejected outright |
| Value / gas caps per transaction | `MAX_TESTNET_TX_VALUE_WEI` (0.001 native), `MAX_TESTNET_GAS_UNITS` (500k) |
| Every transaction audited | `logs/testnet_tx_log.jsonl` (gitignored); credentials never recorded |
| Agent wallet is DSS-owned, testnet-only | encrypted keystore in the DSS secret store; identity bound at signing |

## 2. Allowlisted networks

| Network | Chain ID | Added | Review record |
| --- | --- | --- | --- |
| Ethereum Sepolia | 11155111 | 2026-11-04 (WO-ENG-003 D) | RPC verified live on 2026-11-04: `eth_chainId` = 11155111, block height 12,062,812 (publicnode RPC); Flashbots Sepolia relay documented by Flashbots (docs updated 2025-12-16) |

**Adding a network** (e.g. Hoodi) requires, before `ALLOWED_TESTNET_CHAINS`
is touched: a documented review of (a) current network health, (b) RPC
availability and reported chain ID, (c) required contract/protocol
deployments for the strategies involved, and (d) Coordinator sign-off.

## 3. Wallet custody

The agent wallet is generated inside the DSS runtime from OS CSPRNG entropy;
no mnemonic exists. The raw key is encrypted at creation into a web3
secret-storage v3 keystore (scrypt) with a machine-generated 256-bit
passphrase. Both files (0600) live in the DSS secret store (0700) next to
the existing agent credentials. The key never touches disk unencrypted, is
never printed, and never enters Git, `.env`, CI secrets, reports or
conversation transcripts.

## 4. First-use protocol

1. Principal funds the agent wallet's public address with Sepolia ETH.
2. DSS performs read-only balance/network checks.
3. Before the FIRST state-changing transaction or deployment, DSS reports
   target, purpose, estimated gas, maximum test-asset expenditure and
   intended transaction behaviour to the Coordinator for review.
4. Only after Coordinator approval is `DSS_TESTNET_EXEC=1` set in the agent
   runtime (never in CI, never committed) for that specific operation.
5. Simulation (`eth_callBundle`) always precedes submission; they are
   separate operations (see `tests/test_flashbots_integration.py`, which is
   separately gated and never runs in CI).

## 5. Audit

Each submitted transaction appends a JSONL record with: timestamp, purpose,
chain ID, target, function selector, estimated gas, actual gas used,
transaction hash, outcome. No private credentials are ever recorded.
