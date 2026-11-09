"""
tests/test_eng016_backrun_correctness.py
=========================================
DSS-WO-ENG-016 regression coverage — canonical runtime path correctness
for mempool/backrun execution.

§3A Mempool execution profit handling
  A promoted mempool candidate (PendingOpportunity.to_opportunity() →
  OpportunityEngine.process_candidates → ExecutionRouter → ArbExecutor)
  carries NO detector economics in its metadata: the authoritative
  validated profit is stamped by the canonical pipeline
  (validated_profit_floor_wei, with quote_profit_wei as the only
  fallback — the same precedence the canonical economics enrichment
  uses). Pinned here:
  - the validated floor is used consistently (logging, result
    construction AND the contract-level minProfit guard);
  - a candidate without a positive validated profit fails closed
    (no invented fallback, no silent zero, nothing built/signed/
    submitted);
  - a successful Flashbots simulation does NOT overwrite or weaken the
    validated floor (DSS-WO-ENG-017 §2 — the simulation's gas_fee_wei
    is native-currency wei while minProfit is tokenIn-denominated, and
    a successful simulation of the real contract already proves the
    simulated profit reaches the floor);
  - the batch-preparation shape of the skip stays unpack-safe.

§3B Preflight skip and audit handling
  _execute() intentionally returns None for ordinary preflight skips;
  submit() then audits the outcome. Pinned here, in an ENABLED
  execution context with every dependency mocked (no real signing, no
  real submission, no real audit file):
  - an ordinary skip must not raise and is audited as the distinct
    outcome "skipped";
  - refused / failed / successful attempts are audited as "refused" /
    "failed" / "success" — the four outcome categories are never
    conflated;
  - an audit write failure never masks the actual execution outcome.

All tests are fully offline: mock Web3 objects, patched
builder/signer/relay boundaries and a patched audit sink. The
EXECUTE_ONCHAIN master switch is enabled ONLY inside the patched test
environment with every downstream boundary mocked — nothing real is
ever signed or submitted.
"""
import os
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from eth_utils import to_checksum_address

from config import CHAINS
from executor.errors import ExecutionRefused
from executor.relay import RelayResult
from executor.executor import ArbExecutor, ExecutionResult
from logger import Opportunity, OppType
from routing import ArbKind

SEPOLIA = 11155111
FAKE_ARB = to_checksum_address("0x" + "ab" * 20)
FAKE_AGENT = to_checksum_address("0x" + "cd" * 20)
TOKEN = to_checksum_address("0x" + "11" * 20)
TOKEN_B = to_checksum_address("0x" + "22" * 20)
POOL_1 = to_checksum_address("0x" + "33" * 20)
POOL_2 = to_checksum_address("0x" + "44" * 20)
TRIGGER_TX = "0x" + "ee" * 32

VALIDATED_FLOOR_WEI = 1_234_567
VALIDATED_QUOTE_WEI = 1_371_741  # floor is 90% of the quote


def _mempool_opp(metadata: dict | None = None) -> Opportunity:
    """A promoted mempool backrun candidate as the canonical pipeline
    delivers it to the executor (validation/enrichment metadata merged
    onto the raw to_opportunity() shape)."""
    meta = {
        "trigger_tx": TRIGGER_TX,
        "impact_pct": 3.2,
        "is_mempool": True,
        "detector": "mempool_backrun",
        "arb_kind": ArbKind.BACKRUN.value,
        "execution_simulated": True,
        "execution_ready": True,
        "execution_supported": True,
        "validated_profit_floor_wei": VALIDATED_FLOOR_WEI,
        "quote_profit_wei": VALIDATED_QUOTE_WEI,
        "validated_gas_units": 260_000,
        "validated_lender_mode": 1,
        "validated_lender": "pool_flash_swap",
        "gas_price_wei": 2_000_000_000,
        "economics_profitable": True,
    }
    meta.update(metadata or {})
    return Opportunity(
        opp_type=OppType.DEX_SPREAD,
        chain_id=SEPOLIA,
        arb_kind=ArbKind.BACKRUN,
        block_number=100,
        tokens=[TOKEN, TOKEN_B, TOKEN],
        pools=[POOL_1, POOL_2],
        fees=[3000, 3000],
        dexes=["uniswap_v3", "sushiswap_v3"],
        estimated_profit_usd=1.5,
        net_profit_usd=1.2,
        gas_cost_usd=0.3,
        metadata=meta,
    )


class _EnabledExecutorTestMixin:
    """Armed executor + ENABLED master switch, every downstream boundary
    (sender lookup, builder, signer, relay, audit sink) mocked. Nothing
    real is ever built, signed or submitted."""

    def setUp(self):
        # Arm the executor with a configured arb contract for Sepolia.
        self._original = CHAINS[SEPOLIA].arb_exec_address
        CHAINS[SEPOLIA].arb_exec_address = FAKE_ARB
        self.addCleanup(setattr, CHAINS[SEPOLIA], "arb_exec_address", self._original)

        self._env = patch.dict(os.environ, {"EXECUTE_ONCHAIN": "true"})
        self._env.start()
        self.addCleanup(self._env.stop)

        self.audit_mock = MagicMock()
        patcher = patch("executor.executor.execution_audit.record_execution",
                        self.audit_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.get_sender = patch("executor.executor.get_sender",
                                return_value=FAKE_AGENT)
        self.get_sender.start()
        self.addCleanup(self.get_sender.stop)

        self.build_tx_patch = patch("executor.executor.build_execute_tx",
                                    return_value={"to": FAKE_ARB, "value": 0})
        self.build_tx = self.build_tx_patch.start()
        self.addCleanup(self.build_tx_patch.stop)

        self.sign_patch = patch("executor.executor.sign_transaction",
                                return_value=b"\x01\x02\x03")
        self.sign = self.sign_patch.start()
        self.addCleanup(self.sign_patch.stop)

        self.relay_patch = patch(
            "executor.executor.relay_transaction",
            return_value=RelayResult(success=True, tx_hash="0xabc", method="flashbots"),
        )
        self.relay = self.relay_patch.start()
        self.addCleanup(self.relay_patch.stop)

        self.executor = ArbExecutor(MagicMock(), chain_id=SEPOLIA)
        # Sepolia has no FLASHBOTS_RPCS entry → use_flashbots is False;
        # the simulation-refresh test arms it explicitly with a mocked
        # simulation.
        self.assertFalse(self.executor.use_flashbots)


# ══════════════════════════════════════════════════════════════════════════════
# §3A — mempool execution profit handling
# ══════════════════════════════════════════════════════════════════════════════

class TestMempoolExecutionPreparation(_EnabledExecutorTestMixin, unittest.TestCase):

    def test_validated_floor_used_consistently(self):
        """profit_wei comes from the canonical validated floor and is used
        consistently: result construction AND the contract minProfit."""
        result = self.executor.submit(_mempool_opp())
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        # The validated floor — not the raw quote — is the authoritative
        # profit value of the promoted candidate.
        self.assertEqual(result.profit_wei, VALIDATED_FLOOR_WEI)
        self.assertEqual(result.lender, "pool_flash_swap")
        self.assertEqual(result.amount_in, 0)
        # The built transaction's ArbData carries the floor as the
        # contract-level minProfit guard.
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], VALIDATED_FLOOR_WEI)
        # One sign + one relay, through the single Builder→Signer→Relay path.
        self.sign.assert_called_once()
        self.relay.assert_called_once()

    def test_validated_quote_used_when_floor_genuinely_absent(self):
        """The validated quote is the only fallback — and only when the
        floor key is GENUINELY ABSENT (same missing-vs-invalid precedence
        as the canonical economics enrichment; DSS-WO-ENG-017 §2)."""
        meta = _mempool_opp().metadata
        del meta["validated_profit_floor_wei"]  # key absent, not zero
        opp = _mempool_opp()
        opp.metadata = dict(meta)
        result = self.executor.submit(opp)
        self.assertIsNotNone(result)
        self.assertEqual(result.profit_wei, VALIDATED_QUOTE_WEI)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], VALIDATED_QUOTE_WEI)

    def test_no_validated_profit_fails_closed(self):
        """A mempool candidate without any validated profit value cannot
        proceed: nothing is built, signed or submitted."""
        opp = _mempool_opp({
            "validated_profit_floor_wei": 0,
            "quote_profit_wei": 0,
        })
        self.assertIsNone(self.executor.submit(opp))
        self.build_tx.assert_not_called()
        self.sign.assert_not_called()
        self.relay.assert_not_called()

    def test_missing_validated_profit_keys_fails_closed(self):
        """Raw (unpromoted) mempool metadata — the exact shape
        PendingOpportunity.to_opportunity() emits, with no canonical
        validation fields — fails closed."""
        opp = _mempool_opp({
            "validated_profit_floor_wei": None,
            "quote_profit_wei": None,
        })
        self.assertIsNone(self.executor.submit(opp))
        self.relay.assert_not_called()

    def test_negative_validated_profit_fails_closed(self):
        opp = _mempool_opp({
            "validated_profit_floor_wei": -5,
            "quote_profit_wei": -9,
        })
        self.assertIsNone(self.executor.submit(opp))
        self.relay.assert_not_called()

    def test_floor_never_lowered_by_smaller_detector_minimum(self):
        """If a metadata min_profit were ever smaller than the validated
        floor, the floor wins (the guard is never weakened)."""
        opp = _mempool_opp({"min_profit": 1})
        result = self.executor.submit(opp)
        self.assertIsNotNone(result)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], VALIDATED_FLOOR_WEI)

    def test_batch_preparation_skip_is_unpack_safe(self):
        """The no-validated-profit skip in batch preparation mode returns
        the 4-tuple of Nones build_batch_calls() expects — never a bare
        None that would crash its unpacking."""
        opp = _mempool_opp({
            "validated_profit_floor_wei": 0,
            "quote_profit_wei": 0,
        })
        prepared = self.executor._execute(opp, for_batch=True)
        self.assertEqual(prepared, [None] * 4)

    def test_successful_simulation_never_changes_min_profit(self):
        """With the Flashbots path armed, a successful pre-submission
        simulation does NOT overwrite or weaken the contract guard:
        minProfit stays the canonical validated floor (DSS-WO-ENG-017 §2
        — the simulation's gas_fee_wei is native-currency wei, not
        tokenIn-denominated, so it must never be assigned to minProfit),
        while the measured gas_used still refines the gas-limit estimate."""
        self.executor.use_flashbots = True
        sim = {
            "success": True,
            "gas_used": 240_000,
            "gas_fee_wei": 500_000_000_000_000,  # 0.0005 native wei
            "target_block": 101,
            "target_raw_tx": None,
        }
        with patch.object(self.executor, "execution_simulation",
                          return_value=sim) as sim_mock:
            result = self.executor.submit(_mempool_opp())
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        sim_mock.assert_called_once()
        # The contract guard is UNCHANGED: still the validated floor —
        # never gas_fee_wei * 1.2, never any other simulation figure.
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], VALIDATED_FLOOR_WEI)
        # The gas-limit estimate IS refined from the measured gas_used.
        self.assertEqual(self.build_tx.call_args.kwargs["gas_units"], 240_000)
        # The validated floor remains the reported profit value.
        self.assertEqual(result.profit_wei, VALIDATED_FLOOR_WEI)

    def test_failed_simulation_skips_submission(self):
        """A failed pre-submission simulation is an ordinary preflight
        skip: nothing is signed or submitted."""
        self.executor.use_flashbots = True
        with patch.object(self.executor, "execution_simulation",
                          return_value={"success": False, "error": "revert"}):
            self.assertIsNone(self.executor.submit(_mempool_opp()))
        self.sign.assert_not_called()
        self.relay.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# §3B — preflight skip and audit handling (enabled context, mocked deps)
# ══════════════════════════════════════════════════════════════════════════════

class TestSubmitOutcomeTaxonomyAndAudit(_EnabledExecutorTestMixin, unittest.TestCase):

    def _audit_outcomes(self):
        return [call.kwargs.get("outcome") for call in self.audit_mock.call_args_list]

    def test_ordinary_preflight_skip_never_raises_and_audits_skipped(self):
        """An ordinary preflight skip (profit gate, simulation gate, ...)
        must not cause an AttributeError in the enabled context; it is
        audited as the distinct outcome 'skipped' and returned as None."""
        # An ordinary-path opportunity with no viable amount skips
        # before any execution attempt.
        opp = _mempool_opp({"is_mempool": False, "arb_data": None})
        result = self.executor.submit(opp)
        self.assertIsNone(result)  # skip, not a failure
        self.assertEqual(self._audit_outcomes(), ["skipped"])
        # Nothing downstream was touched.
        self.build_tx.assert_not_called()
        self.sign.assert_not_called()
        self.relay.assert_not_called()

    def test_refused_attempt_audits_refused(self):
        refused = ExecutionResult(
            success=False, tx_hash="", method="refused", lender="",
            amount_in=0, profit_wei=0, gas_cost_usd=0.0, error="policy",
        )
        with patch.object(self.executor, "_execute",
                          side_effect=ExecutionRefused("builder policy")):
            result = self.executor.submit(_mempool_opp())
        self.assertFalse(result.success)
        self.assertEqual(result.method, "refused")
        self.assertEqual(self._audit_outcomes(), ["refused"])
        _ = refused  # shape reference for readability

    def test_failed_attempt_audits_failed(self):
        with patch.object(self.executor, "_execute",
                          side_effect=RuntimeError("boom")):
            result = self.executor.submit(_mempool_opp())
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertIn("boom", result.error)
        self.assertEqual(self._audit_outcomes(), ["failed"])

    def test_successful_attempt_audits_success(self):
        self.assertTrue(self.executor.submit(_mempool_opp()).success)
        self.assertEqual(self._audit_outcomes(), ["success"])

    def test_audit_failure_never_masks_outcome(self):
        """If the audit write itself fails, the actual execution outcome
        is still returned unchanged."""
        self.audit_mock.side_effect = OSError("disk full")
        result = self.executor.submit(_mempool_opp())
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        self.assertEqual(result.tx_hash, "0xabc")

    def test_disabled_switch_keeps_disabled_run_behaviour(self):
        """With the master switch disabled, nothing executes and nothing
        is audited (historical disabled-run behaviour)."""
        with patch.dict(os.environ, {"EXECUTE_ONCHAIN": "false"}):
            result = self.executor.submit(_mempool_opp())
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertEqual(result.method, "refused")
        self.audit_mock.assert_not_called()
        self.sign.assert_not_called()
        self.relay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
