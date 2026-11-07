"""DSS-WO-ENG-015 R2 — runtime security policy and custody isolation.

The R2 correction freezes the RUNTIME SECURITY POLICY: no caller can
weaken the testnet execution policy by constructing a custom
configuration, injecting a policy object or touching the environment.

Pinned here (all offline, throwaway wallets, controlled mocks — no real
wallet, no live RPC, no signing or broadcast outside the gate stubs):

  §2 the hard expenditure caps are immutable code constants:
     - a manually constructed configuration cannot carry (or raise) any
       limit — value, gas, fee-per-gas, priority fee or aggregate fee;
     - no production interface accepts a limit override; both the direct-
       RPC and Flashbots orchestrations enforce the SAME constants.
  §3 wallet custody, audit destination and the reviewed relay are
     code-controlled:
     - a caller cannot substitute keystore/passphrase/recorded-address
       paths, cannot redirect or suppress the audit log, and cannot
       redirect the Flashbots session to another relay;
     - an audit-path failure never turns a refusal into an apparent
       success.
  §4 operational configuration (RPC endpoint, expected chain) is
     strictly separated from security policy; the ``policy`` injection
     point on ``ExecutionAuthorizationBoundary`` cannot produce an
     authorized handle with weakened gate settings; direct calls to the
     gate orchestrations cannot bypass the central policy.
  §5 the standard application route stays disabled without the REAL
     process environment's explicit testnet opt-in.

Structural invariants additionally pin the interfaces so future code
cannot silently re-introduce caller-configurable security policy or
alternative broadcast paths.
"""
import contextlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executor.testnet_gate as _gate

from eth_account import Account

from tests.gate_r2_fixtures import IsolatedTestnetConfig, throwaway_store

from executor.authorization import (
    ExecutionAuthorizationBoundary,
    ExecutionPolicy,
    AuthorizedExecution,
    TestnetExecutionPolicy,
    get_execution_authorization,
    reset_execution_authorization,
    resolve_execution_policy,
)
from executor.testnet_gate import (
    FLASHBOTS_SEPOLIA_RELAY,
    KNOWN_TARGETS,
    MAX_TESTNET_GAS_UNITS,
    MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
    MAX_TESTNET_MAX_PRIORITY_FEE_WEI,
    MAX_TESTNET_TX_FEE_WEI,
    MAX_TESTNET_TX_VALUE_WEI,
    SECURITY_POLICY,
    TestnetExecutionConfig,
    TestnetSafetyError,
    TestnetSecurityPolicy,
    load_agent_wallet,
    prepare_flashbots_bundle,
    sign_and_submit_testnet_tx,
    validate_bundle,
    validate_bundle_fee_exposure,
    validate_fee_exposure,
    validate_transaction,
    verify_secret_store,
)

SEPOLIA = 11155111

# Enormous values a hostile caller would inject to weaken policy.
HUGE_WEI = 10**22                 # 10,000 native units — far above every cap
HUGE_GAS = 30_000_000             # full block gas — far above the cap
HUGE_FEE_PRICE = 10_000 * 10**9   # 10,000 gwei — far above the cap


@contextlib.contextmanager
def _opted_out():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DSS_TESTNET_EXEC", None)
        yield


_MODULE_OPT_IN = None


def setUpModule():
    """Simulate the explicit testnet opt-in for this module ONLY via the
    REAL process environment (the single authorization authority); tests
    that pin the disabled default remove it again via _opted_out()."""
    global _MODULE_OPT_IN
    _MODULE_OPT_IN = patch.dict(os.environ, {"DSS_TESTNET_EXEC": "1"}, clear=False)
    _MODULE_OPT_IN.start()
    unittest.addModuleCleanup(_MODULE_OPT_IN.stop)


def _restricted_write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _throwaway_keystore(tmpdir: str):
    """Create a throwaway encrypted keystore (offline, throwaway key)."""
    acct = Account.create()
    keystore = Account.encrypt(acct.key, b"r2-test-passphrase")
    _restricted_write(os.path.join(tmpdir, "keystore.json"),
                      json.dumps(keystore).encode())
    _restricted_write(os.path.join(tmpdir, "pass"),
                      b"r2-test-passphrase".hex().encode())
    _restricted_write(os.path.join(tmpdir, "address"),
                      (acct.address + "\n").encode())
    return acct


# ── RPC / relay stubs (never touch a network; record everything) ────────────


class _GateW3:
    """Fake direct-RPC transport. Records every raw broadcast so tests can
    assert NOTHING was submitted on a refusal."""

    def __init__(self, chain_values=(SEPOLIA,), head=200):
        self._values = list(chain_values)
        self._i = 0
        self.head = head
        self.raw_submissions = []

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

    @property
    def gas_price(self):
        return 2 * 10**9

    def to_wei(self, v, u):
        return int(v * 10**9) if u == "gwei" else v

    def send_raw_transaction(self, raw):
        self.raw_submissions.append(bytes(raw))
        return b"\xab" * 32

    def wait_for_transaction_receipt(self, tx_hash, timeout=None, **kw):
        return {"status": 1, "gasUsed": 21000}


class _PrepareW3:
    """Web3 stub for prepare_flashbots_bundle (offline)."""

    def __init__(self, chain_values=(SEPOLIA,), head=99):
        self._values = list(chain_values)
        self._i = 0
        self._head = head

    def is_connected(self):
        return True

    @property
    def eth(self):
        return self

    @property
    def chain_id(self):
        value = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return value

    @property
    def block_number(self):
        return self._head

    @property
    def gas_price(self):
        return 10**9

    def get_transaction_count(self, addr):
        return 7

    def get_block(self, tag):
        return {"baseFeePerGas": 10**9}

    def to_wei(self, v, u):
        return int(v * 10**9) if u == "gwei" else v


class _RelayStub:
    """Mock Flashbots transport: records calls, never executes anything."""

    def __init__(self, chain_values=(SEPOLIA,), head=99):
        self.eth = _PrepareW3(chain_values=chain_values, head=head)
        self.flashbots = self
        self.simulate_calls = []
        self.send_calls = []
        self.received_relay_urls = []

    def simulate(self, bundle, block_tag, *args, **kwargs):
        self.simulate_calls.append((bundle, block_tag))
        return {
            "bundleHash": "0x" + "cd" * 32,
            "totalGasUsed": 21000 * len(bundle),
            "results": [
                {"txHash": "0x" + "ee" * 32, "gasUsed": 21000, "value": "0x0"}
                for _ in bundle
            ],
        }

    def send_bundle(self, bundle, target_block_number=None, **kwargs):
        self.send_calls.append((bundle, target_block_number))
        return {"bundleHash": "0x" + "77" * 32}

    def assert_not_called(self):
        if self.simulate_calls or self.send_calls:
            raise AssertionError("relay must not be contacted, but was")


def _self_transfer_tx(sender, **over):
    tx = {
        "to": sender,
        "value": 0,
        "gas": 21000,
        "maxFeePerGas": 2 * 10**9,
        "maxPriorityFeePerGas": 10**9,
        "data": b"",
        "nonce": 7,
        "chainId": SEPOLIA,
    }
    tx.update(over)
    return tx


def _read_audit(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ═════════════════════════════════════════════════════════════════════════════
# §2 — security limits are immutable code-controlled constants
# ═════════════════════════════════════════════════════════════════════════════


class TestLimitsAreImmutable(unittest.TestCase):
    """R2 §2 req 1–5: enormous configured limits cannot weaken policy."""

    def test_config_cannot_carry_any_limit_field(self):
        """The operational config does not HAVE limit fields: replaying the
        old override fails at construction (TypeError), on the production
        class and on the test-isolated helper alike."""
        for kwarg in (
            "max_value_wei", "max_gas_units", "max_fee_per_gas_wei",
            "max_priority_fee_wei", "max_total_fee_wei",
            "keystore_path", "passphrase_path", "address_path",
            "tx_log_path", "relay_url",
        ):
            with self.subTest(field=kwarg):
                self.assertNotIn(
                    kwarg, TestnetExecutionConfig.__dataclass_fields__)
                with self.assertRaises(TypeError):
                    TestnetExecutionConfig(**{kwarg: HUGE_WEI})

    def test_no_env_var_can_change_the_limits(self):
        """No environment variable is read for any cap — the constants are
        code-controlled, and from_env() carries only operational inputs."""
        with patch.dict(os.environ, {
            "DSS_TESTNET_MAX_VALUE_WEI": str(HUGE_WEI),
            "DSS_TESTNET_MAX_GAS_UNITS": str(HUGE_GAS),
            "DSS_TESTNET_MAX_FEE_PER_GAS_WEI": str(HUGE_FEE_PRICE),
            "DSS_TESTNET_MAX_TOTAL_FEE_WEI": str(HUGE_WEI),
        }):
            cfg = TestnetExecutionConfig.from_env()
            self.assertEqual(
                list(TestnetExecutionConfig.__dataclass_fields__),
                ["rpc_url", "expected_chain_id"],
            )
        self.assertEqual(SECURITY_POLICY.max_value_wei, MAX_TESTNET_TX_VALUE_WEI)
        self.assertEqual(SECURITY_POLICY.max_gas_units, MAX_TESTNET_GAS_UNITS)
        self.assertEqual(
            SECURITY_POLICY.max_fee_per_gas_wei,
            MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
        )
        self.assertEqual(
            SECURITY_POLICY.max_priority_fee_wei,
            MAX_TESTNET_MAX_PRIORITY_FEE_WEI,
        )
        self.assertEqual(
            SECURITY_POLICY.max_total_fee_wei, MAX_TESTNET_TX_FEE_WEI)

    def test_security_policy_object_has_no_state(self):
        """The policy singleton carries no instance state: attribute
        assignment is refused, so no caller can graft an override onto it."""
        self.assertIsInstance(SECURITY_POLICY, TestnetSecurityPolicy)
        with self.assertRaises((AttributeError, TypeError)):
            SECURITY_POLICY.max_value_wei = HUGE_WEI
        with self.assertRaises((AttributeError, TypeError)):
            SECURITY_POLICY.allowed_chains = {1: "mainnet"}

    def test_policy_primitives_accept_no_limit_override(self):
        """The policy-validation primitives expose NO limit parameter at
        all (structural pin)."""
        for fn in (
            validate_transaction, validate_bundle,
            validate_fee_exposure, validate_bundle_fee_exposure,
        ):
            params = set(inspect.signature(fn).parameters)
            for limit in (
                "max_value_wei", "max_gas_units", "max_fee_per_gas_wei",
                "max_priority_fee_wei", "max_total_fee_wei",
            ):
                self.assertNotIn(
                    limit, params,
                    f"{fn.__name__} must not accept a {limit} override",
                )

    def test_enormous_configured_limits_cannot_weaken_value_and_gas_caps(self):
        """R2 §5 req 1: a manually constructed configuration with enormous
        transaction-value and gas limits cannot weaken the real policy —
        the production interface refuses such fields, and the REAL caps
        still refuse an over-cap transaction."""
        # (a) the override does not exist on any production interface:
        with self.assertRaises(TypeError):
            TestnetExecutionConfig(
                rpc_url="https://sepolia.example",
                max_value_wei=HUGE_WEI, max_gas_units=HUGE_GAS,
            )
        # (b) the REAL caps still hold at the policy primitive level:
        sender = "0x" + "aa" * 20
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                _self_transfer_tx(sender, value=MAX_TESTNET_TX_VALUE_WEI + 1),
                sender=sender, chain_id=SEPOLIA,
            )
        self.assertIn("exceeds the testnet cap", str(ctx.exception))
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                _self_transfer_tx(sender, gas=MAX_TESTNET_GAS_UNITS + 1),
                sender=sender, chain_id=SEPOLIA,
            )
        self.assertIn("outside the permitted testnet range", str(ctx.exception))

    def test_enormous_fee_ceilings_cannot_weaken_fee_caps(self):
        """R2 §5 req 2: enormous fee-per-gas / priority-fee ceilings cannot
        weaken the real policy."""
        sender = "0x" + "aa" * 20
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                _self_transfer_tx(
                    sender,
                    maxFeePerGas=MAX_TESTNET_MAX_FEE_PER_GAS_WEI + 1,
                    maxPriorityFeePerGas=MAX_TESTNET_MAX_PRIORITY_FEE_WEI + 1,
                ),
                sender=sender, chain_id=SEPOLIA,
            )
        self.assertIn("exceeds the testnet cap", str(ctx.exception))
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_fee_exposure(
                _self_transfer_tx(
                    sender,
                    maxFeePerGas=MAX_TESTNET_MAX_FEE_PER_GAS_WEI,
                    maxPriorityFeePerGas=MAX_TESTNET_MAX_PRIORITY_FEE_WEI,
                    gas=MAX_TESTNET_TX_FEE_WEI // MAX_TESTNET_MAX_FEE_PER_GAS_WEI + 1,
                )
            )
        self.assertIn("exceeds the testnet cap", str(ctx.exception))

    def test_enormous_aggregate_ceiling_cannot_weaken_bundle_limits(self):
        """R2 §5 req 3: an enormous aggregate fee ceiling cannot weaken the
        Flashbots bundle limits — two individually compliant transactions
        whose combined exposure exceeds MAX_TESTNET_TX_FEE_WEI are refused
        as a whole."""
        price = 49 * 10**9  # individually compliant (< 50 gwei)
        gas = 21_000
        # one tx at the top of the per-tx budget; a second identical one
        # busts the aggregate cap
        compliant = {
            "to": "0x" + "aa" * 20, "value": 0, "gas": gas,
            "maxFeePerGas": price, "maxPriorityFeePerGas": 10**9, "data": b"",
        }
        self.assertLessEqual(gas * price, MAX_TESTNET_TX_FEE_WEI)
        self.assertGreater(2 * gas * price, MAX_TESTNET_TX_FEE_WEI)
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_bundle(
                [dict(compliant), dict(compliant)],
                sender=compliant["to"], chain_id=SEPOLIA,
            )
        self.assertIn("aggregate bundle fee exposure", str(ctx.exception))
        with self.assertRaises(TestnetSafetyError):
            validate_bundle_fee_exposure(
                [dict(compliant), dict(compliant)])

    def test_enormous_limits_through_the_live_orchestration_shape(self):
        """End to end with the offline stubs: a caller that does everything
        else right (opt-in present, designated custody, compliant RPC
        stub) still cannot push an over-cap value through the direct-RPC
        orchestration, and nothing is signed or broadcast."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            w3 = _GateW3()
            with self.assertRaises(TestnetSafetyError) as ctx:
                sign_and_submit_testnet_tx(
                    _self_transfer_tx(acct.address, value=HUGE_WEI),
                    purpose="R2 value cap", config=cfg,
                    web3_factory=lambda url: w3,
                )
            self.assertIn("exceeds the testnet cap", str(ctx.exception))
            self.assertEqual(w3.raw_submissions, [])  # fail closed


# ═════════════════════════════════════════════════════════════════════════════
# §3 — wallet custody, audit destination and relay are code-controlled
# ═════════════════════════════════════════════════════════════════════════════


class TestCustodyAuditRelayProtected(unittest.TestCase):
    """R2 §5 reqs 4–6: caller-supplied custody/audit/relay settings have
    no effect; the designated destinations are always used."""

    def test_custody_paths_are_code_controlled(self):
        """The designated custody locations are the SECURITY_POLICY paths
        (the DSS secret store), and the custody functions read exactly
        them — there is no parameter to point them elsewhere."""
        self.assertEqual(
            SECURITY_POLICY.keystore_path,
            os.path.join("/home/z/my-project/.secrets",
                         "dss_testnet_wallet.keystore.json"),
        )
        self.assertEqual(
            SECURITY_POLICY.passphrase_path,
            os.path.join("/home/z/my-project/.secrets",
                         "dss_testnet_wallet.pass"),
        )
        self.assertEqual(
            SECURITY_POLICY.address_path,
            os.path.join("/home/z/my-project/.secrets",
                         "dss_testnet_wallet.address"),
        )
        self.assertEqual(list(inspect.signature(
            load_agent_wallet).parameters), [])
        self.assertEqual(list(inspect.signature(
            verify_secret_store).parameters), [])

    def test_caller_wallet_and_passphrase_paths_cannot_replace_custody(self):
        """R2 §5 req 4: caller-supplied wallet/passphrase paths cannot
        replace the designated DSS custody locations — the config refuses
        the fields, and the wallet path ignores them completely."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            # (a) a production config cannot carry substitute paths:
            with self.assertRaises(TypeError):
                TestnetExecutionConfig(
                    keystore_path=os.path.join(tmp, "evil.json"),
                    passphrase_path=os.path.join(tmp, "evil.pass"),
                )
            # (b) even the test helper refuses values that diverge from
            # the live policy (the controlled mock must be coherent):
            with self.assertRaises(AssertionError):
                IsolatedTestnetConfig(
                    keystore_path=os.path.join(tmp, "evil.json"))
            # (c) the custody check runs against the DESIGNATED paths —
            # an empty designated store refuses regardless of what the
            # caller wanted to point at:
            with tempfile.TemporaryDirectory() as empty, throwaway_store(empty):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    verify_secret_store()
                self.assertIn("missing or not a regular file", str(ctx.exception))
            # (d) positive control: with the designated (patched) store
            # populated, the REAL wallet path decrypts the REAL designated
            # keystore — not anything a caller handed in.
            wallet = load_agent_wallet()
            self.assertEqual(wallet.address.lower(), acct.address.lower())

    def test_caller_audit_paths_cannot_redirect_mandatory_records(self):
        """R2 §5 req 5: caller-supplied audit paths cannot suppress or
        redirect mandatory audit records — every orchestration record goes
        to the designated destination."""
        self.assertEqual(
            SECURITY_POLICY.tx_log_path,
            os.path.join("logs", "testnet_tx_log.jsonl"),
        )
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            designated = _gate.SECURITY_POLICY.tx_log_path
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            # (a) a production config cannot carry an audit redirect:
            with self.assertRaises(TypeError):
                TestnetExecutionConfig(tx_log_path=os.path.join(tmp, "evil.jsonl"))
            # (b) a refusal under the mock-audited gate lands in the
            # DESIGNATED (patched) audit log, not anywhere else:
            w3 = _GateW3()
            with self.assertRaises(TestnetSafetyError):
                sign_and_submit_testnet_tx(
                    _self_transfer_tx(acct.address, value=HUGE_WEI),
                    purpose="R2 audit destination", config=cfg,
                    web3_factory=lambda url: w3,
                )
            records = _read_audit(designated)  # the designated (patched) path
            self.assertEqual([r["outcome"] for r in records], ["refused"])
            self.assertEqual(records[0]["purpose"], "R2 audit destination")
            # no stray audit file anywhere else in tmp:
            self.assertEqual(
                sorted(os.listdir(tmp)),
                ["address", "keystore.json", "pass", "txlog.jsonl"],
            )

    def test_audit_path_failure_does_not_turn_refusal_into_success(self):
        """R2 §3: an audit-path failure must not turn an execution refusal
        into an apparent success — the refusal is still raised."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            # Break the DESIGNATED audit destination: make it a directory
            # so every write fails.
            os.makedirs(_gate.SECURITY_POLICY.tx_log_path, exist_ok=True)
            w3 = _GateW3()
            with self.assertRaises(TestnetSafetyError) as ctx:
                sign_and_submit_testnet_tx(
                    _self_transfer_tx(acct.address, value=HUGE_WEI),
                    purpose="R2 audit failure", config=cfg,
                    web3_factory=lambda url: w3,
                )
            # The POLICY verdict is preserved verbatim:
            self.assertIn("exceeds the testnet cap", str(ctx.exception))
            self.assertEqual(w3.raw_submissions, [])

    def test_caller_relay_urls_cannot_redirect_production_session(self):
        """R2 §5 req 6: caller-supplied relay URLs cannot redirect a
        production Flashbots session — the reviewed Sepolia relay is
        code-controlled and the gate hands exactly it to the transport."""
        self.assertEqual(SECURITY_POLICY.relay_url, FLASHBOTS_SEPOLIA_RELAY)
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            # (a) a config cannot carry a relay redirect:
            with self.assertRaises(TypeError):
                TestnetExecutionConfig(relay_url="https://evil.example")
            # (b) the gate passes EXACTLY the reviewed relay URL to the
            # relay factory — captured to prove it:
            captured = []

            def relay_factory(url, auth_account, relay_url):
                captured.append(relay_url)
                return _RelayStub()

            session = prepare_flashbots_bundle(
                [_self_transfer_tx(acct.address)],
                purpose="R2 relay pin", config=cfg,
                web3_factory=lambda url: _PrepareW3(),
                relay_factory=relay_factory,
            )
            self.assertEqual(captured, [FLASHBOTS_SEPOLIA_RELAY])
            self.assertEqual(session._config.relay_url, FLASHBOTS_SEPOLIA_RELAY)


# ═════════════════════════════════════════════════════════════════════════════
# §4 — central policy cannot be bypassed; both paths enforce identical caps
# ═════════════════════════════════════════════════════════════════════════════


class HostilePolicy(ExecutionPolicy):
    """A policy a bypass-seeking caller would inject: it hands out an
    arbitrary operational config. It confers NO authority — the gate
    re-derives every decision from the authoritative sources."""

    name = "hostile"
    network = "testnet"

    def __init__(self):
        self._cfg = TestnetExecutionConfig(
            rpc_url="https://sepolia.example",
            expected_chain_id=SEPOLIA,
        )

    def authorize(self, purpose, chain_id=None):
        return self._cfg

    def execution_available(self):
        return True  # lie — the gate decides, not the policy

    def refusal_reason(self):
        return "hostile policy always claims readiness"


class TestBypassResistance(unittest.TestCase):
    """R2 §5 req 7: direct calls to either gate orchestration (and to the
    boundary with an injected policy) cannot bypass these protections."""

    def test_direct_orchestration_calls_enforce_real_caps(self):
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            w3 = _GateW3()
            relay = _RelayStub()
            # direct-RPC orchestration: over-cap value refused
            with self.assertRaises(TestnetSafetyError) as ctx:
                sign_and_submit_testnet_tx(
                    _self_transfer_tx(acct.address, value=HUGE_WEI),
                    purpose="R2 direct call", config=cfg,
                    web3_factory=lambda url: w3,
                )
            self.assertIn("exceeds the testnet cap", str(ctx.exception))
            # Flashbots orchestration: over-cap value refused identically
            with self.assertRaises(TestnetSafetyError) as ctx:
                prepare_flashbots_bundle(
                    [_self_transfer_tx(acct.address, value=HUGE_WEI)],
                    purpose="R2 direct bundle call", config=cfg,
                    web3_factory=lambda url: _PrepareW3(),
                    relay_factory=lambda url, auth, relay_url: relay,
                )
            self.assertIn("exceeds the testnet cap", str(ctx.exception))
            relay.assert_not_called()
            self.assertEqual(w3.raw_submissions, [])

    def test_injected_policy_cannot_weaken_gate_settings(self):
        """R2 §4: a custom policy injected into the boundary cannot
        produce an authorized handle with weakened gate settings — the
        handle it mints still runs the gate, which enforces the real
        caps, real custody and the real environment opt-in."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            boundary = ExecutionAuthorizationBoundary(policy=HostilePolicy())
            # The hostile policy LIES that execution is available; the
            # minted handle still runs the REAL gate. With the opt-in
            # present (module patch) the handle exists — but an over-cap
            # transaction is refused exactly as through the boundary:
            auth = boundary.authorize_execution("hostile attempt")
            self.assertIsInstance(auth, AuthorizedExecution)
            self.assertEqual(auth.policy_name, "hostile")
            w3 = _GateW3()
            with self.assertRaises(TestnetSafetyError) as ctx:
                auth.sign_and_submit(
                    _self_transfer_tx(acct.address, value=HUGE_WEI),
                    web3_factory=lambda url: w3,
                )
            self.assertIn("exceeds the testnet cap", str(ctx.exception))
            self.assertEqual(w3.raw_submissions, [])
            # And with the REAL opt-in removed, the hostile policy can
            # still MINT a handle (it performs no checks), but the handle
            # cannot sign anything: the gate re-derives the decision and
            # refuses with the disabled-gate verdict.
            with _opted_out():
                rogue_handle = boundary.authorize_execution("hostile attempt")
                w3b = _GateW3()
                with self.assertRaises(TestnetSafetyError) as ctx2:
                    rogue_handle.sign_and_submit(
                        _self_transfer_tx(acct.address),
                        web3_factory=lambda url: w3b,
                    )
                self.assertIn("DISABLED", str(ctx2.exception))
                self.assertEqual(w3b.raw_submissions, [])

    def test_hand_minted_authorized_execution_gains_nothing(self):
        """Directly constructing an AuthorizedExecution (bypassing the
        boundary) does not bypass the gate either: the orchestrations
        re-run every check."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            rogue = AuthorizedExecution(
                policy_name="rogue", network="testnet",
                chain_id=SEPOLIA, sender=acct.address,
                purpose="rogue handle",
                config=TestnetExecutionConfig(rpc_url="https://sepolia.example"),
            )
            w3 = _GateW3()
            with _opted_out():
                with self.assertRaises(TestnetSafetyError) as ctx:
                    rogue.sign_and_submit(
                        _self_transfer_tx(acct.address),
                        web3_factory=lambda url: w3,
                    )
            self.assertIn("DISABLED", str(ctx.exception))
            self.assertEqual(w3.raw_submissions, [])

    def test_both_paths_enforce_identical_policy_constants(self):
        """R2 §5 req 8: the ordinary executor and Flashbots paths enforce
        IDENTICAL policy constants — the same violation produces the same
        refusal on both transports, and both read the same policy object."""
        for over in (
            {"value": HUGE_WEI},
            {"gas": HUGE_GAS},
            {"maxFeePerGas": HUGE_FEE_PRICE,
             "maxPriorityFeePerGas": MAX_TESTNET_MAX_PRIORITY_FEE_WEI},
        ):
            with self.subTest(violation=sorted(over)):
                with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
                    acct = _throwaway_keystore(tmp)
                    cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
                    direct_err = None
                    bundle_err = None
                    with self.assertRaises(TestnetSafetyError) as ctx:
                        sign_and_submit_testnet_tx(
                            _self_transfer_tx(acct.address, **over),
                            purpose="R2 parity", config=cfg,
                            web3_factory=lambda url: _GateW3(),
                        )
                    direct_err = str(ctx.exception)
                    with self.assertRaises(TestnetSafetyError) as ctx:
                        prepare_flashbots_bundle(
                            [_self_transfer_tx(acct.address, **over)],
                            purpose="R2 parity", config=cfg,
                            web3_factory=lambda url: _PrepareW3(),
                            relay_factory=lambda url, auth, relay_url:
                                _RelayStub(),
                        )
                    bundle_err = str(ctx.exception)
                    # the SAME constant fired on both transports — the
                    # bundle path wraps the identical refusal with its
                    # per-transaction context
                    self.assertIn(direct_err, bundle_err)


# ═════════════════════════════════════════════════════════════════════════════
# §5 — fail-closed refusals and the disabled-by-default application route
# ═════════════════════════════════════════════════════════════════════════════


class TestFailClosedAndDisabledDefault(unittest.TestCase):
    """R2 §5 reqs 9–10: refusals never decrypt, sign or broadcast; the
    standard application route stays disabled without the real opt-in."""

    def test_every_refusal_path_is_fail_closed(self):
        """No wallet decryption, no signing and no broadcast on ANY
        refusal (disabled gate, over-cap value, missing custody)."""
        booby = AssertionError("must never be reached on a refusal")
        with tempfile.TemporaryDirectory() as tmp:
            # (a) disabled gate: nothing happens at all
            with throwaway_store(tmp):
                acct = _throwaway_keystore(tmp)
                cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
                w3 = _GateW3()
                with patch(
                    "executor.testnet_gate.load_agent_wallet",
                    side_effect=booby,
                ):
                    with _opted_out():
                        with self.assertRaises(TestnetSafetyError) as ctx:
                            sign_and_submit_testnet_tx(
                                _self_transfer_tx(acct.address),
                                purpose="R2 fail-closed disabled",
                                config=cfg,
                                web3_factory=lambda url: w3,
                            )
                self.assertIn("DISABLED", str(ctx.exception))
                self.assertEqual(w3.raw_submissions, [])
            # (b) over-cap value with the gate on: refusal BEFORE custody
            with throwaway_store(tmp):
                w3 = _GateW3()
                relay = _RelayStub()
                with patch(
                    "executor.testnet_gate.load_agent_wallet",
                    side_effect=booby,
                ):
                    with self.assertRaises(TestnetSafetyError) as ctx:
                        sign_and_submit_testnet_tx(
                            _self_transfer_tx(acct.address, value=HUGE_WEI),
                            purpose="R2 fail-closed over-cap",
                            config=IsolatedTestnetConfig(
                                rpc_url="https://sepolia.example"),
                            web3_factory=lambda url: w3,
                        )
                    with self.assertRaises(TestnetSafetyError):
                        prepare_flashbots_bundle(
                            [_self_transfer_tx(acct.address, value=HUGE_WEI)],
                            purpose="R2 fail-closed over-cap bundle",
                            config=IsolatedTestnetConfig(
                                rpc_url="https://sepolia.example"),
                            web3_factory=lambda url: _PrepareW3(),
                            relay_factory=lambda url, auth, relay_url: relay,
                        )
                self.assertIn("exceeds the testnet cap", str(ctx.exception))
                self.assertEqual(w3.raw_submissions, [])
                relay.assert_not_called()

    def test_standard_application_route_disabled_without_real_opt_in(self):
        """R2 §5 req 10: without the REAL environment's explicit testnet
        opt-in the standard application route (default boundary, executor
        wiring) is fully disabled; EXECUTE_ONCHAIN=true cannot change it."""
        reset_execution_authorization()
        try:
            with patch.dict(os.environ, {"EXECUTE_ONCHAIN": "true"}), _opted_out():
                boundary = get_execution_authorization()
                self.assertFalse(boundary.execution_available())
                self.assertIn(
                    "DSS_TESTNET_EXEC", boundary.refusal_reason())
                with self.assertRaises(TestnetSafetyError):
                    boundary.authorize_execution("standard route")
                # policy resolution ignores EXECUTE_ONCHAIN and CHAIN_ID:
                with patch.dict(os.environ, {"CHAIN_ID": str(SEPOLIA)}):
                    policy = resolve_execution_policy()
                from executor.authorization import DisabledExecutionPolicy
                self.assertIsInstance(policy, DisabledExecutionPolicy)
        finally:
            reset_execution_authorization()


# ═════════════════════════════════════════════════════════════════════════════
# Structural invariants — interfaces cannot silently regress
# ═════════════════════════════════════════════════════════════════════════════


class TestStructuralInvariants(unittest.TestCase):
    """R2 §4: the production interfaces are pinned so future code cannot
    silently re-introduce caller-configurable security policy or
    alternative submission paths."""

    def test_orchestration_signatures_accept_operational_config_only(self):
        from executor.testnet_gate import (
            prepare_flashbots_bundle as prep,
            sign_and_submit_testnet_tx as sign,
        )
        self.assertEqual(
            list(inspect.signature(sign).parameters),
            ["tx", "purpose", "config", "web3_factory", "receipt_timeout"],
        )
        self.assertEqual(
            list(inspect.signature(prep).parameters),
            ["unsigned_txs", "purpose", "config", "web3_factory",
             "relay_factory"],
        )
        self.assertEqual(
            list(TestnetExecutionConfig.__dataclass_fields__),
            ["rpc_url", "expected_chain_id"],
        )

    def test_orchestrations_source_pins_designated_policy_destinations(self):
        """Both orchestrations must read the audit destination and the
        relay from SECURITY_POLICY — and must not read any security field
        from the operational config."""
        from executor.testnet_gate import (
            prepare_flashbots_bundle as prep,
            sign_and_submit_testnet_tx as sign,
        )
        for fn in (sign, prep):
            src = inspect.getsource(fn)
            self.assertIn("SECURITY_POLICY", src, fn.__name__)
            for forbidden in (
                "cfg.tx_log_path", "cfg.relay_url", "cfg.max_value_wei",
                "cfg.max_gas_units", "cfg.max_fee_per_gas_wei",
                "cfg.max_priority_fee_wei", "cfg.max_total_fee_wei",
                "config.tx_log_path", "config.relay_url",
                "load_agent_wallet(cfg", "load_agent_wallet(config",
            ):
                self.assertNotIn(
                    forbidden, src,
                    f"{fn.__name__} must not reference {forbidden}",
                )

    def test_no_alternative_broadcast_path_outside_the_gate(self):
        """The ONLY signing/submission implementation in the whole
        repository is the gate: no non-test module touches raw
        transaction submission, bundle submission, direct JSON-RPC send
        or any private-transaction route (R1 §4 audit, pinned as a
        structural invariant — future code cannot silently re-introduce
        an alternative path)."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        broadcast_markers = (
            "send_raw_transaction", "send_transaction",
            "eth_sendRawTransaction", "eth_sendTransaction",
            "send_bundle", "eth_sendBundle",
            "send_private_transaction",
        )
        skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv",
                     "cache", "logs"}
        offenders = []
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, repo_root)
                if rel.startswith("tests" + os.sep) or rel.startswith(
                        "study" + os.sep):
                    continue
                if rel == os.path.join("executor", "testnet_gate.py"):
                    continue  # THE sanctioned submission boundary
                with open(path, encoding="utf-8", errors="replace") as f:
                    src = f.read()
                for marker in broadcast_markers:
                    if marker in src:
                        offenders.append((rel, marker))
        self.assertEqual(
            offenders, [],
            "alternative broadcast path re-introduced outside the gate",
        )

    def test_known_targets_remain_empty(self):
        """No contract target was approved (R1 pin, kept): only a
        separately reviewed code change may add one."""
        self.assertEqual(KNOWN_TARGETS, {})
        self.assertIs(SECURITY_POLICY.known_targets, KNOWN_TARGETS)


if __name__ == "__main__":
    unittest.main()
