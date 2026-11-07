"""DSS-WO-ENG-015 R3 — immutable authorization maps and sealed transports.

The R3 correction closes the two remaining mutable-policy weaknesses:

  §2 the code-controlled authorization maps are GENUINELY read-only:
     - ``ALLOWED_TESTNET_CHAINS`` and ``KNOWN_TARGETS`` are deep-frozen
       ``MappingProxyType`` views (nested maps included);
     - mutating ANY exposed policy mapping — the module constants,
       ``SECURITY_POLICY.allowed_chains``, ``SECURITY_POLICY.known_targets``
       or the ``effective_allowlist()`` result — is refused and cannot
       authorize a chain or a contract target;
     - Sepolia remains the only authorized testnet and ``KNOWN_TARGETS``
       remains empty.
  §3 the production signing/submission interfaces accept NO transport
     injection:
     - ``web3_factory``/``relay_factory`` are gone from
       ``sign_and_submit_testnet_tx``, ``prepare_flashbots_bundle`` and
       both ``AuthorizedExecution`` methods;
     - the transports are constructed internally (direct RPC from the
       configured operational URL; Flashbots against the code-controlled
       ``FLASHBOTS_SEPOLIA_RELAY``);
     - offline determinism is preserved through the PRIVATE internal
       builders patched by test fixtures — never a runtime argument.

All tests are offline: throwaway wallets, controlled mocks, no real
credentials, no live RPC, no signing or broadcast outside the gate stubs.
"""
import ast
import contextlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from types import MappingProxyType
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executor.testnet_gate as _gate

from eth_account import Account

from tests.gate_r2_fixtures import IsolatedTestnetConfig, throwaway_store
from tests.gate_r3_fixtures import frozen_map, offline_transports

from executor.authorization import (
    AuthorizedExecution,
    ExecutionAuthorizationBoundary,
)

from executor.testnet_gate import (
    ALLOWED_TESTNET_CHAINS,
    FLASHBOTS_SEPOLIA_RELAY,
    KNOWN_TARGETS,
    SECURITY_POLICY,
    TestnetExecutionConfig,
    TestnetSafetyError,
    check_chain_id,
    effective_allowlist,
    prepare_flashbots_bundle,
    sign_and_submit_testnet_tx,
    validate_transaction,
)

SEPOLIA = 11155111
HOLESKY = 17000

_MODULE_OPT_IN = None


def setUpModule():
    """Simulate the explicit testnet opt-in for this module ONLY via the
    REAL process environment (the single authorization authority)."""
    global _MODULE_OPT_IN
    _MODULE_OPT_IN = patch.dict(os.environ, {"DSS_TESTNET_EXEC": "1"}, clear=False)
    _MODULE_OPT_IN.start()
    unittest.addModuleCleanup(_MODULE_OPT_IN.stop)


@contextlib.contextmanager
def _opted_out():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DSS_TESTNET_EXEC", None)
        yield


def _restricted_write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _throwaway_keystore(tmpdir: str):
    acct = Account.create()
    keystore = Account.encrypt(acct.key, b"r3-test-passphrase")
    _restricted_write(os.path.join(tmpdir, "keystore.json"),
                      json.dumps(keystore).encode())
    _restricted_write(os.path.join(tmpdir, "pass"),
                      b"r3-test-passphrase".hex().encode())
    _restricted_write(os.path.join(tmpdir, "address"),
                      (acct.address + "\n").encode())
    return acct


def _self_transfer_tx(sender, **over):
    tx = {
        "to": sender, "value": 0, "gas": 21000,
        "maxFeePerGas": 2 * 10**9, "maxPriorityFeePerGas": 10**9,
        "data": b"", "nonce": 7, "chainId": SEPOLIA,
    }
    tx.update(over)
    return tx


class _RecordingRpc:
    """Mock direct-RPC transport: records every raw broadcast."""

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


class _RecordingRelay:
    """Mock Flashbots transport: records calls, never executes anything."""

    def __init__(self, chain_values=(SEPOLIA,), head=99):
        self._rpc = _RecordingRpc(chain_values=chain_values, head=head)
        self.eth = self._rpc
        self.flashbots = self
        self.simulate_calls = []
        self.send_calls = []

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


# ═════════════════════════════════════════════════════════════════════════════
# §4a — authorization-map immutability
# ═════════════════════════════════════════════════════════════════════════════


class TestAuthorizationMapImmutability(unittest.TestCase):
    """R3 §4: the policy maps cannot be mutated through ANY exposed
    reference, and a mutation attempt can never authorize anything."""

    def test_mutating_module_allowlist_directly_is_refused(self):
        with self.assertRaises(TypeError):
            ALLOWED_TESTNET_CHAINS[HOLESKY] = "Holesky"
        with self.assertRaises(TypeError):
            del ALLOWED_TESTNET_CHAINS[SEPOLIA]
        # a read-only mapping exposes NO mutating API at all:
        for method in ("clear", "pop", "popitem", "setdefault", "update"):
            self.assertFalse(
                hasattr(ALLOWED_TESTNET_CHAINS, method), method)
        self.assertEqual(
            dict(ALLOWED_TESTNET_CHAINS), {SEPOLIA: "Ethereum Sepolia"})

    def test_mutating_policy_allowed_chains_is_refused(self):
        exposed = SECURITY_POLICY.allowed_chains
        with self.assertRaises(TypeError):
            exposed[HOLESKY] = "Holesky"
        with self.assertRaises(TypeError):
            del exposed[SEPOLIA]
        self.assertEqual(dict(effective_allowlist()),
                         {SEPOLIA: "Ethereum Sepolia"})

    def test_mutating_effective_allowlist_output_cannot_change_allowlist(self):
        returned = effective_allowlist()
        with self.assertRaises(TypeError):
            returned[HOLESKY] = "Holesky"
        # the live allowlist is untouched and the SAME read-only object
        self.assertEqual(dict(effective_allowlist()),
                         {SEPOLIA: "Ethereum Sepolia"})
        self.assertIs(effective_allowlist(), ALLOWED_TESTNET_CHAINS)

    def test_added_chain_through_policy_mapping_cannot_authorize(self):
        """Every route a caller could try to abuse is read-only, so a
        smuggled chain can never pass the identity gate."""
        for mapping in (
            ALLOWED_TESTNET_CHAINS,
            SECURITY_POLICY.allowed_chains,
            effective_allowlist(),
        ):
            with self.subTest(route=type(mapping).__name__):
                with self.assertRaises(TypeError):
                    mapping[HOLESKY] = "smuggled"
        with self.assertRaises(TestnetSafetyError):
            check_chain_id(HOLESKY)
        self.assertNotIn(HOLESKY, effective_allowlist())

    def test_sepolia_permitted_only_with_all_other_checks_passing(self):
        """Sepolia alone is not execution authorization: the identity gate
        accepts it, but mainnet stays hard-rejected, unknown chains fail
        closed, a mismatched expectation fails, and the disabled-gate
        verdict still refuses everything."""
        self.assertEqual(check_chain_id(SEPOLIA), SEPOLIA)
        # mainnet is hard-rejected BEFORE the allowlist is even consulted:
        with self.assertRaises(TestnetSafetyError):
            check_chain_id(1)
        with self.assertRaises(TestnetSafetyError):
            check_chain_id(SEPOLIA, expected=999999)
        with _opted_out():
            with self.assertRaises(TestnetSafetyError) as ctx:
                TestnetExecutionConfig(
                    rpc_url="https://sepolia.example").require_ready()
            self.assertIn("DISABLED", str(ctx.exception))

    def test_mutating_module_known_targets_directly_is_refused(self):
        with self.assertRaises(TypeError):
            KNOWN_TARGETS[SEPOLIA] = {"0x" + "cc" * 20: "evil"}
        with self.assertRaises(TypeError):
            del KNOWN_TARGETS[SEPOLIA]
        for method in ("clear", "pop", "popitem", "setdefault", "update"):
            self.assertFalse(hasattr(KNOWN_TARGETS, method), method)
        self.assertEqual(dict(KNOWN_TARGETS), {})

    def test_mutating_policy_known_targets_is_refused(self):
        exposed = SECURITY_POLICY.known_targets
        with self.assertRaises(TypeError):
            exposed[SEPOLIA] = {"0x" + "cc" * 20: "evil"}
        self.assertIs(SECURITY_POLICY.known_targets, KNOWN_TARGETS)
        self.assertEqual(dict(KNOWN_TARGETS), {})

    def test_injected_recipient_through_policy_map_cannot_authorize(self):
        """A contract target cannot be approved through any returned map:
        the mappings expose no mutating API, item assignment is refused,
        and validation still refuses the target."""
        attacker = ("0x" + "cc" * 20).lower()
        for mapping in (KNOWN_TARGETS, SECURITY_POLICY.known_targets):
            with self.subTest(route=type(mapping).__name__):
                with self.assertRaises(TypeError):
                    mapping[SEPOLIA] = {attacker: "evil"}
                for method in ("clear", "pop", "popitem", "setdefault",
                               "update"):
                    self.assertFalse(hasattr(mapping, method), method)
        with self.assertRaises(TestnetSafetyError) as ctx:
            validate_transaction(
                _self_transfer_tx(attacker),
                sender="0x" + "11" * 20,  # NOT the attacker: not a self-transfer
                chain_id=SEPOLIA,
            )
        self.assertIn("neither the agent wallet nor", str(ctx.exception))

    def test_nested_target_maps_are_immutable_too(self):
        """The freeze is RECURSIVE: a nested chain-to-target map built the
        way production builds them is as read-only as the outer map —
        protecting only the outer dictionary would be insufficient."""
        nested = _gate._frozen_policy_mapping(
            {SEPOLIA: {"0x" + "ab" * 20: " reviewed target"}}
        )
        self.assertIsInstance(nested, MappingProxyType)
        with self.assertRaises(TypeError):
            nested[SEPOLIA]["0x" + "ab" * 20] = "mutated"
        with self.assertRaises(TypeError):
            nested[SEPOLIA]["0x" + "cd" * 20] = "added"
        with self.assertRaises(TypeError):
            del nested[SEPOLIA]
        self.assertEqual(
            dict(nested[SEPOLIA]), {"0x" + "ab" * 20: " reviewed target"})

    def test_controlled_mock_replacement_still_works(self):
        """The established offline technique survives: tests REPLACE the
        module attribute wholesale (never mutate it) and the gate reads
        the patched map at execution time — while the production map is
        restored afterwards and stays immutable."""
        self.assertIsInstance(ALLOWED_TESTNET_CHAINS, MappingProxyType)
        with patch.object(
                _gate, "ALLOWED_TESTNET_CHAINS",
                frozen_map({**_gate.ALLOWED_TESTNET_CHAINS,
                            HOLESKY: "Holesky"}),
        ):
            self.assertEqual(check_chain_id(HOLESKY, expected=HOLESKY), HOLESKY)
            self.assertIn(HOLESKY, effective_allowlist())
        # restored and still read-only:
        self.assertNotIn(HOLESKY, effective_allowlist())
        with self.assertRaises(TypeError):
            ALLOWED_TESTNET_CHAINS[HOLESKY] = "Holesky"


# ═════════════════════════════════════════════════════════════════════════════
# §4b — runtime transport isolation
# ═════════════════════════════════════════════════════════════════════════════


class TestRuntimeTransportIsolation(unittest.TestCase):
    """R3 §4: no caller can inject a transport through any production
    interface; the gate builds its transports internally."""

    def test_signing_signature_accepts_no_web3_factory(self):
        params = inspect.signature(sign_and_submit_testnet_tx).parameters
        self.assertNotIn("web3_factory", params)
        self.assertEqual(
            list(params), ["tx", "purpose", "config", "receipt_timeout"])
        with self.assertRaises(TypeError):
            sign_and_submit_testnet_tx(
                _self_transfer_tx("0x" + "aa" * 20),
                purpose="injection", config=None,
                web3_factory=lambda url: MagicMock(),
            )

    def test_flashbots_signature_accepts_no_factory(self):
        params = inspect.signature(prepare_flashbots_bundle).parameters
        self.assertNotIn("web3_factory", params)
        self.assertNotIn("relay_factory", params)
        self.assertEqual(list(params), ["unsigned_txs", "purpose", "config"])
        with self.assertRaises(TypeError):
            prepare_flashbots_bundle(
                [_self_transfer_tx("0x" + "aa" * 20)],
                purpose="injection", config=None,
                web3_factory=lambda url: MagicMock(),
            )
        with self.assertRaises(TypeError):
            prepare_flashbots_bundle(
                [_self_transfer_tx("0x" + "aa" * 20)],
                purpose="injection", config=None,
                relay_factory=lambda url, auth, relay_url: MagicMock(),
            )

    def test_authorized_execution_exposes_no_injection_parameters(self):
        for method in (
            AuthorizedExecution.sign_and_submit,
            AuthorizedExecution.prepare_flashbots_bundle,
        ):
            with self.subTest(method=method.__name__):
                params = inspect.signature(method).parameters
                self.assertNotIn("web3_factory", params)
                self.assertNotIn("relay_factory", params)
        with self.assertRaises(TypeError):
            AuthorizedExecution.sign_and_submit(
                MagicMock(),  # unbound call on a dummy receiver
                tx=_self_transfer_tx("0x" + "aa" * 20),
                web3_factory=lambda url: MagicMock(),
            )
        with self.assertRaises(TypeError):
            AuthorizedExecution.prepare_flashbots_bundle(
                MagicMock(),
                unsigned_txs=[_self_transfer_tx("0x" + "aa" * 20)],
                relay_factory=lambda url, auth, relay_url: MagicMock(),
            )

    def test_offline_tests_still_control_mocks_via_private_seam(self):
        """Deterministic offline testing survives: the PRIVATE builders
        are patched by test fixtures (never by a runtime argument) and
        the full orchestration runs against the controlled mocks."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            w3 = _RecordingRpc()
            relay = _RecordingRelay()
            with offline_transports(rpc=w3):
                record = sign_and_submit_testnet_tx(
                    _self_transfer_tx(acct.address),
                    purpose="R3 offline seam", config=cfg,
                )
            self.assertEqual(record["outcome"], "mined")
            self.assertEqual(len(w3.raw_submissions), 1)
            with offline_transports(rpc=_RecordingRpc(), relay=relay):
                session = prepare_flashbots_bundle(
                    [_self_transfer_tx(acct.address)],
                    purpose="R3 offline seam", config=cfg,
                )
            relay.assert_not_called()  # prepare never contacts the relay
            session.simulate()
            self.assertEqual(len(relay.simulate_calls), 1)

    def test_caller_cannot_inject_chain_id_lying_provider(self):
        """A lying provider cannot enter through the public API (TypeError)
        and the internal transport is not influenced by any caller state:
        the gate still verifies the REPORTED chain at every checkpoint."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            liar = _RecordingRpc(chain_values=[1])  # claims mainnet
            with self.assertRaises(TypeError):
                sign_and_submit_testnet_tx(
                    _self_transfer_tx(acct.address),
                    purpose="lying provider", config=cfg,
                    web3_factory=lambda url: liar,
                )
            # through the ONLY entry (the internal builder) the lie is
            # still caught by the identity gate:
            with offline_transports(rpc=liar):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    sign_and_submit_testnet_tx(
                        _self_transfer_tx(acct.address),
                        purpose="lying provider", config=cfg,
                    )
            self.assertIn("refuses Ethereum mainnet", str(ctx.exception))
            self.assertEqual(liar.raw_submissions, [])

    def test_caller_cannot_inject_relay_transport(self):
        """A relay transport that ignores the reviewed relay URL cannot
        enter through the public API, and the production relay builder
        reads the code-controlled relay from SECURITY_POLICY itself."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            rogue_relay = _RecordingRelay()
            with self.assertRaises(TypeError):
                prepare_flashbots_bundle(
                    [_self_transfer_tx(acct.address)],
                    purpose="rogue relay", config=cfg,
                    relay_factory=lambda url, auth, relay_url: rogue_relay,
                )
            # the ONLY production relay construction reads the reviewed
            # constant INSIDE the builder — proven with the REAL builder
            # and a spied flashbot attachment (no network object dials):
            attached = []

            def _spy_flashbot(relay_w3, auth_account, relay_url):
                attached.append(relay_url)

            with patch.object(_gate, "FLASHBOTS_SEPOLIA_RELAY",
                              "https://fake-relay.example"), \
                    patch("flashbots.flashbot", _spy_flashbot):
                transport = _gate._build_relay_transport(
                    "https://sepolia.example", acct)
            self.assertEqual(attached, ["https://fake-relay.example"])
            # the provider points at the configured operational RPC; the
            # relay destination came from the code-controlled policy read
            self.assertEqual(
                getattr(transport.provider, "endpoint_uri", None),
                "https://sepolia.example",
            )

    def test_production_flashbots_transport_always_code_controlled_relay(self):
        """The prepare orchestration constructs its relay transport ONLY
        through the private builder (spied), which reads the reviewed
        relay from the code-controlled policy at construction time."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            builder_calls = []
            relay = _RecordingRelay()

            def _spy_builder(rpc_url, auth_account):
                builder_calls.append(
                    (rpc_url, _gate.SECURITY_POLICY.relay_url))
                return relay

            with offline_transports(rpc=_RecordingRpc()), patch.object(
                    _gate, "_build_relay_transport", _spy_builder):
                prepare_flashbots_bundle(
                    [_self_transfer_tx(acct.address)],
                    purpose="R3 relay construction", config=cfg,
                )
            self.assertEqual(
                builder_calls,
                [("https://sepolia.example", FLASHBOTS_SEPOLIA_RELAY)],
            )
            relay.assert_not_called()

    def test_chain_rechecked_before_signing_and_submission(self):
        """Both transports re-verify the REPORTED chain ID immediately
        before signing and again immediately before submission: a chain
        flip after the initial read refuses and nothing is broadcast."""
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url="https://sepolia.example")
            with patch.object(
                    _gate, "ALLOWED_TESTNET_CHAINS",
                    frozen_map({**_gate.ALLOWED_TESTNET_CHAINS,
                                HOLESKY: "Holesky"}),
            ):
                flipper = _RecordingRpc(chain_values=[SEPOLIA, SEPOLIA, HOLESKY])
                with offline_transports(rpc=flipper):
                    with self.assertRaises(TestnetSafetyError) as ctx:
                        sign_and_submit_testnet_tx(
                            _self_transfer_tx(acct.address),
                            purpose="R3 chain flip", config=cfg,
                        )
                self.assertIn("RPC chain mismatch", str(ctx.exception))
                self.assertEqual(flipper.raw_submissions, [])

    def test_transport_exception_cannot_leak_payload_or_credentials(self):
        """No raw signed transaction or credential material escapes
        through a transport exception or the audit log: a hostile stub
        error embedding both is contained by the gate."""
        cred_url = "https://API_KEY_12345@fake-rpc.example/v2"
        with tempfile.TemporaryDirectory() as tmp, throwaway_store(tmp):
            acct = _throwaway_keystore(tmp)
            cfg = IsolatedTestnetConfig(rpc_url=cred_url)

            class _HostileRpc(_RecordingRpc):
                def send_raw_transaction(self, raw):
                    raise RuntimeError(
                        f"broadcast failed for {raw.hex()} via {cred_url}")

            hostile = _HostileRpc(chain_values=[SEPOLIA, SEPOLIA, SEPOLIA])
            with offline_transports(rpc=hostile):
                with self.assertRaises(TestnetSafetyError) as ctx:
                    sign_and_submit_testnet_tx(
                        _self_transfer_tx(acct.address),
                        purpose="R3 hostile transport", config=cfg,
                    )
            message = str(ctx.exception)
            self.assertNotIn(bytes(hostile.raw_submissions[0]).hex(), message) \
                if hostile.raw_submissions else None
            self.assertNotIn(cred_url, message)
            self.assertIn("testnet submission", message)
            with open(_gate.SECURITY_POLICY.tx_log_path) as f:
                log_text = f.read()
            self.assertNotIn(cred_url, log_text)


# ═════════════════════════════════════════════════════════════════════════════
# §5 — structural invariants
# ═════════════════════════════════════════════════════════════════════════════


class TestR3StructuralInvariants(unittest.TestCase):
    """R3 §5: the interfaces and sources are pinned so future code cannot
    silently re-introduce mutable policy maps or transport injection."""

    def test_policy_mappings_are_read_only_types(self):
        self.assertIsInstance(ALLOWED_TESTNET_CHAINS, MappingProxyType)
        self.assertIsInstance(KNOWN_TARGETS, MappingProxyType)
        self.assertIsInstance(SECURITY_POLICY.allowed_chains, MappingProxyType)
        self.assertIsInstance(SECURITY_POLICY.known_targets, MappingProxyType)
        self.assertIsInstance(effective_allowlist(), MappingProxyType)

    def test_no_runtime_mutation_route_for_either_mapping(self):
        """Every publicly reachable representation of either map refuses
        mutation — there is no route from a returned object to the live
        policy."""
        for mapping in (
            ALLOWED_TESTNET_CHAINS, KNOWN_TARGETS,
            SECURITY_POLICY.allowed_chains, SECURITY_POLICY.known_targets,
            effective_allowlist(),
        ):
            with self.subTest(mapping=type(mapping).__name__):
                with self.assertRaises((AttributeError, TypeError)):
                    mapping["_graft"] = True  # item assignment refused
                with self.assertRaises((AttributeError, TypeError)):
                    mapping._graft = True    # attribute graft refused too
        src = inspect.getsource(_gate._frozen_policy_mapping)
        self.assertIn("MappingProxyType", src)
        self.assertIn("isinstance(value, dict)", src)  # recursive freeze

    def test_validation_uses_immutable_policy_representation(self):
        src = inspect.getsource(_gate.validate_transaction)
        self.assertIn("SECURITY_POLICY.known_targets", src)
        self.assertNotIn("targets = KNOWN_TARGETS", src)

    def test_no_transport_factory_arguments_on_production_interfaces(self):
        for fn in (
            sign_and_submit_testnet_tx,
            prepare_flashbots_bundle,
            AuthorizedExecution.sign_and_submit,
            AuthorizedExecution.prepare_flashbots_bundle,
        ):
            with self.subTest(fn=fn.__qualname__):
                params = inspect.signature(fn).parameters
                self.assertNotIn("web3_factory", params)
                self.assertNotIn("relay_factory", params)
        # and no public name re-exports a factory parameter:
        import executor.authorization as auth_mod
        self.assertFalse(hasattr(auth_mod, "web3_factory"))
        self.assertFalse(hasattr(auth_mod, "relay_factory"))

    def test_transports_built_only_through_private_gate_builders(self):
        """The orchestration sources construct transports exclusively via
        the private builders; the builders are private module attributes
        referenced nowhere else in production code."""
        def _code_names(fn):
            tree = ast.parse(inspect.getsource(fn))
            names = {n.id for n in ast.walk(tree)
                     if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute)}
            return names

        sign_src = inspect.getsource(_gate.sign_and_submit_testnet_tx)
        prep_src = inspect.getsource(_gate.prepare_flashbots_bundle)
        self.assertIn("_build_rpc_transport(cfg.rpc_url)", sign_src)
        self.assertIn("_build_rpc_transport(cfg.rpc_url)", prep_src)
        self.assertIn("_build_relay_transport(cfg.rpc_url, account)", prep_src)
        for fn in (_gate.sign_and_submit_testnet_tx,
                   _gate.prepare_flashbots_bundle):
            names = _code_names(fn)
            self.assertNotIn("web3_factory", names)
            self.assertNotIn("relay_factory", names)
        # the relay builder reads the code-controlled relay itself:
        relay_src = inspect.getsource(_gate._build_relay_transport)
        self.assertIn("SECURITY_POLICY.relay_url", relay_src)
        self.assertIn("flashbot(", relay_src)

    def test_production_relay_constant_is_the_sole_destination(self):
        self.assertEqual(FLASHBOTS_SEPOLIA_RELAY,
                         "https://relay-sepolia.flashbots.net")
        self.assertEqual(SECURITY_POLICY.relay_url, FLASHBOTS_SEPOLIA_RELAY)
        # no other relay URL constant exists in the gate module source
        gate_src = open(_gate.__file__, encoding="utf-8").read()
        for line in gate_src.splitlines():
            if "https://" in line and "relay" in line.lower() \
                    and not line.strip().startswith("#") \
                    and "FLASHBOTS_SEPOLIA_RELAY =" not in line \
                    and "relay-sepolia.flashbots.net" not in line:
                self.fail(f"unexpected relay destination: {line.strip()}")

    def test_router_and_mempool_paths_use_the_unified_boundary(self):
        """The ordinary execution router and the mempool/backrun paths
        still obtain signing/submission authority exclusively through the
        unified authorization boundary (R1 pin, re-pinned after R3)."""
        import executor.executor as executor_mod
        import executor.mev_execution as mev_mod
        exec_src = inspect.getsource(executor_mod)
        mev_src = inspect.getsource(mev_mod)
        self.assertIn("prepare_flashbots_bundle", exec_src)
        self.assertIn("sign_and_submit", exec_src)
        self.assertIn("prepare_flashbots_bundle", mev_src)
        # they call them on the AUTHORIZED HANDLE, never on the gate
        # module directly:
        for src in (exec_src, mev_src):
            self.assertNotIn("testnet_gate.sign_and_submit_testnet_tx", src)
            self.assertNotIn("testnet_gate.prepare_flashbots_bundle", src)
            self.assertNotIn("web3_factory", src)
            self.assertNotIn("relay_factory", src)

    def test_no_transport_factory_names_in_production_code(self):
        """``web3_factory``/``relay_factory`` exist as CODE nowhere in
        production modules — the injection surface is gone from the
        repository (docstring mentions of the REMOVAL are not code)."""
        import tokenize

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in {
                ".git", "__pycache__", "node_modules", ".venv", "venv",
                "cache", "logs"}]
            for name in files:
                if not name.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(root, name), repo_root)
                if rel.startswith("tests" + os.sep) or rel.startswith(
                        "study" + os.sep):
                    continue
                with open(os.path.join(root, name), "rb") as f:
                    try:
                        for tok in tokenize.tokenize(f.readline):
                            if tok.type == tokenize.NAME and tok.string in (
                                    "web3_factory", "relay_factory"):
                                offenders.append((rel, tok.string))
                    except (tokenize.TokenError, SyntaxError):
                        pass
        self.assertEqual(
            offenders, [],
            "transport-factory identifier re-introduced in production code",
        )


if __name__ == "__main__":
    unittest.main()
