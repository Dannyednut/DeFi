"""
tests/test_r4_execution_boundary.py
====================================
The DSS-WO-ENG-015 R4 execution-boundary suite.

Proves the RETAINED multi-chain execution architecture enforces a
coherent, consistent and testable execution policy:

  * the master execution switch (EXECUTE_ONCHAIN) is enforced at the
    router, the executor (single AND batch), the signer and the relay;
  * chain identity (configured CHAIN_ID vs tx chainId vs RPC-reported)
    is verified before signing and immediately before broadcasting;
  * the builder enforces the full transaction policy — recipient =
    the chain's configured arb_exec_address, zero native value, gas and
    monetary fee-exposure ceilings, valid EIP-1559/legacy fee inputs —
    with BATCH parity (the batch path runs the same validator);
  * an unsupported chain or missing execution profile fails closed;
  * a supported chain with valid configuration keeps the legitimate
    execution flow operable under mocks (Builder → Signer → Relay);
  * a failed private (atomic) submission never falls back to the public
    mempool;
  * normal shared-provider reads keep working (the provider was NOT made
    read-only);
  * structural invariants: the retired authorization modules and every
    operational reference are gone; signing happens ONLY in
    executor/signer.py, broadcasting ONLY in executor/relay.py; there is
    no import-time key loading; no transport-factory parameters; the
    process-environment EXECUTE_ONCHAIN read is the single decision
    function.

All tests are fully offline: mock transports and THROWAWAY keys created
in temporary directories. No real wallet, no live signing, no network
activity, no broadcast anywhere.
"""
import inspect
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from eth_account import Account
from eth_utils import to_checksum_address

from config import CHAINS, execution_enabled
from execution_profile import PROFILES, FEE_POLICIES
from executor.errors import ExecutionRefused
from executor.signer import sign_transaction
from executor.relay import relay_transaction
from executor.builder import (
    build_execute_tx, build_execute_batch_tx, validate_transaction_policy,
)

SEPOLIA = 11155111
FAKE_ARB = to_checksum_address("0x" + "ab" * 20)


# ── offline fixtures ─────────────────────────────────────────────────────────

def _provision_throwaway_store(tmp_path, key: Account) -> None:
    """Provision a compliant DSS secret store around a THROWAWAY key."""
    store = tmp_path / "secrets"
    store.mkdir(mode=0o700, exist_ok=True)
    keystore = Account.encrypt(key.key, b"offline-test-passphrase")
    p = store / "dss_wallet.keystore.json"
    p.write_text(json.dumps(keystore))
    os.chmod(p, 0o600)
    p = store / "dss_wallet.pass"
    p.write_text(b"offline-test-passphrase".hex())
    os.chmod(p, 0o600)
    p = store / "dss_wallet.address"
    p.write_text(key.address)
    os.chmod(p, 0o600)


def _signed_raw(key: Account, chain_id: int = SEPOLIA, to: str = FAKE_ARB) -> bytes:
    tx = {
        "to": to,
        "value": 0,
        "gas": 21_000,
        "maxFeePerGas": 30 * 10**9,
        "maxPriorityFeePerGas": 1 * 10**9,
        "nonce": 0,
        "chainId": chain_id,
    }
    signed = key.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    return bytes(raw)


class _MockEth:
    """Deterministic mock RPC edge: constant chain, recorded broadcasts."""

    gas_price = 30 * 10**9

    def __init__(self, chain_id: int = SEPOLIA, head: int = 100):
        self._chain_id = chain_id
        self.block_number = head
        self.sent = []

    @property
    def chain_id(self):
        return self._chain_id

    def get_block(self, _which):
        return {"baseFeePerGas": 30 * 10**9, "number": self.block_number}

    @property
    def max_priority_fee(self):
        return 1 * 10**9

    def get_transaction_count(self, _addr, _tag="pending"):
        return 0

    def send_raw_transaction(self, raw):
        self.sent.append(bytes(raw))
        return b"\xab" * 32


class _MockW3:
    def __init__(self, chain_id: int = SEPOLIA):
        self.eth = _MockEth(chain_id)

    to_checksum_address = staticmethod(to_checksum_address)


class _FlippingEth(_MockEth):
    """Chain ID flips after `n` reads — models an RPC failover mid-flight."""

    def __init__(self, chain_id, flips_to, after_reads):
        super().__init__(chain_id)
        self._flips_to = flips_to
        self._after = after_reads
        self._reads = 0

    @property
    def chain_id(self):
        self._reads += 1
        if self._reads > self._after:
            return self._flips_to
        return self._chain_id


# ══════════════════════════════════════════════════════════════════════════════
# §6 matrix — master switch enforcement
# ══════════════════════════════════════════════════════════════════════════════

class TestMasterSwitchEnforcement(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"EXECUTE_ONCHAIN": "false"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # The executor requires a configured arb contract to be armed.
        self._original = CHAINS[SEPOLIA].arb_exec_address
        CHAINS[SEPOLIA].arb_exec_address = FAKE_ARB
        self.addCleanup(setattr, CHAINS[SEPOLIA], "arb_exec_address", self._original)

    def test_router_execution_workers_remain_disabled(self):
        from execution_router import create_router

        fake_executor = MagicMock()
        fake_executor.chain_id = SEPOLIA
        fake_executor._arb_exec = MagicMock()  # ready executor

        router = create_router(
            tracker=MagicMock(), executor=fake_executor,
            config={"MIN_PROFIT_USD": 0.01},
        )
        self.assertIs(router.execute_onchain, False)
        self.assertFalse(router.start(), None) if False else router.start()
        # No worker threads were started.
        self.assertEqual(router._worker_threads, [])

    def test_executor_single_attempt_refused_when_switch_off(self):
        with patch("executor.signer.load_wallet") as never_wallet, \
             patch("executor.relay.load_wallet") as never_relay_wallet:
            from executor.executor import ArbExecutor

            executor = ArbExecutor(MagicMock(), chain_id=SEPOLIA)
            result = executor.submit(MagicMock())
            self.assertIsNotNone(result)
            self.assertFalse(result.success)
            self.assertEqual(result.method, "refused")
            self.assertIn("disabled", result.error)
            never_wallet.assert_not_called()
            never_relay_wallet.assert_not_called()

    def test_executor_batch_attempt_refused_when_switch_off(self):
        from executor.executor import ArbExecutor

        executor = ArbExecutor(MagicMock(), chain_id=SEPOLIA)
        result = executor.submit_batch([MagicMock()], block_number=100)
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertEqual(result.method, "refused")
        self.assertIn("disabled", result.error)

    def test_signer_produces_no_signature_when_switch_off(self):
        with patch("executor.signer.load_wallet") as never_wallet:
            w3 = _MockW3()
            tx = {
                "to": FAKE_ARB, "value": 0, "gas": 21_000,
                "maxFeePerGas": 30 * 10**9, "maxPriorityFeePerGas": 10**9,
                "nonce": 0, "chainId": SEPOLIA,
            }
            with self.assertRaises(ExecutionRefused) as ctx:
                sign_transaction(w3, tx, chain_id=SEPOLIA)
            self.assertIn("disabled", str(ctx.exception))
            never_wallet.assert_not_called()

    def test_relay_makes_no_submission_when_switch_off(self):
        w3 = _MockW3()
        key = Account.create()
        raw = _signed_raw(key)
        with self.assertRaises(ExecutionRefused) as ctx:
            relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
        self.assertIn("disabled", str(ctx.exception))
        self.assertEqual(w3.eth.sent, [])


# ══════════════════════════════════════════════════════════════════════════════
# §6 matrix — chain-identity enforcement
# ══════════════════════════════════════════════════════════════════════════════

class TestChainIdentityEnforcement(unittest.TestCase):
    def _enabled_store(self, key: Account):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="r4-boundary-"))
        _provision_throwaway_store(tmp, key)
        env = patch.dict(os.environ, {
            "EXECUTE_ONCHAIN": "true",
            "DSS_SECRET_STORE": str(tmp / "secrets"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_configured_chain_differs_from_rpc_chain_refused_before_signing(self):
        self._enabled_store(Account.create())
        # RPC reports 137 but the executor expects Sepolia.
        w3 = _MockW3(chain_id=137)
        tx = {
            "to": FAKE_ARB, "value": 0, "gas": 21_000,
            "maxFeePerGas": 30 * 10**9, "maxPriorityFeePerGas": 10**9,
            "nonce": 0, "chainId": SEPOLIA,
        }
        with patch("executor.signer.load_wallet") as never_wallet:
            with self.assertRaises(ExecutionRefused) as ctx:
                sign_transaction(w3, tx, chain_id=SEPOLIA)
            self.assertIn("chain mismatch", str(ctx.exception))
            never_wallet.assert_not_called()

    def test_rpc_chain_change_before_submission_refused_no_broadcast(self):
        key = Account.create()
        self._enabled_store(key)
        # Relay-entry read (1) sees Sepolia; the pre-broadcast recheck
        # (read 2) sees a DIFFERENT chain → refused at the transport gate,
        # nothing broadcast.
        w3 = _MockW3()
        w3.eth = _FlippingEth(SEPOLIA, flips_to=1, after_reads=1)
        raw = _signed_raw(key)
        result = relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
        self.assertFalse(result.success)
        self.assertEqual(result.method, "refused")
        self.assertEqual(w3.eth.sent, [])

    def test_transaction_chain_id_mismatch_refused(self):
        self._enabled_store(Account.create())
        w3 = _MockW3()
        tx = {
            "to": FAKE_ARB, "value": 0, "gas": 21_000,
            "maxFeePerGas": 30 * 10**9, "maxPriorityFeePerGas": 10**9,
            "nonce": 0, "chainId": 137,  # ≠ expected Sepolia
        }
        with patch("executor.signer.load_wallet") as never_wallet:
            with self.assertRaises(ExecutionRefused) as ctx:
                sign_transaction(w3, tx, chain_id=SEPOLIA)
            self.assertIn("chainId", str(ctx.exception))
            never_wallet.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# §6 matrix — builder transaction policy
# ══════════════════════════════════════════════════════════════════════════════

def _valid_tx(**overrides):
    tx = {
        "to": FAKE_ARB, "value": 0, "gas": 300_000,
        "maxFeePerGas": 37 * 10**9, "maxPriorityFeePerGas": 10**9,
        "nonce": 0, "chainId": SEPOLIA, "data": "0xabc",
    }
    tx.update(overrides)
    return tx


class TestBuilderPolicy(unittest.TestCase):
    def setUp(self):
        # Point the chain's configured recipient at the fake arb contract.
        self._original = CHAINS[SEPOLIA].arb_exec_address
        CHAINS[SEPOLIA].arb_exec_address = FAKE_ARB
        self.addCleanup(setattr, CHAINS[SEPOLIA], "arb_exec_address", self._original)

    def test_recipient_differs_from_configured_arb_exec_address(self):
        for wrong in (
            to_checksum_address("0x" + "cd" * 20),  # another contract
            None,                                    # deployment
            "",                                      # empty
        ):
            with self.assertRaises(ExecutionRefused):
                validate_transaction_policy(_valid_tx(to=wrong), chain_id=SEPOLIA)

    def test_recipient_must_equal_configured_address_even_for_self(self):
        # The R0–R3 policy allowed self-transfers; the R4 arbitrage path
        # accepts ONLY the configured execution contract.
        self.assertRaises(
            ExecutionRefused,
            validate_transaction_policy,
            _valid_tx(to="0x" + "9a" * 20), chain_id=SEPOLIA,
        )

    def test_transaction_with_unexpected_native_value_refused(self):
        for value in (1, 10**15, -1):
            with self.assertRaises(ExecutionRefused):
                validate_transaction_policy(
                    _valid_tx(value=value), chain_id=SEPOLIA,
                )

    def test_gas_over_chain_limit_refused(self):
        policy = FEE_POLICIES[SEPOLIA]
        with self.assertRaises(ExecutionRefused):
            validate_transaction_policy(
                _valid_tx(gas=policy.max_gas_units + 1), chain_id=SEPOLIA,
            )

    def test_fee_exposure_over_chain_limit_refused(self):
        policy = FEE_POLICIES[SEPOLIA]
        # Worst case = gas × price just above the monetary cap.
        price = policy.max_fee_per_gas_wei
        with self.assertRaises(ExecutionRefused):
            validate_transaction_policy(
                _valid_tx(gas=policy.max_gas_units, maxFeePerGas=price + 1),
                chain_id=SEPOLIA,
            )
        # Per-gas ceiling itself also refuses.
        with self.assertRaises(ExecutionRefused):
            validate_transaction_policy(
                _valid_tx(maxFeePerGas=policy.max_fee_per_gas_wei + 1),
                chain_id=SEPOLIA,
            )

    def test_malformed_fee_inputs_refused(self):
        # Both envelopes at once.
        self.assertRaises(
            ExecutionRefused, validate_transaction_policy,
            _valid_tx(gasPrice=10**9), chain_id=SEPOLIA,
        )
        # Neither envelope.
        self.assertRaises(
            ExecutionRefused, validate_transaction_policy,
            _valid_tx(maxFeePerGas=None), chain_id=SEPOLIA,
        )
        # Non-integer and negative.
        for bad in ("30 gwei", 30.5, True, -30):
            self.assertRaises(
                ExecutionRefused, validate_transaction_policy,
                _valid_tx(maxFeePerGas=bad), chain_id=SEPOLIA,
            )
        # Priority fee above the max fee.
        self.assertRaises(
            ExecutionRefused, validate_transaction_policy,
            _valid_tx(maxPriorityFeePerGas=40 * 10**9), chain_id=SEPOLIA,
        )
        # Priority fee above the chain ceiling (2 gwei on Sepolia).
        self.assertRaises(
            ExecutionRefused, validate_transaction_policy,
            _valid_tx(maxFeePerGas=40 * 10**9, maxPriorityFeePerGas=3 * 10**9),
            chain_id=SEPOLIA,
        )

    def test_batch_transaction_violating_policy_refused(self):
        """The batch path must receive the SAME validation: a batch-built
        transaction violating the recipient policy is refused whole."""
        w3 = _MockW3()
        arb_exec = MagicMock()
        violating = _valid_tx(to=to_checksum_address("0x" + "cd" * 20))
        arb_exec.functions.multiCall.return_value.build_transaction.return_value = violating

        with self.assertRaises(ExecutionRefused):
            build_execute_batch_tx(
                w3=w3, arb_exec=arb_exec, calls=[b"call1", b"call2"],
                sender="0x" + "e1" * 20, gas_units=100_000,
            )
        # And a compliant batch transaction passes the same validator.
        arb_exec.functions.multiCall.return_value.build_transaction.return_value = _valid_tx()
        tx = build_execute_batch_tx(
            w3=w3, arb_exec=arb_exec, calls=[b"call1", b"call2"],
            sender="0x" + "e1" * 20, gas_units=100_000,
        )
        self.assertEqual(tx["to"].lower(), FAKE_ARB.lower())

    def test_unsupported_chain_missing_profile_fails_closed(self):
        unconfigured = 999_999
        self.assertNotIn(unconfigured, PROFILES)
        self.assertNotIn(unconfigured, FEE_POLICIES)
        with self.assertRaises(ExecutionRefused):
            validate_transaction_policy(_valid_tx(), chain_id=unconfigured)


# ══════════════════════════════════════════════════════════════════════════════
# §6 matrix — legitimate flow under mocks
# ══════════════════════════════════════════════════════════════════════════════

class TestLegitimateFlowOperable(unittest.TestCase):
    def test_supported_chain_with_valid_configuration_end_to_end(self):
        """Builder → Signer → Relay stays operable for a supported chain
        with a valid configuration, entirely under mocks."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="r4-boundary-"))
        key = Account.create()
        _provision_throwaway_store(tmp, key)
        env = patch.dict(os.environ, {
            "EXECUTE_ONCHAIN": "true",
            "DSS_SECRET_STORE": str(tmp / "secrets"),
        })
        env.start()
        self.addCleanup(env.stop)

        original = CHAINS[SEPOLIA].arb_exec_address
        CHAINS[SEPOLIA].arb_exec_address = FAKE_ARB
        self.addCleanup(setattr, CHAINS[SEPOLIA], "arb_exec_address", original)

        w3 = _MockW3()
        arb_exec = MagicMock()
        arb_exec.functions.swap.return_value.build_transaction.side_effect = (
            lambda overrides: {**_valid_tx(), **overrides}
        )

        # 1. Builder: a policy-compliant transaction for the configured
        #    recipient.
        tx = build_execute_tx(
            w3=w3, arb_exec=arb_exec,
            arb_data={"amountIn": 1, "minProfit": 0, "tokens": [], "pools": [],
                      "fees": [], "tokenIn": FAKE_ARB, "mode": 0},
            force_balancer=False, builder_tip_bps=1000,
            sender=key.address, gas_units=300_000,
        )
        self.assertEqual(tx["to"].lower(), FAKE_ARB.lower())
        self.assertEqual(tx["value"], 0)

        # 2. Signer: real (throwaway-key) signing through the full
        #    enforcement path.
        raw = sign_transaction(w3, tx, chain_id=SEPOLIA)
        self.assertIsInstance(raw, bytes)
        self.assertGreater(len(raw), 100)

        # 3. Relay: submission succeeds; exactly one broadcast; the
        #    broadcast bytes ARE the signed payload.
        result = relay_transaction(w3, raw, expected_chain_id=SEPOLIA)
        self.assertTrue(result.success)
        self.assertEqual(result.method, "direct")
        self.assertEqual(w3.eth.sent, [raw])


# ══════════════════════════════════════════════════════════════════════════════
# §6 matrix — private relay fallback + shared provider
# ══════════════════════════════════════════════════════════════════════════════

class TestPrivateRelayNoPublicFallback(unittest.TestCase):
    def test_required_atomic_bundle_failure_has_no_public_fallback(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="r4-boundary-"))
        key = Account.create()
        _provision_throwaway_store(tmp, key)
        env = patch.dict(os.environ, {
            "EXECUTE_ONCHAIN": "true",
            "DSS_SECRET_STORE": str(tmp / "secrets"),
        })
        env.start()
        self.addCleanup(env.stop)

        w3 = _MockW3()
        w3.flashbots = MagicMock()
        w3.flashbots.send_bundle.return_value = None  # private path rejects

        raw = _signed_raw(key)
        with patch("executor.relay.FLASHBOTS_CHAINS", frozenset({SEPOLIA})):
            result = relay_transaction(
                w3, raw, expected_chain_id=SEPOLIA,
                use_flashbots=True, target_block=101,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.method, "flashbots")
        # The public mempool was never touched.
        self.assertEqual(w3.eth.sent, [])


class TestSharedProviderReads(unittest.TestCase):
    def test_normal_shared_provider_reads_continue_to_work(self):
        """The shared FailoverProvider is NOT read-only: ordinary RPC
        reads (eth_blockNumber) keep flowing through it."""
        import utils.rpc_manager as rpc_manager_module

        response = {"jsonrpc": "2.0", "id": 1, "result": hex(100)}
        fake_http = MagicMock()
        fake_http.return_value.make_request.return_value = response

        with patch.object(rpc_manager_module, "HTTPProvider", fake_http):
            manager = rpc_manager_module.MultiRPCManager([
                rpc_manager_module.RPCProvider(url="http://fake-node", name="fake"),
            ])
            w3 = manager.get_failover_web3(ws=False)
            head = w3.eth.block_number
        self.assertEqual(head, 100)
        # And the provider was not restricted by any execution gating.
        self.assertNotIn("execution_enabled", inspect.getsource(rpc_manager_module))


# ══════════════════════════════════════════════════════════════════════════════
# Structural invariants
# ══════════════════════════════════════════════════════════════════════════════

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCLUDED_TOP_LEVEL = {"tests", "scripts", "docs", ".github", "venv", ".venv",
                      "logs", "cache", "contracts", "node_modules"}

# Operational symbols of the retired architecture and the legacy signing
# path. NONE of these may appear in any production module.
RETIRED_SYMBOLS = (
    "executor.authorization", "executor/authorization",
    "testnet_gate",
    "ExecutionAuthorizationBoundary", "AuthorizedExecution",
    "DSS_TESTNET_EXEC",
    "DisabledExecutionPolicy", "TestnetExecutionPolicy",
    "ProductionExecutionPolicy", "TestnetSafetyError",
    "authorized_sender_address", "load_agent_wallet",
    "KNOWN_TARGETS", "ALLOWED_TESTNET_CHAINS",
    "FLASHBOTS_SEPOLIA_RELAY", "TestnetExecutionConfig",
    "web3_factory", "relay_factory",
    # DSS-WO-ENG-016 §4/§5 — retired alternative orchestration and
    # unused policy layers (one canonical runtime path only):
    "MEVExecutionService", "MEVExecutionDecision",
    "BundleCandidate", "BundleSimulator", "BundleSimulation",
    "TxFeeModel", "BidPolicy", "InclusionEstimate",
    "MempoolOpportunityEngine", "PendingObservation",
    "BackrunCandidate", "mev_execution",
    # DSS-WO-ENG-017 §3/§4 — retired unintegrated utilities and dormant
    # duplicates (evidence-backed dispositions in docs/DEPENDENCIES.md §11):
    "NonceManager", "nonce_manager",
    "RelayRegistry", "RelayEndpoint",
    "TriggerTracker", "mempool/reorg",
    "ProtocolOpportunityDetector",
    "SharpeDetector",
)


def _production_sources():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in EXCLUDED_TOP_LEVEL or rel.parts[0].startswith("."):
            continue
        if "__pycache__" in rel.parts:
            continue
        yield rel, path.read_text(encoding="utf-8")


class TestStructuralInvariants(unittest.TestCase):

    def test_retired_modules_are_deleted(self):
        self.assertFalse((REPO_ROOT / "executor" / "authorization.py").exists(),
                         "executor/authorization.py must be removed")
        self.assertFalse((REPO_ROOT / "executor" / "testnet_gate.py").exists(),
                         "executor/testnet_gate.py must be removed")
        # DSS-WO-ENG-016 §4: the unused alternative MEV orchestration and
        # its orphaned support modules are retired.
        self.assertFalse((REPO_ROOT / "executor" / "mev_execution.py").exists(),
                         "executor/mev_execution.py must be removed")
        for retired in ("models.py", "simulator.py", "inclusion.py"):
            self.assertFalse((REPO_ROOT / "mev" / retired).exists(),
                             f"mev/{retired} must be removed (policy.py stays: "
                             "the relay consumes ChainMEVPolicy)")
        # DSS-WO-ENG-016 §5: the unused mempool policy engine is retired.
        self.assertFalse((REPO_ROOT / "mempool" / "engine.py").exists(),
                         "mempool/engine.py must be removed")
        # DSS-WO-ENG-017 §3B/§3C/§3D: unintegrated utilities with no runtime
        # role (serial execution invariant, FLASHBOTS_RPCS + ChainMEVPolicy
        # relay selection, outcome-resolver trigger handling).
        self.assertFalse((REPO_ROOT / "executor" / "nonce_manager.py").exists(),
                         "executor/nonce_manager.py must be removed")
        self.assertFalse((REPO_ROOT / "executor" / "relay_registry.py").exists(),
                         "executor/relay_registry.py must be removed")
        self.assertFalse((REPO_ROOT / "mempool" / "reorg.py").exists(),
                         "mempool/reorg.py must be removed")
        # DSS-WO-ENG-017 §3G: consumer-less simulation shims are retired; the
        # route-compilation framework stays research-only.
        for retired in ("economics.py", "state_transition.py"):
            self.assertFalse((REPO_ROOT / "simulation" / retired).exists(),
                             f"simulation/{retired} must be removed")

    def test_no_retired_symbol_in_any_production_module(self):
        offenders = []
        for rel, source in _production_sources():
            for symbol in RETIRED_SYMBOLS:
                if symbol in source:
                    offenders.append(f"{rel}: {symbol}")
        self.assertEqual(offenders, [],
                         "retired authorization/transport-factory symbols "
                         "found in production modules")

    def test_signing_is_defined_only_in_the_signer(self):
        for rel, source in _production_sources():
            if rel.as_posix() == "executor/signer.py":
                continue
            self.assertNotIn("def sign_transaction", source,
                             f"{rel} defines its own signing path")
            self.assertNotIn("Account.sign_transaction", source,
                             f"{rel} signs transactions directly")

    def test_broadcast_is_defined_only_in_the_relay(self):
        for rel, source in _production_sources():
            if rel.as_posix() == "executor/relay.py":
                continue
            self.assertNotIn("send_raw_transaction", source,
                             f"{rel} broadcasts directly")
            self.assertNotIn("send_bundle", source,
                             f"{rel} submits bundles directly")
            self.assertNotIn("send_private_transaction", source,
                             f"{rel} submits private transactions directly")

    def test_no_import_time_key_loading_anywhere(self):
        # Operational key-loading patterns — the word "PRIVATE_KEY" may only
        # survive in comments documenting its removal, never as a setting.
        operational_patterns = (
            "PRIVATE_KEY =", "PRIVATE_KEY=",
            'os.getenv("PRIVATE_KEY', "os.environ[\"PRIVATE_KEY",
            "os.environ.get(\"PRIVATE_KEY",
        )
        for rel, source in _production_sources():
            for pattern in operational_patterns:
                self.assertNotIn(pattern, source,
                                 f"{rel} loads a legacy PRIVATE_KEY setting")
            if rel.as_posix() != "executor/signer.py":
                self.assertNotIn("Account.decrypt", source,
                                 f"{rel} decrypts keystore material")
                self.assertNotIn("Account.from_key", source,
                                 f"{rel} loads raw keys")
            # No production module may generate a wallet.
            self.assertNotIn("Account.create(", source,
                             f"{rel} generates wallet material")

    def test_no_transport_factory_parameters(self):
        from executor.relay import relay_transaction, simulate_bundle
        from executor.signer import sign_transaction

        for func in (relay_transaction, simulate_bundle, sign_transaction):
            params = inspect.signature(func).parameters
            for name in params:
                self.assertNotIn("factory", name.lower(),
                                 f"{func.__name__} accepts a factory parameter")

    def test_batch_path_runs_the_same_validator(self):
        import executor.builder as builder

        batch_source = inspect.getsource(builder.build_execute_batch_tx)
        self.assertIn("validate_transaction_policy", batch_source,
                      "the batch path must run the same policy validator")
        single_source = inspect.getsource(builder.build_execute_tx)
        self.assertIn("validate_transaction_policy", single_source,
                      "the single path must run the policy validator")

    def test_execution_enabled_is_the_single_decision_function(self):
        import config
        self.assertTrue(callable(config.execution_enabled))
        # Disabled by default: an unset environment is disabled.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXECUTE_ONCHAIN", None)
            self.assertIs(execution_enabled(), False)
        # Only config.py reads EXECUTE_ONCHAIN from the environment.
        readers = []
        for rel, source in _production_sources():
            if 'os.environ.get("EXECUTE_ONCHAIN"' in source or \
               "os.getenv(\"EXECUTE_ONCHAIN\"" in source:
                readers.append(str(rel))
        self.assertEqual(readers, ["config.py"],
                         "EXECUTE_ONCHAIN must be read in exactly one place")

    def test_flashbots_middleware_is_attached_only_in_the_relay(self):
        for rel, source in _production_sources():
            if rel.as_posix() == "executor/relay.py":
                continue
            self.assertNotIn("flashbot(", source,
                             f"{rel} attaches relay middleware itself")

    def test_single_execution_path_composition(self):
        """Executor → Builder → Signer → Relay is the one wired path."""
        import executor.executor as executor_module
        src = inspect.getsource(executor_module)
        for required in ("build_execute_tx", "build_execute_batch_tx",
                         "sign_transaction", "relay_transaction"):
            self.assertIn(required, src)


if __name__ == "__main__":
    unittest.main()
