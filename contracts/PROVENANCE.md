# Contract source provenance (DSS-WO-ENG-002)

The **only** active execution contract in this repository is:

```
Dannyednut/EVM @ 898fe82f29a5695a01278365eb442ab3a26811b5
commit subject: "Exec : optimized Flash, Helper: optimized v3"
```

The files below are byte-for-byte copies of that pinned commit. Their git
blob SHAs (SHA-1 over `blob <len>\0<content>`) are recorded so any copy can
be re-verified offline:

| File | Git blob SHA at pinned commit |
| --- | --- |
| contracts/Exec.sol | `36dfb77737b11a4a4ea39a6c09246364f6f0d3d4` |
| contracts/libraries/Helper.sol | `9da89f8168f88a6b7ae8e099abbc85c883d59104` |
| contracts/interfaces/IUniswapV2Pair.sol | `de351e0f88b5c79432d17612b8ede17cf02e3cbb` |
| contracts/interfaces/IUniswapV3Pool.sol | `0ac01112198f2e44fd16a48ae840e3344c855a45` |
| contracts/interfaces/IQuoter.sol | `06f3f69b91e9aae9c6b2dae4a2a1be1b5f09bb65` |
| contracts/interfaces/IWETH.sol | `aa87824b41ee241a807fd004d3149ffc86b6e35d` |
| contracts/interfaces/IBalancerVault.sol | `9c6cdbf33d1d218d62a0763cb39e162f1c83425b` |

`tests/test_exec_contract_alignment.py` recomputes the blob hash of every
local copy and asserts equality with this table, so provenance is verified
mechanically, not by trust.

## Retired artifacts

- The separate `AtomicArbExecutor` contract was **retired by the Principal**
  and must not be restored. Its ABI (`executor/atomic_executor_abi.json`),
  its generic `executeAtomic` backend, the `ATOMIC_EXECUTOR_*` environment
  configuration and the Phase 12 contract-existence tests were removed.
- The former in-repo `contracts/Exec.sol` variant (5-argument constructor
  `(_w, _q, _b, _aave, initialOperator)`, `swapFast`, `flashLoanAave`,
  `executeAtomic`, `multiCallAtomic`, operator allow-lists) was **replaced**
  by the authoritative three-argument `constructor(_w, _q, _b)` version
  above. The two variants were never combined; the old variant's
  `swapFast`/`executeAtomic` entrypoints do not exist in the authoritative
  contract and the Python integration no longer references them.

## Compilation toolchain

- Compiler: `solc 0.8.28+commit.7893614a` (pinned; SHA-256
  `9a0fb7e0db2c0641dbae1c5cc645dc686820c83af516226abb1c0a2f76636f25` for the
  official `linux-amd64` build, verified after download).
- Dependencies: `@openzeppelin/contracts@5.7.0` exactly, via the committed
  `contracts/package-lock.json`.
- ABI artifact: `executor/exec_abi.json` is generated from these sources by
  `scripts/exec_build.py` and its conformance is enforced by tests.
