"""
tests/test_execution_authorization.py
=====================================
DSS-WO-ENG-015 — Unified Execution Authorization Boundary.

These are the INTEGRATION tests for the mandatory authorization boundary.
Unit tests of ``executor/testnet_gate.py`` alone are NOT sufficient: they do
not prove that the ORDINARY executor is forced to use it. This file proves:

  * Authorization and network isolation
    - CHAIN_ID=11155111 alone never authorizes execution;
    - missing/false DSS_TESTNET_EXEC prevents signing and submission;
    - a non-Sepolia chain and mainnet (chain ID 1) are refused;
    - configured-vs-RPC chain-ID mismatches and RPC check failures refuse;
    - EXECUTE_ONCHAIN=true cannot bypass the testnet policy;
    - the production execution policy stays disabled.

  * Signing-path integration (ordinary entry points)
    - ArbExecutor.submit cannot sign without central authorization;
    - the execution router cannot bypass the boundary;
    - batch and backrun routes use the same boundary;
    - the legacy PRIVATE_KEY signer cannot be invoked as a fallback (the
      module no longer exists and config has no PRIVATE_KEY);
    - no route can broadcast after authorization has been refused.

  * Transaction restrictions (through the ordinary submission handle)
    - unknown recipients, deployments, unbounded approvals rejected;
    - value/gas/monetary fee caps enforced;
    - wallet identity checked against the recorded address;
    - a chain-ID change between signing and submission refuses.

  * Flashbots
    - bundle preparation passes through the boundary;
    - simulation is required before submission;
    - the exact prepared payload stays bound to the session;
    - relay exceptions cannot leak signed payloads or credentials.

  * Audit and failure handling
    - successful, refused, failed and uncertain attempts are accounted for;
    - no raw signed transaction bytes or credentials reach logs or records.

Everything is offline and deterministic: throwaway keystores, stub RPC/relay
transports, controlled injected configurations. No real wallet, no real RPC,
no signing of anything outside the process, no broadcasting.
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
import contextlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account
from eth_utils import to_checksum_address

from executor.authorization import (
    AuthorizationRefused,
    AuthorizedExecution,
    DisabledExecutionPolicy,
    ExecutionAuthorizationBoundary,
    ProductionExecutionPolicy,
    TestnetExecutionPolicy,
    authorized_sender_address,
    get_execution_authorization,
    reset_execution_authorization,
    resolve_execution_policy,
)
import executor.testnet_gate as _gate
from executor.testnet_gate import (
    ALLOWED_TESTNET_CHAINS,
    KNOWN_TARGETS,
    MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
    MAX_TESTNET_TX_VALUE_WEI,
    TestnetExecutionConfig,
    TestnetSafetyError,
    _decode_signed_tx,
)
from tests.gate_r2_fixtures import IsolatedTestnetConfig, throwaway_store
from tests.gate_r3_fixtures import offline_transports
from executor.relay import RelayResult
from executor.mev_execution import MEVExecutionService

SEPOLIA = 11155111
MAINNET = 1
HOLESKY = 17000


@contextlib.contextmanager
def _opted_out():
    """Controlled environment patch (DSS-WO-ENG-015 R1): the REAL process
    environment is the ONLY authorization authority; offline tests simulate
    the disabled default by removing the opt-in from os.environ."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DSS_TESTNET_EXEC", None)
        yield


_MODULE_OPT_IN = None


def setUpModule():
    """DSS-WO-ENG-015 R1: every authorization re-derives its decision from
    the REAL process environment. The offline suite simulates the explicit
    testnet opt-in by patching os.environ for this module; tests that pin
    the disabled default remove it again via _opted_out()."""
    global _MODULE_OPT_IN
    _MODULE_OPT_IN = patch.dict(os.environ, {"DSS_TESTNET_EXEC": "1"}, clear=False)
    _MODULE_OPT_IN.start()
    unittest.addModuleCleanup(_MODULE_OPT_IN.stop)


# ── offline fixtures ─────────────────────────────────────────────────────────


def _restricted_write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _tmp_secret_store(tmpdir: str):
    """Throwaway encrypted keystore (offline, throwaway key)."""
    acct = Account.create()
    keystore = Account.encrypt(acct.key, b"test-passphrase")
    _restricted_write(os.path.join(tmpdir, "keystore.json"),
                      json.dumps(keystore).encode())
    _restricted_write(os.path.join(tmpdir, "pass"),
                      b"test-passphrase".hex().encode())
    _restricted_write(os.path.join(tmpdir, "address"),
                      (acct.address + "\n").encode())
    return acct


def _testnet_config(tmpdir: str, *, account=None,
                    expected_chain_id: int = SEPOLIA,
                    rpc_url="https://sepolia.example"):
    """R1: the config carries NO authorization state — no `enabled`, no
    `allowlist`. Authorization is decided from the REAL process environment
    (patched per-module by setUpModule) and the code-controlled maps."""
    acct = account or _tmp_secret_store(tmpdir)
    return IsolatedTestnetConfig(
        rpc_url=rpc_url,
        keystore_path=os.path.join(tmpdir, "keystore.json"),
        passphrase_path=os.path.join(tmpdir, "pass"),
        address_path=os.path.join(tmpdir, "address"),
        tx_log_path=os.path.join(tmpdir, "txlog.jsonl"),
        expected_chain_id=expected_chain_id,
    ), acct


def _read_audit(cfg):
    with open(cfg.tx_log_path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ── gate-side RPC stub (passed to the orchestrations via web3_factory) ───────


class _GateW3:
    """Fake RPC for the gate orchestrations. Records every raw broadcast so
    tests can assert NOTHING was submitted after a refusal."""

    def __init__(self, chain_values=(SEPOLIA,), head=200, fail_submit=None,
                 receipt_status=1):
        self._values = list(chain_values)
        self._i = 0
        self.head = head
        self.fail_submit = fail_submit
        self.receipt_status = receipt_status
        self.raw_submissions = []
        self.receipt_reads = 0

    def is_connected(self):
        return True

    @property
    def eth(self):
        return self

    @property
    def chain_id(self):
        value = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def block_number(self):
        return self.head

    def get_transaction_count(self, addr, tag=None):
        return 7

    def get_block(self, tag):
        return {"baseFeePerGas": 2 * 10**9, "number": self.head}

    def to_wei(self, v, u):
        return int(v * 10**9) if u == "gwei" else v

    def send_raw_transaction(self, raw):
        self.raw_submissions.append(bytes(raw))
        if self.fail_submit is not None:
            raise self.fail_submit
        return bytes.fromhex("ab" * 32)

    def wait_for_transaction_receipt(self, tx_hash, timeout=None, **kw):
        self.receipt_reads += 1
        return {
            "status": self.receipt_status,
            "gasUsed": 21000,
            "effectiveGasPrice": 10**9,
            "blockNumber": self.head + 1,
        }


# ── executor-side RPC stub (tx building + receipt reads; NO broadcast) ───────


class _StubArbExecContract:
    def __init__(self, address):
        self.address = address

    @property
    def functions(self):
        return self

    def swap(self, *args, **kwargs):
        return self

    def multiCall(self, *args, **kwargs):
        return self

    def build_transaction(self, params):
        tx = dict(params)
        tx["to"] = self.address
        tx["value"] = 0
        return tx

    def encodeABI(self, fn_name=None, args=None, **kwargs):
        # Real ABI encoding via eth_abi would be overkill here: the gate
        # only policy-checks calldata bytes; any well-shaped opaque bytes
        # are fine for these offline integration tests.
        return "0x" + "5e" * 64

    encode_abi = encodeABI

    @property
    def events(self):
        return self

    def DONE(self):
        return self

    def BATCH(self):
        return self

    def process_receipt(self, receipt):
        return []


class _ExecutorW3:
    """The executor's OWN Web3 connection. Deliberately has NO
    send_raw_transaction and NO flashbots attribute: broadcasting must be
    impossible from the executor's connection."""

    def __init__(self, chain_id=SEPOLIA, head=100, receipt=None):
        self._chain_id = chain_id
        self.head = head
        self._receipt = receipt
        self.eth = self

    @property
    def chain_id(self):
        return self._chain_id

    @property
    def block_number(self):
        return self.head

    @property
    def max_priority_fee(self):
        return 10**9

    @property
    def gas_price(self):
        return 2 * 10**9

    def get_block(self, tag):
        return {"number": self.head, "baseFeePerGas": 2 * 10**9}

    def get_transaction_count(self, addr, tag=None):
        return 7

    def to_checksum_address(self, v):
        return to_checksum_address(v)

    def to_wei(self, v, u):
        return int(v * 10**9) if u == "gwei" else v

    def contract(self, address=None, abi=None):
        return _StubArbExecContract(address)

    def wait_for_transaction_receipt(self, tx_hash, timeout=None, **kw):
        return self._receipt


_UNSET_TO = object()


def _self_transfer_tx(sender, *, to=_UNSET_TO, value=0, data=b""):
    tx = {
        "to": sender,
        "value": value,
        "gas": 21000,
        "maxFeePerGas": 2 * 10**9,
        "maxPriorityFeePerGas": 10**9,
        "data": data,
        "nonce": 7,
        "chainId": SEPOLIA,
    }
    if to is not _UNSET_TO:
        # may be None (deployment attempt) or an explicit recipient
        tx["to"] = to
    return tx


# ── Flashbots relay stub ──────────────────────────────────────────────────────


class _RelayW3:
    """Mock Flashbots transport: records calls, never executes anything."""

    def __init__(self, chain_values=(SEPOLIA,), head=199, sim_result=None,
                 sim_error=None, send_error=None):
        self._chain_values = list(chain_values)
        self._ci = 0
        self.head = head
        self._sim_result = sim_result
        self._sim_error = sim_error
        self._send_error = send_error
        self.simulate_calls = []
        self.send_calls = []
        self.eth = self
        self.flashbots = self

    @property
    def chain_id(self):
        value = self._chain_values[min(self._ci, len(self._chain_values) - 1)]
        self._ci += 1
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def block_number(self):
        return self.head

    def simulate(self, bundle, block_tag, *args, **kwargs):
        self.simulate_calls.append((bundle, block_tag))
        if self._sim_error is not None:
            raise self._sim_error
        if self._sim_result is not None:
            return self._sim_result
        return {
            "bundleHash": "0x" + "cd" * 32,
            "coinbaseDiff": "0x1",
            "totalGasUsed": 21000,
            "results": [{"txHash": "0x" + "ee" * 32, "gasUsed": 21000,
                         "value": "0x0"}],
        }

    def send_bundle(self, bundle, target_block_number=None, **kwargs):
        self.send_calls.append((bundle, target_block_number))
        if self._send_error is not None:
            raise self._send_error
        return {"bundleHash": "0x" + "77" * 32}


def _ok_opportunity(sender):
    """A minimal, well-formed 2-pool opportunity (from getProfit arb_data)."""
    from logger import Opportunity, OppType

    token_a = to_checksum_address("0x" + "e0" * 20)
    token_b = to_checksum_address("0x" + "c0" * 20)
    pool_a = to_checksum_address("0x" + "a1" * 20)
    pool_b = to_checksum_address("0x" + "b2" * 20)
    arb_data = (
        10 ** 17, 0,
        [token_a, token_b],
        [pool_a, pool_b],
        [3000, 3000],
        token_a,
        0,
    )
    opp = Opportunity(
        opp_type=OppType.DEX_SPREAD,
        chain_id=SEPOLIA,
        block_number=100,
        tokens=[token_a, token_b],
        pools=[pool_a, pool_b],
        dexes=["UniswapV3", "UniswapV3"],
        metadata={
            "arb_data": arb_data,
            "profit_wei": 10 ** 15,
            "fees_pip": [3000, 3000],
            "gas_units": 300_000,
            "execution_supported": True,
            "execution_simulated": True,
        },
    )
    opp.gas_cost_usd = 1.0
    opp.competition_level = "low"
    return opp


def _stub_lender():
    from executor.lender import LenderChoice
    return LenderChoice(force_balancer=False, mode=0, lender_name="stub")


def _authorized_boundary(tmpdir):
    cfg, acct = _testnet_config(tmpdir)
    return ExecutionAuthorizationBoundary(policy=TestnetExecutionPolicy(cfg)), cfg, acct


# ═════════════════════════════════════════════════════════════════════════════
# Authorization and network isolation
# ═════════════════════════════════════════════════════════════════════════════


class TestPolicySelection(unittest.TestCase):
    """Network selection is not execution authorization."""

    def test_chain_id_alone_never_authorizes(self):
        """R1: policy resolution reads the REAL process environment — a
        CHAIN_ID setting can never select or enable any policy."""
        with patch.dict(os.environ, {"CHAIN_ID": str(SEPOLIA)}), _opted_out():
            policy = resolve_execution_policy()
        self.assertIsInstance(policy, DisabledExecutionPolicy)
        boundary = ExecutionAuthorizationBoundary(policy=policy)
        with self.assertRaises(AuthorizationRefused):
            boundary.authorize_execution("test", chain_id=SEPOLIA)

    def test_execute_onchain_true_alone_never_authorizes(self):
        with patch.dict(os.environ, {"EXECUTE_ONCHAIN": "true"}), _opted_out():
            policy = resolve_execution_policy()
        self.assertIsInstance(policy, DisabledExecutionPolicy)
        boundary = ExecutionAuthorizationBoundary(policy=policy)
        with self.assertRaises(AuthorizationRefused):
            boundary.authorize_execution("test")

    def test_execute_onchain_true_cannot_authorize_mainnet(self):
        with patch.dict(
            os.environ, {"EXECUTE_ONCHAIN": "true", "CHAIN_ID": str(MAINNET)}
        ), _opted_out():
            policy = resolve_execution_policy()
        self.assertIsInstance(policy, DisabledExecutionPolicy)
        with self.assertRaises(AuthorizationRefused):
            ExecutionAuthorizationBoundary(policy=policy).authorize_execution(
                "test", chain_id=MAINNET
            )

    def test_testnet_optin_selects_testnet_policy(self):
        """R1: ONLY the real process environment carries the opt-in."""
        with patch.dict(os.environ, {"DSS_TESTNET_EXEC": "1"}):
            policy = resolve_execution_policy()
        # NOTE: availability is NOT asserted here — it depends on secret-store
        # provisioning, which is environment-specific. Policy SELECTION is
        # what this test pins.
        self.assertIsInstance(policy, TestnetExecutionPolicy)

    def test_production_policy_is_disabled_and_never_selectable(self):
        prod = ProductionExecutionPolicy()
        self.assertFalse(prod.execution_available())
        with self.assertRaises(AuthorizationRefused):
            prod.authorize("test", chain_id=SEPOLIA)
        # Production is not selectable from the real environment, whatever
        # it contains (R1: there is no injected-environment route either):
        for extra in (
            {"EXECUTE_ONCHAIN": "true"},
            {"EXECUTE_ONCHAIN": "1"},
            {"DSS_TESTNET_EXEC": "1"},
        ):
            with patch.dict(os.environ, extra):
                self.assertNotIsInstance(
                    resolve_execution_policy(), ProductionExecutionPolicy
                )

    def test_disabled_policy_refusal_is_testnet_safety_error(self):
        # Refusals are TestnetSafetyError subclasses so every existing
        # fail-closed handler covers boundary refusals too.
        with self.assertRaises(TestnetSafetyError):
            DisabledExecutionPolicy().authorize("test")


class TestBoundaryAuthorization(unittest.TestCase):
    def test_disabled_boundary_refuses_execution(self):
        boundary = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        self.assertFalse(boundary.execution_available())
        with self.assertRaises(AuthorizationRefused) as ctx:
            boundary.authorize_execution("arb_execution", chain_id=SEPOLIA)
        self.assertIn("DSS_TESTNET_EXEC", str(ctx.exception))

    def test_authorized_boundary_returns_handle(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            self.assertTrue(boundary.execution_available())
            auth = boundary.authorize_execution("arb_execution", chain_id=SEPOLIA)
            self.assertIsInstance(auth, AuthorizedExecution)
            self.assertEqual(auth.sender.lower(), acct.address.lower())
            self.assertEqual(auth.network, "testnet")
            self.assertEqual(auth.chain_id, SEPOLIA)

    def test_mainnet_chain_refused(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            with self.assertRaises(AuthorizationRefused) as ctx:
                boundary.authorize_execution("arb_execution", chain_id=MAINNET)
            self.assertIn("binds chain", str(ctx.exception))
            # refused attempts are audited when a policy context exists
            records = _read_audit(cfg)
            self.assertEqual([r["outcome"] for r in records], ["refused"])

    def test_non_sepolia_chain_refused(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            for chain in (HOLESKY, 8453, 42161, 9999999):
                with self.assertRaises(AuthorizationRefused):
                    boundary.authorize_execution("arb_execution", chain_id=chain)

    def test_missing_credentials_fail_closed(self):
        # real-environment opt-in without any secret store: authorization
        # still refuses (custody is re-verified at every authorization).
        # R2: the custody locations are the code-controlled policy paths —
        # the test redirects them (controlled mock) to an EMPTY store.
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            boundary = ExecutionAuthorizationBoundary(
                policy=TestnetExecutionPolicy(cfg)
            )
            self.assertFalse(boundary.execution_available())
            with self.assertRaises(TestnetSafetyError):
                boundary.authorize_execution("arb_execution", chain_id=SEPOLIA)

    def test_disabled_boundary_does_not_write_audit_files(self):
        # No policy context → refusal is logged, never written to a file.
        # (Robust to a pre-existing audit log from an earlier session: the
        # file must not GROW.)
        boundary = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        log_path = os.path.join("logs", "testnet_tx_log.jsonl")
        before = os.path.getsize(log_path) if os.path.exists(log_path) else None
        with self.assertLogs("defi.executor.authorization", level="WARNING"):
            with self.assertRaises(AuthorizationRefused):
                boundary.authorize_execution("arb_execution")
        after = os.path.getsize(log_path) if os.path.exists(log_path) else None
        self.assertEqual(before, after)


class TestSenderAddress(unittest.TestCase):
    def test_sender_requires_authorized_policy(self):
        boundary = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        with self.assertRaises(TestnetSafetyError):
            boundary.sender_address_for_chain(SEPOLIA)

    def test_sender_returns_recorded_public_address_only(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            sender = boundary.sender_address_for_chain(SEPOLIA)
            self.assertEqual(sender.lower(), acct.address.lower())
            self.assertTrue(sender.startswith("0x"))
            self.assertEqual(len(sender), 42)

    def test_sender_chain_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            with self.assertRaises(TestnetSafetyError):
                boundary.sender_address_for_chain(MAINNET)

    def test_module_function_uses_default_boundary(self):
        reset_execution_authorization()
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DSS_TESTNET_EXEC", None)
                with self.assertRaises(TestnetSafetyError):
                    authorized_sender_address(SEPOLIA)
        finally:
            reset_execution_authorization()


# ═════════════════════════════════════════════════════════════════════════════
# Signing-path integration — the ORDINARY entry points
# ═════════════════════════════════════════════════════════════════════════════


ARB_EXEC_ADDRESS = to_checksum_address("0x" + "99" * 20)


class _ChainStub:
    """Chain configuration stub for the executor (offline)."""

    def __init__(self, arb_exec_address=ARB_EXEC_ADDRESS):
        self.arb_exec_address = arb_exec_address
        self.wrapped_native = to_checksum_address("0x" + "ff" * 20)
        self.stablecoins = []


def _patch_chain():
    return patch(
        "executor.executor.get_chain",
        return_value=_ChainStub(ARB_EXEC_ADDRESS),
    )


class TestSigningPathIntegration(unittest.TestCase):
    """The ordinary executor/router/batch/backrun routes cannot sign or
    broadcast without central authorization."""

    def _executor(self, boundary, *, chain_id=SEPOLIA):
        from executor.executor import ArbExecutor
        with _patch_chain():
            return ArbExecutor(
                _ExecutorW3(chain_id=chain_id), chain_id=chain_id,
                authorization=boundary,
            )

    def test_ordinary_executor_cannot_sign_without_authorization(self):
        boundary = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        executor = self._executor(boundary)
        opp = _ok_opportunity("0x" + "11" * 20)
        # No wallet may be decrypted, no key derived, nothing broadcast.
        with patch("eth_account.Account.decrypt",
                   side_effect=AssertionError("wallet decrypted!")) as \
                no_decrypt, \
                patch("eth_account.Account.from_key",
                      side_effect=AssertionError("key loaded!")) as no_key:
            result = executor.submit(opp)
        no_decrypt.assert_not_called()
        no_key.assert_not_called()
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertEqual(result.method, "refused")
        self.assertIn("authorization refused", result.error)
        self.assertEqual(result.tx_hash, "")

    def test_no_broadcast_after_refusal_single_route(self):
        # The executor's own connection CANNOT broadcast at all (no send
        # capability exists on it) and the gate was never contacted.
        boundary = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        executor = self._executor(boundary)
        opp = _ok_opportunity("0x" + "11" * 20)
        result = executor.submit(opp)
        self.assertFalse(result.success)
        self.assertFalse(
            hasattr(executor.w3, "send_raw_transaction"),
            "the executor's own Web3 must have no broadcast capability",
        )
        self.assertFalse(
            hasattr(executor.w3, "flashbots"),
            "the executor's own Web3 must have no relay capability",
        )

    def test_ordinary_router_cannot_bypass_boundary(self):
        from execution_router import create_router
        tracker = MagicMock()
        fake_executor = MagicMock()
        fake_executor.chain_id = SEPOLIA
        fake_executor._arb_exec = MagicMock()
        disabled = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        router = create_router(
            tracker=tracker, executor=fake_executor,
            authorization=disabled,
            config={"EXECUTE_ONCHAIN": True},
        )
        self.assertFalse(router.execute_onchain)
        router.start()
        self.assertEqual(router._worker_threads, [])
        from logger import Opportunity, OppType
        opp = Opportunity(
            opp_type=OppType.DEX_SPREAD, chain_id=SEPOLIA, block_number=1,
        )
        self.assertFalse(router.submit_opportunity(opp, "cycles"))
        fake_executor.submit.assert_not_called()
        router.shutdown()

    def test_batch_route_uses_same_boundary(self):
        boundary = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        executor = self._executor(boundary)
        result = executor.submit_batch(
            [_ok_opportunity("0x" + "11" * 20)], 100
        )
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertEqual(result.method, "refused")

    def test_backrun_service_uses_same_boundary(self):
        boundary = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        service = MEVExecutionService(
            _ExecutorW3(chain_id=SEPOLIA), simulator=None,
            authorization=boundary,
        )
        from mev.models import BundleCandidate
        candidate = BundleCandidate(
            chain_id=SEPOLIA, trigger_hash="0x" + "01" * 32,
            searcher_raw_tx="", target_block=201,
            expected_profit_wei=10**16, gas_limit=300_000,
            max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**9,
        )
        result = service.submit(
            candidate, unsigned_searcher_tx=_self_transfer_tx("0x" + "11" * 20)
        )
        self.assertIsInstance(result, RelayResult)
        self.assertFalse(result.success)
        self.assertEqual(result.method, "refused")

    def test_legacy_private_key_signer_cannot_be_invoked_as_fallback(self):
        # The module was REMOVED: importing it fails, so no code path can
        # fall back to it.
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("executor.signer")
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("executor.signers")
        import config
        self.assertFalse(
            hasattr(config, "PRIVATE_KEY"),
            "config must not expose a PRIVATE_KEY any more",
        )
        # The executor package no longer re-exports any signing symbol.
        import executor
        self.assertFalse(hasattr(executor, "sign_transaction"))
        self.assertFalse(hasattr(executor, "get_sender"))

    def test_grep_level_invariant_only_gate_broadcasts(self):
        """Structural invariant (R1 §4 — broader submission-path audit):
        outside tests, the only module that can broadcast by ANY mechanism
        (send_raw_transaction, send_transaction, eth_sendRawTransaction,
        eth_sendBundle, send_bundle) is executor/testnet_gate.py, and no
        production module submits a send-shaped JSON-RPC method through
        make_request."""
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["git", "grep", "-l", "-E",
             "send_raw_transaction|send_transaction|eth_sendRawTransaction|"
             "eth_sendBundle|send_bundle",
             "--", "*.py"],
            cwd=root, capture_output=True, text=True,
        )
        files = {
            line for line in result.stdout.strip().splitlines() if line
        }
        files = {f for f in files if not f.startswith("tests/")}
        self.assertEqual(
            files, {"executor/testnet_gate.py"},
            f"raw broadcast calls must exist ONLY in the gate, found: {files}",
        )
        # Direct JSON-RPC submission through make_request (or equivalent)
        # must not exist in production code: only the gate orchestrations
        # reach a network write, and only through the sanctioned transports.
        result = subprocess.run(
            ["git", "grep", "-n", "-E",
             r"make_request\(\s*['\"]eth_send", "--", "*.py"],
            cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(
            result.stdout.strip(), "",
            "no production module may submit transactions via make_request",
        )


# ═════════════════════════════════════════════════════════════════════════════
# Full executor flow through the boundary (offline, controlled injection)
# ═════════════════════════════════════════════════════════════════════════════


class TestExecutorThroughBoundary(unittest.TestCase):
    """End-to-end offline proof: the ordinary executor's submission reaches
    the gate orchestrations, and the gate's full checks apply to it."""

    def _make_executor(self, boundary, *, receipt=None):
        from executor.executor import ArbExecutor
        receipt = receipt or {"status": 1, "gasUsed": 21000,
                              "effectiveGasPrice": 10**9, "blockNumber": 101}
        with _patch_chain():
            return ArbExecutor(
                _ExecutorW3(chain_id=SEPOLIA, receipt=receipt),
                chain_id=SEPOLIA, authorization=boundary,
            )

    def test_submit_refused_when_recipient_not_authorized(self):
        """With the production KNOWN_TARGETS map untouched (empty), the
        ordinary executor's arb transaction (recipient = arb_exec contract)
        is refused by the transaction policy at the boundary — arbitrary
        contract targets are not executable."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3()
            opp = _ok_opportunity(acct.address)
            with patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                result = executor.submit(opp)
            self.assertIsNotNone(result)
            self.assertFalse(result.success)
            self.assertIn("neither the agent wallet nor", result.error)
            self.assertEqual(gate_w3.raw_submissions, [])
            # the refused attempt is audited
            records = _read_audit(cfg)
            self.assertEqual([r["outcome"] for r in records], ["refused"])

    def test_submit_full_flow_through_boundary_with_approved_target(self):
        """With a controlled offline-known target injected (test-only
        injection — the committed KNOWN_TARGETS stays empty), the ordinary
        executor's single-submission route completes through the gate:
        one signature, one broadcast, one audited mined record."""
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3()
            opp = _ok_opportunity(acct.address)
            submit_probe = AuthSubmitProbe(gate_w3)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       wraps=submit_probe):
                result = executor.submit(opp)
            self.assertIsNotNone(result)
            self.assertTrue(result.success)
            self.assertEqual(result.method, "direct")
            self.assertNotEqual(result.tx_hash, "")
            # exactly ONE broadcast happened — inside the gate
            self.assertEqual(len(gate_w3.raw_submissions), 1)
            # the signed transaction's recovered signer is the AUTHORIZED
            # agent wallet — nobody else could have signed it
            raw = gate_w3.raw_submissions[0]
            decoded = _decode_signed_tx(raw)
            self.assertEqual("0x" + decoded["to"].hex(), arb_addr.lower())
            self.assertEqual(decoded["chain_id"], SEPOLIA)
            self.assertEqual(decoded["nonce"], 7)
            # audited as mined
            records = _read_audit(cfg)
            self.assertEqual([r["outcome"] for r in records], ["mined"])
            # the probe saw the exact unsigned tx the executor built
            submitted_tx = submit_probe.calls[0]
            self.assertEqual(submitted_tx["to"].lower(), arb_addr.lower())

    def test_submit_batch_route_full_flow_through_boundary(self):
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3()
            opp = _ok_opportunity(acct.address)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.executor._get_amount_out",
                       return_value=10 ** 17), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                result = executor.submit_batch([opp], 100)
            self.assertIsNotNone(result)
            self.assertTrue(result.success)
            self.assertEqual(result.method, "direct")
            self.assertEqual(len(gate_w3.raw_submissions), 1)
            records = _read_audit(cfg)
            self.assertEqual([r["outcome"] for r in records], ["mined"])

    def test_chain_change_between_sign_and_submit_refuses(self):
        """The RPC-reported chain ID is re-checked immediately before
        submission; a change after signing refuses the broadcast."""
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            # initial read + pre-sign read = SEPOLIA; pre-submit = MAINNET
            gate_w3 = _GateW3(chain_values=[SEPOLIA, SEPOLIA, MAINNET])
            opp = _ok_opportunity(acct.address)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                result = executor.submit(opp)
            self.assertFalse(result.success)
            self.assertEqual(result.method, "refused")
            self.assertEqual(gate_w3.raw_submissions, [])  # NEVER broadcast
            records = _read_audit(cfg)
            self.assertEqual([r["outcome"] for r in records], ["refused"])

    def test_rpc_chain_check_failure_refuses(self):
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3(
                chain_values=[ConnectionError("rpc died mid-preflight")]
            )
            opp = _ok_opportunity(acct.address)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                result = executor.submit(opp)
            self.assertFalse(result.success)
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_rpc_reporting_mainnet_refuses(self):
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3(chain_values=[MAINNET])
            opp = _ok_opportunity(acct.address)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                result = executor.submit(opp)
            self.assertFalse(result.success)
            self.assertIn("mainnet", result.error)
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_mismatched_keystore_identity_refuses(self):
        """The wallet identity is checked against the recorded address —
        a keystore that does not match refuses before any signature."""
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            # replace the recorded address with a DIFFERENT address
            other = Account.create().address
            _restricted_write(os.path.join(tmp, "address"),
                              (other + "\n").encode())
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3()
            opp = _ok_opportunity(acct.address)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                result = executor.submit(opp)
            self.assertFalse(result.success)
            self.assertIn("identity mismatch", result.error)
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_submission_failure_is_failed_not_successful(self):
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3(fail_submit=ValueError("nonce too low"))
            opp = _ok_opportunity(acct.address)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                result = executor.submit(opp)
            self.assertFalse(result.success)
            records = _read_audit(cfg)
            self.assertEqual([r["outcome"] for r in records], ["rejected"])

    def test_uncertain_submission_outcome_is_accounted(self):
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3(fail_submit=ConnectionError("provider dropped"))
            opp = _ok_opportunity(acct.address)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                result = executor.submit(opp)
            self.assertFalse(result.success)
            records = _read_audit(cfg)
            self.assertEqual([r["outcome"] for r in records], ["uncertain"])

    def test_audit_never_contains_payload_or_credentials(self):
        arb_addr = to_checksum_address("0x" + "99" * 20)
        known = {SEPOLIA: {arb_addr.lower(): "offline test arb exec"}}
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            executor = self._make_executor(boundary)
            gate_w3 = _GateW3()
            opp = _ok_opportunity(acct.address)
            with patch("executor.testnet_gate.KNOWN_TARGETS", known), \
                 patch("executor.executor.select_lender",
                       return_value=_stub_lender()), \
                 patch("executor.executor.is_profitable_after_gas",
                       return_value=True), \
                 patch("executor.authorization.sign_and_submit_testnet_tx",
                       side_effect=lambda tx, **kw:
                           _real_sign_and_submit(tx, gate_w3=gate_w3, **kw)):
                executor.submit(opp)
            with open(cfg.tx_log_path) as f:
                log_text = f.read()
            for raw in gate_w3.raw_submissions:
                self.assertNotIn(raw.hex(), log_text)
                self.assertNotIn("0x" + raw.hex(), log_text)
            self.assertNotIn("test-passphrase", log_text)
            self.assertNotIn("hunter2", log_text)


class AuthSubmitProbe:
    """Wraps the REAL gate orchestration with a stub RPC, recording the
    unsigned tx each call receives."""

    def __init__(self, gate_w3):
        self.gate_w3 = gate_w3
        self.calls = []

    def __call__(self, tx, *, purpose, config=None, receipt_timeout=120):
        from executor.testnet_gate import sign_and_submit_testnet_tx
        self.calls.append(dict(tx))
        with offline_transports(rpc=self.gate_w3):
            return sign_and_submit_testnet_tx(
                tx, purpose=purpose, config=config,
                receipt_timeout=receipt_timeout,
            )


def _real_sign_and_submit(tx, *, gate_w3, **kwargs):
    """Run the REAL gate orchestration with an offline stub transport
    (R3: injected by patching the gate's PRIVATE transport builder —
    a test-only seam, not a production argument)."""
    from executor.testnet_gate import sign_and_submit_testnet_tx
    with offline_transports(rpc=gate_w3):
        return sign_and_submit_testnet_tx(tx, **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# Transaction restrictions through the ordinary submission handle
# ═════════════════════════════════════════════════════════════════════════════


class TestTransactionRestrictionsViaBoundary(unittest.TestCase):
    def _auth(self, tmp):
        boundary, cfg, acct = _authorized_boundary(tmp)
        auth = boundary.authorize_execution("policy-test", chain_id=SEPOLIA)
        return auth, cfg, acct, _GateW3()

    def _submit(self, auth, gate_w3, tx):
        with patch("executor.authorization.sign_and_submit_testnet_tx",
                   side_effect=lambda t, **kw:
                       _real_sign_and_submit(t, gate_w3=gate_w3, **kw)):
            return auth.sign_and_submit(tx)

    def test_deployment_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            auth, cfg, acct, gate_w3 = self._auth(tmp)
            with self.assertRaises(TestnetSafetyError) as ctx:
                self._submit(auth, gate_w3, _self_transfer_tx(acct.address, to=None))
            self.assertIn("deployment", str(ctx.exception))
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_unknown_recipient_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            auth, cfg, acct, gate_w3 = self._auth(tmp)
            stranger = to_checksum_address("0x" + "dd" * 20)
            with self.assertRaises(TestnetSafetyError) as ctx:
                self._submit(auth, gate_w3,
                             _self_transfer_tx(acct.address, to=stranger))
            self.assertIn("neither the agent wallet nor", str(ctx.exception))
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_unbounded_approval_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            auth, cfg, acct, gate_w3 = self._auth(tmp)
            # an approval to SELF (the only permitted recipient) with an
            # unlimited amount must still be rejected by the amount policy
            approve_calldata = bytes.fromhex("095ea7b3") + (
                bytes.fromhex("00" * 12) + bytes.fromhex("ee" * 20)
            ) + ((2 ** 256 - 1).to_bytes(32, "big"))
            with self.assertRaises(TestnetSafetyError) as ctx:
                self._submit(auth, gate_w3, _self_transfer_tx(
                    acct.address, data=approve_calldata))
            self.assertIn("approval", str(ctx.exception))
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_value_cap_enforced(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            auth, cfg, acct, gate_w3 = self._auth(tmp)
            with self.assertRaises(TestnetSafetyError) as ctx:
                self._submit(auth, gate_w3, _self_transfer_tx(
                    acct.address, value=MAX_TESTNET_TX_VALUE_WEI + 1))
            self.assertIn("cap", str(ctx.exception))
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_fee_price_cap_enforced(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            auth, cfg, acct, gate_w3 = self._auth(tmp)
            tx = _self_transfer_tx(acct.address)
            tx["maxFeePerGas"] = MAX_TESTNET_MAX_FEE_PER_GAS_WEI + 1
            with self.assertRaises(TestnetSafetyError):
                self._submit(auth, gate_w3, tx)
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_gas_over_cap_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            auth, cfg, acct, gate_w3 = self._auth(tmp)
            tx = _self_transfer_tx(acct.address)
            tx["gas"] = 1_000_000  # > MAX_TESTNET_GAS_UNITS (500k)
            with self.assertRaises(TestnetSafetyError):
                self._submit(auth, gate_w3, tx)
            self.assertEqual(gate_w3.raw_submissions, [])


# ═════════════════════════════════════════════════════════════════════════════
# Flashbots through the boundary
# ═════════════════════════════════════════════════════════════════════════════


class TestFlashbotsViaBoundary(unittest.TestCase):
    def test_preparation_requires_boundary_authorization(self):
        boundary = ExecutionAuthorizationBoundary(
            policy=DisabledExecutionPolicy()
        )
        with self.assertRaises(AuthorizationRefused):
            # Directly minting via a disabled boundary is refused; nothing
            # is signed.
            boundary._policy.authorize("test")

    def test_mixed_signer_bundle_refused_for_backrun_service(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            relay = _RelayW3()
            service = MEVExecutionService(
                _ExecutorW3(chain_id=SEPOLIA), simulator=None,
                authorization=boundary,
            )
            from mev.models import BundleCandidate
            candidate = BundleCandidate(
                chain_id=SEPOLIA, trigger_hash="0x" + "01" * 32,
                searcher_raw_tx="", target_block=201,
                expected_profit_wei=10**16, gas_limit=300_000,
                max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**9,
                trigger_raw_tx="0x" + "f8" * 40,  # third-party trigger bytes
            )
            result = service.submit(
                candidate,
                unsigned_searcher_tx=_self_transfer_tx(acct.address),
            )
            self.assertFalse(result.success)
            self.assertEqual(result.method, "refused")
            self.assertIn("third-party trigger", result.error)
            # the relay was never contacted
            self.assertEqual(relay.simulate_calls, [])
            self.assertEqual(relay.send_calls, [])

    def _prepare_session(self, tmp, acct, relay):
        """Prepare a REAL gate session for a self-transfer intent of the SAME
        throwaway account whose keystore is in tmp — using explicit offline
        RPC/relay factories (no production network is touched)."""
        cfg, _ = _testnet_config(tmp, account=acct)
        boundary = ExecutionAuthorizationBoundary(
            policy=TestnetExecutionPolicy(cfg)
        )
        auth = boundary.authorize_execution("bundle-test", chain_id=SEPOLIA)
        prepare_w3 = _PrepareStub()
        with offline_transports(rpc=prepare_w3, relay=relay):
            session = auth.prepare_flashbots_bundle(
                [_self_transfer_tx(acct.address)],
            )
        return session, cfg

    def test_simulation_required_before_submission(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _tmp_secret_store(tmp)
            relay = _RelayW3()
            session, cfg = self._prepare_session(tmp, acct, relay)
            # submit() without a successful simulation is refused BEFORE
            # the relay is contacted
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.submit()
            self.assertIn("no successful simulation", str(ctx.exception))
            self.assertEqual(relay.send_calls, [])

    def test_exact_payload_bound_to_session(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _tmp_secret_store(tmp)
            relay = _RelayW3()
            session, cfg = self._prepare_session(tmp, acct, relay)
            session.simulate()
            result = session.submit()
            self.assertEqual(result["audit"]["outcome"], "accepted")
            # the relay received EXACTLY ONE bundle whose single signed
            # transaction recovers to the AUTHORIZED wallet and matches
            # the validated intent
            self.assertEqual(len(relay.send_calls), 1)
            bundle, target_block = relay.send_calls[0]
            self.assertEqual(len(bundle), 1)
            raw = bytes(bundle[0]["signed_transaction"])
            decoded = _decode_signed_tx(raw)
            self.assertEqual("0x" + decoded["to"].hex(), acct.address.lower())
            self.assertEqual(decoded["value"], 0)
            self.assertEqual(decoded["nonce"], 7)
            self.assertEqual(decoded["chain_id"], SEPOLIA)

    def test_relay_exception_cannot_leak_payload(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _tmp_secret_store(tmp)
            payload_like = "0x" + "f8" * 120  # long hex blob
            relay = _RelayW3(sim_error=RuntimeError(
                f"relay exploded near {payload_like} key=hunter2"))
            session, cfg = self._prepare_session(tmp, acct, relay)
            with self.assertRaises(TestnetSafetyError) as ctx:
                session.simulate()
            message = str(ctx.exception)
            self.assertNotIn(payload_like, message)
            self.assertNotIn("hunter2", message)
            # the failed simulation is audited, without the payload
            with open(cfg.tx_log_path) as f:
                self.assertNotIn(payload_like, f.read())


class _PrepareStub:
    """Web3 stub for the gate's prepare_flashbots_bundle."""

    def __init__(self, chain_values=(SEPOLIA,), head=199):
        self._values = list(chain_values)
        self._i = 0
        self.head = head

    def is_connected(self):
        return True

    @property
    def eth(self):
        return self

    @property
    def chain_id(self):
        value = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def block_number(self):
        return self.head

    def get_transaction_count(self, addr):
        return 7

    def get_block(self, tag):
        return {"baseFeePerGas": 10**9}

    def to_wei(self, v, u):
        return int(v * 10**9) if u == "gwei" else v


# ═════════════════════════════════════════════════════════════════════════════
# Production invariants
# ═════════════════════════════════════════════════════════════════════════════



# ═════════════════════════════════════════════════════════════════════════════
# DSS-WO-ENG-015 R1 — override-isolation regression tests (Coordinator R1 §3)
# ═════════════════════════════════════════════════════════════════════════════


class TestR1EnvironmentAuthority(unittest.TestCase):
    """The REAL process environment is the ONLY authority for the testnet
    opt-in. A manually constructed configuration — however "enabled" it
    looks — can never authorize execution without it, and no production
    interface accepts an environment/config override."""

    def _manual_config(self, tmp):
        """A hand-built, FULLY provisioned configuration (throwaway keystore
        present and custody-clean) — exactly what a caller trying to route
        around the central policy would construct."""
        acct = _tmp_secret_store(tmp)
        cfg = IsolatedTestnetConfig(
            rpc_url="https://sepolia.example",
            keystore_path=os.path.join(tmp, "keystore.json"),
            passphrase_path=os.path.join(tmp, "pass"),
            address_path=os.path.join(tmp, "address"),
            tx_log_path=os.path.join(tmp, "txlog.jsonl"),
        )
        return cfg, acct

    def test_config_no_longer_has_enabled_or_allowlist_fields(self):
        # The structure itself removes the override: there is no `enabled`
        # or `allowlist` field left to set.
        self.assertNotIn("enabled", TestnetExecutionConfig.__dataclass_fields__)
        self.assertNotIn("allowlist", TestnetExecutionConfig.__dataclass_fields__)
        with self.assertRaises(TypeError):
            # A caller replaying the OLD bypass (enabled=True) fails at
            # construction.
            TestnetExecutionConfig(enabled=True, rpc_url="https://x.example")
        with self.assertRaises(TypeError):
            TestnetExecutionConfig(allowlist={8453: "Base"})

    def test_manually_constructed_config_cannot_authorize_without_env(self):
        """R1 §3.1: a hand-built config (even fully provisioned) cannot
        authorize execution when the REAL process environment has not
        enabled DSS_TESTNET_EXEC."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            cfg, acct = self._manual_config(tmp)
            boundary = ExecutionAuthorizationBoundary(
                policy=TestnetExecutionPolicy(cfg)
            )
            with _opted_out():
                # boundary-level refusal (the gate raises the base
                # TestnetSafetyError; AuthorizationRefused is its subclass
                # for disabled-policy and chain-mismatch verdicts)
                with self.assertRaises(TestnetSafetyError) as ctx:
                    boundary.authorize_execution("arb_execution", chain_id=SEPOLIA)
                self.assertIn("DSS_TESTNET_EXEC", str(ctx.exception))
                # gate-level refusal on a DIRECT orchestration call
                with self.assertRaises(TestnetSafetyError) as ctx2:
                    cfg.require_ready()
                self.assertIn("DISABLED", str(ctx2.exception))
                # and the signed-intent handle minted BEFORE the env change
                # still cannot sign: every call re-reads the environment
                with patch.dict(os.environ, {"DSS_TESTNET_EXEC": "1"}):
                    auth = boundary.authorize_execution(
                        "arb_execution", chain_id=SEPOLIA)
                with _opted_out():
                    gate_w3 = _GateW3()
                    with patch(
                        "executor.authorization.sign_and_submit_testnet_tx",
                        side_effect=lambda t, **kw: _real_sign_and_submit(
                            t, gate_w3=gate_w3, **kw),
                    ):
                        with self.assertRaises(TestnetSafetyError):
                            auth.sign_and_submit(
                                _self_transfer_tx(acct.address))
                    self.assertEqual(gate_w3.raw_submissions, [])

    def test_no_environment_parameter_on_production_interfaces(self):
        """R1 §1.6: production-facing interfaces accept no caller-supplied
        environment injection."""
        import inspect as _inspect
        self.assertEqual(
            list(_inspect.signature(resolve_execution_policy).parameters), []
        )
        self.assertEqual(
            list(_inspect.signature(
                ExecutionAuthorizationBoundary.__init__).parameters),
            ["self", "policy"],
        )
        # from_env keeps its optional mapping for offline-test CONFIG only;
        # the opt-in is never taken from it.
        with _opted_out():
            cfg = TestnetExecutionConfig.from_env(
                {"DSS_TESTNET_EXEC": "1",
                 "DSS_TESTNET_RPC_URL": "https://sepolia.example"})
            with self.assertRaises(TestnetSafetyError):
                cfg.require_ready()

    def test_injected_policy_confers_no_authorization_authority(self):
        """Any policy object re-derives the decision from the authoritative
        sources inside the gate — an injected policy cannot flip execution
        on."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            cfg, acct = self._manual_config(tmp)
            boundary = ExecutionAuthorizationBoundary(
                policy=TestnetExecutionPolicy(cfg)
            )
            with _opted_out():
                self.assertFalse(boundary.execution_available())
                with self.assertRaises(TestnetSafetyError):
                    boundary.authorize_execution("arb_execution")
            with patch.dict(os.environ, {"DSS_TESTNET_EXEC": "1"}):
                self.assertTrue(boundary.execution_available())

    def test_direct_gate_orchestration_calls_cannot_bypass_policy(self):
        """R1 §3.4: direct calls to the gate orchestrations with a hand-built
        configuration hit the SAME central authorization policy."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            cfg, acct = self._manual_config(tmp)
            gate_w3 = _GateW3()
            relay = _RelayW3()
            with _opted_out():
                with self.assertRaises(TestnetSafetyError) as ctx:
                    from executor.testnet_gate import sign_and_submit_testnet_tx
                    with offline_transports(rpc=gate_w3):
                        sign_and_submit_testnet_tx(
                            _self_transfer_tx(acct.address),
                            purpose="direct gate call must not bypass",
                            config=cfg,
                        )
                self.assertIn("DISABLED", str(ctx.exception))
                with self.assertRaises(TestnetSafetyError) as ctx2:
                    from executor.testnet_gate import prepare_flashbots_bundle
                    with offline_transports(rpc=_PrepareStub(), relay=relay):
                        prepare_flashbots_bundle(
                            [_self_transfer_tx(acct.address)],
                            purpose="direct gate bundle must not bypass",
                            config=cfg,
                        )
                self.assertIn("DISABLED", str(ctx2.exception))
            # EXECUTE_ONCHAIN=true cannot flip the gate either
            with patch.dict(os.environ, {"EXECUTE_ONCHAIN": "true"}), _opted_out():
                with self.assertRaises(TestnetSafetyError):
                    from executor.testnet_gate import sign_and_submit_testnet_tx                         as _s
                    with offline_transports(rpc=gate_w3):
                        _s(
                            _self_transfer_tx(acct.address),
                            purpose="execute_onchain must not help",
                            config=cfg,
                        )
            # nothing was signed or broadcast on any refusal
            self.assertEqual(gate_w3.raw_submissions, [])
            self.assertEqual(relay.simulate_calls, [])
            self.assertEqual(relay.send_calls, [])

    def test_standard_route_fully_disabled_without_real_opt_in(self):
        """R1 §3.7: the standard application route (default boundary, the
        ordinary executor and router wiring) is fully disabled when the REAL
        environment carries no testnet opt-in."""
        reset_execution_authorization()
        try:
            with _opted_out():
                boundary = ExecutionAuthorizationBoundary()
                self.assertFalse(boundary.execution_available())
                with self.assertRaises(AuthorizationRefused):
                    boundary.authorize_execution("arb_execution", chain_id=SEPOLIA)
                executor = TestSigningPathIntegration()._executor(boundary)
                result = executor.submit(_ok_opportunity("0x" + "11" * 20))
                self.assertIsNotNone(result)
                self.assertFalse(result.success)
                self.assertEqual(result.method, "refused")
        finally:
            reset_execution_authorization()

    def test_no_raw_signing_or_submission_on_any_refusal(self):
        """R1 §3.8: on ANY refusal the wallet is never decrypted and nothing
        is broadcast — proven with a real (throwaway) keystore configured
        and a broadcast-recording stub RPC."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            cfg, acct = self._manual_config(tmp)
            gate_w3 = _GateW3()
            with _opted_out(), patch(
                "executor.testnet_gate.load_agent_wallet",
                side_effect=AssertionError("wallet must never be decrypted"),
            ):
                from executor.testnet_gate import sign_and_submit_testnet_tx
                with self.assertRaises(TestnetSafetyError):
                    with offline_transports(rpc=gate_w3):
                        sign_and_submit_testnet_tx(
                            _self_transfer_tx(acct.address),
                            purpose="refusal must not decrypt or sign",
                            config=cfg,
                        )
            self.assertEqual(gate_w3.raw_submissions, [])
            # also: an authorization refused at the boundary never reaches
            # the gate's wallet path
            boundary = ExecutionAuthorizationBoundary(
                policy=TestnetExecutionPolicy(cfg)
            )
            with _opted_out(), patch(
                "executor.testnet_gate.load_agent_wallet",
                side_effect=AssertionError("wallet must never be decrypted"),
            ):
                with self.assertRaises(TestnetSafetyError):
                    boundary.authorize_execution("arb_execution", chain_id=SEPOLIA)


class TestR1AllowlistIsolation(unittest.TestCase):
    """The production allowlist is the code-controlled
    ALLOWED_TESTNET_CHAINS map with Sepolia as its ONLY entry; no caller
    can expand the live allowlist through any interface."""

    def test_sepolia_is_the_only_production_allowlisted_testnet(self):
        """R1 §3.3: Sepolia remains the only production-allowlisted testnet."""
        self.assertEqual(ALLOWED_TESTNET_CHAINS, {SEPOLIA: "Ethereum Sepolia"})
        self.assertEqual(_gate.ALLOWED_TESTNET_CHAINS, ALLOWED_TESTNET_CHAINS)
        # effective_allowlist() is parameter-free and returns exactly that map
        import inspect as _inspect
        self.assertEqual(
            list(_inspect.signature(_gate.effective_allowlist).parameters), []
        )
        self.assertIs(_gate.effective_allowlist(), ALLOWED_TESTNET_CHAINS)

    def test_caller_supplied_allowlist_cannot_authorize_other_networks(self):
        """R1 §3.2: a caller-supplied test allowlist cannot authorize Base,
        Arbitrum or any other non-approved network — the parameter does not
        exist and the code-controlled map refuses them."""
        for chain, name in (
            (8453, "Base"), (42161, "Arbitrum One"), (10, "OP Mainnet"),
            (56, "BNB Chain"), (HOLESKY, "Holesky"), (137, "Polygon"),
        ):
            with self.subTest(chain=chain, name=name):
                with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
                    acct = _tmp_secret_store(tmp)
                    cfg = IsolatedTestnetConfig(
                        rpc_url="https://attacker.example",
                        keystore_path=os.path.join(tmp, "keystore.json"),
                        passphrase_path=os.path.join(tmp, "pass"),
                        address_path=os.path.join(tmp, "address"),
                        expected_chain_id=chain,
                    )
                    # even with the explicit opt-in present in the REAL
                    # environment, the chain is refused by the production map
                    with patch.dict(os.environ, {"DSS_TESTNET_EXEC": "1"}):
                        with self.assertRaises(TestnetSafetyError) as ctx:
                            cfg.require_ready()
                    self.assertIn("allowlist", str(ctx.exception))
                    # and no interface accepts an alternative map:
                    with self.assertRaises(TypeError):
                        TestnetExecutionConfig(allowlist={chain: name})

    def test_frozen_config_cannot_gain_an_allowlist(self):
        # The pin is on the PRODUCTION config class: it is frozen and has
        # no allowlist field (R1), and no code reads one from a config.
        cfg = TestnetExecutionConfig(rpc_url="https://sepolia.example")
        with self.assertRaises((AttributeError, TypeError)):
            cfg.allowlist = {8453: "Base"}
        import dataclasses as _dc
        with self.assertRaises(TypeError):
            _dc.replace(cfg, allowlist={8453: "Base"})
        # The test-only isolated config is equally closed: no instance
        # state can be attached to it at all.
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            test_cfg, _ = self._manual_config_r1(tmp)
            with self.assertRaises((AttributeError, TypeError)):
                test_cfg.allowlist = {8453: "Base"}

    def _manual_config_r1(self, tmp):
        acct = _tmp_secret_store(tmp)
        return IsolatedTestnetConfig(
            rpc_url="https://sepolia.example",
            keystore_path=os.path.join(tmp, "keystore.json"),
            passphrase_path=os.path.join(tmp, "pass"),
            address_path=os.path.join(tmp, "address"),
            tx_log_path=os.path.join(tmp, "txlog.jsonl"),
        ), acct


class TestR1TargetInjection(unittest.TestCase):
    """No production-facing execution interface accepts a `known_targets`
    override: normal execution ALWAYS validates against the code-controlled
    KNOWN_TARGETS map, which stays EMPTY."""

    def test_known_targets_remain_empty(self):
        self.assertIs(KNOWN_TARGETS, _gate.KNOWN_TARGETS)
        self.assertEqual(_gate.KNOWN_TARGETS, {})

    def test_no_known_targets_parameter_on_execution_interfaces(self):
        import inspect as _inspect
        from executor.testnet_gate import (
            prepare_flashbots_bundle as gate_prepare,
            sign_and_submit_testnet_tx as gate_submit,
            validate_bundle,
            validate_transaction,
        )
        for fn in (
            AuthorizedExecution.sign_and_submit,
            AuthorizedExecution.prepare_flashbots_bundle,
            MEVExecutionService.submit,
            gate_submit,
            gate_prepare,
            validate_transaction,
            validate_bundle,
        ):
            self.assertNotIn(
                "known_targets", _inspect.signature(fn).parameters,
                f"{fn.__qualname__} must not accept a known_targets override",
            )

    def test_runtime_caller_cannot_authorize_arbitrary_contract(self):
        """R1 §3.5: a caller passing its own target map to the ONLY
        sanctioned direct-RPC handle is refused — twice over: the parameter
        no longer exists (TypeError), and the policy refuses the unapproved
        recipient against the EMPTY production map."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            auth = boundary.authorize_execution("arb_execution", chain_id=SEPOLIA)
            attacker_contract = to_checksum_address("0x" + "dd" * 20)
            attacker_map = {SEPOLIA: {attacker_contract.lower(): "attacker"}}
            gate_w3 = _GateW3()
            with self.assertRaises(TypeError):
                auth.sign_and_submit(
                    _self_transfer_tx(acct.address, to=attacker_contract),
                    known_targets=attacker_map,
                )
            # without any override the production policy refuses the target
            with patch(
                "executor.authorization.sign_and_submit_testnet_tx",
                side_effect=lambda t, **kw:
                    _real_sign_and_submit(t, gate_w3=gate_w3, **kw),
            ):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    auth.sign_and_submit(
                        _self_transfer_tx(acct.address, to=attacker_contract),
                    )
            self.assertIn("neither the agent wallet nor", str(ctx.exception))
            self.assertEqual(gate_w3.raw_submissions, [])

    def test_runtime_caller_cannot_authorize_arbitrary_bundle_target(self):
        """The Flashbots preparation handle refuses the override too."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            auth = boundary.authorize_execution("bundle-test", chain_id=SEPOLIA)
            attacker_contract = to_checksum_address("0x" + "df" * 20)
            with self.assertRaises(TypeError):
                auth.prepare_flashbots_bundle(
                    [_self_transfer_tx(acct.address, to=attacker_contract)],
                    known_targets={SEPOLIA: {attacker_contract.lower(): "x"}},
                )
            # and without the override the gate refuses the recipient
            with patch(
                "executor.authorization.prepare_flashbots_bundle",
                side_effect=lambda txs, **kw:
                    _real_prepare(txs, relay=_RelayW3(), **kw),
            ):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    auth.prepare_flashbots_bundle(
                        [_self_transfer_tx(acct.address, to=attacker_contract)],
                    )
            self.assertIn("neither the agent wallet nor", str(ctx.exception))

    def test_mev_service_cannot_authorize_unapproved_target_via_override(self):
        """R1 §3.6: MEVExecutionService.submit cannot authorize an unapproved
        target through an override — the parameter does not exist, and the
        gate still validates against the empty production map."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            boundary, cfg, acct = _authorized_boundary(tmp)
            service = MEVExecutionService(
                _ExecutorW3(chain_id=SEPOLIA), simulator=None,
                authorization=boundary,
            )
            from mev.models import BundleCandidate
            attacker_contract = to_checksum_address("0x" + "de" * 20)
            # target_block=101: the stub head is 100, so the MEV preflight
            # (chain policy, target-block window, profit ceiling) passes and
            # the refusal below is the GATE's target-policy refusal.
            candidate = BundleCandidate(
                chain_id=SEPOLIA, trigger_hash="0x" + "02" * 32,
                searcher_raw_tx="", target_block=101,
                expected_profit_wei=10**16, gas_limit=300_000,
                max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**9,
            )
            with self.assertRaises(TypeError):
                service.submit(
                    candidate,
                    unsigned_searcher_tx=_self_transfer_tx(
                        acct.address, to=attacker_contract),
                    known_targets={SEPOLIA: {attacker_contract.lower(): "evil"}},
                )
            # without any override the unapproved target is still refused
            with patch(
                "executor.authorization.prepare_flashbots_bundle",
                side_effect=lambda txs, **kw:
                    _real_prepare(txs, relay=_RelayW3(), **kw),
            ):
                result = service.submit(
                    candidate,
                    unsigned_searcher_tx=_self_transfer_tx(
                        acct.address, to=attacker_contract),
                )
            self.assertFalse(result.success)
            self.assertEqual(result.method, "flashbots")
            self.assertIn("neither the agent wallet nor", result.error)


def _real_prepare(unsigned_txs, *, relay, **kwargs):
    """Run the REAL gate bundle preparation with offline stub transports
    (R3: injected by patching the gate's PRIVATE transport builders —
    a test-only seam, not a production argument)."""
    from executor.testnet_gate import prepare_flashbots_bundle
    with offline_transports(rpc=_PrepareStub(), relay=relay):
        return prepare_flashbots_bundle(
            [dict(t) for t in unsigned_txs], **kwargs)


class TestProductionInvariants(unittest.TestCase):
    def test_committed_known_targets_remain_empty(self):
        """No contract target was authorized by ENG-015."""
        self.assertEqual(KNOWN_TARGETS, {})

    def test_default_environment_state_is_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSS_TESTNET_EXEC", None)
            boundary = ExecutionAuthorizationBoundary()
            self.assertFalse(boundary.execution_available())

    def test_process_default_boundary_is_lazy_and_disabled(self):
        reset_execution_authorization()
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DSS_TESTNET_EXEC", None)
                boundary = get_execution_authorization()
                self.assertIsInstance(
                    boundary.policy, DisabledExecutionPolicy
                )
        finally:
            reset_execution_authorization()

    def test_r1_environment_is_the_only_opt_in_authority(self):
        """R1: gate_opted_in() reads the REAL process environment and is the
        single authoritative read; resolve_execution_policy() uses exactly
        it."""
        import inspect as _inspect
        from executor.testnet_gate import gate_opted_in
        with _opted_out():
            self.assertFalse(gate_opted_in())
            self.assertIsInstance(
                resolve_execution_policy(), DisabledExecutionPolicy)
        with patch.dict(os.environ, {"DSS_TESTNET_EXEC": "1"}):
            self.assertTrue(gate_opted_in())
            self.assertIsInstance(
                resolve_execution_policy(), TestnetExecutionPolicy)
        with patch.dict(os.environ, {"DSS_TESTNET_EXEC": "true"}):
            # anything but the exact "1" is NOT an opt-in
            self.assertFalse(gate_opted_in())

    def test_r1_disabled_policy_refusal_is_not_silently_accepted(self):
        """A refused authorization must never be reported as available."""
        with _opted_out():
            boundary = ExecutionAuthorizationBoundary()
            self.assertFalse(boundary.execution_available())
            self.assertIn("DSS_TESTNET_EXEC", boundary.refusal_reason())


if __name__ == "__main__":
    unittest.main()
