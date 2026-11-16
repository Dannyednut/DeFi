"""
tests/test_eng017_profit_floor.py
==================================
DSS-WO-ENG-017 §2 regression coverage — one consistent, token-denominated
contract profit floor for the ordinary AND mempool execution paths.

Policy pinned here (all offline, every boundary mocked):

1. The canonical validated profitability floor
   (``validated_profit_floor_wei``, the OpportunityValidator's quote-
   retention floor) is the authoritative ``ArbData.minProfit`` for BOTH
   ordinary and mempool opportunities.
2. A successful Flashbots simulation NEVER overwrites or weakens that
   floor. ``sim_result["gas_fee_wei"]`` (Flashbots ``gasFees``) is
   NATIVE-currency wei; ``minProfit`` is compared by Exec.sol against
   profit denominated in the borrowed ``tokenIn`` base units
   (contracts/Exec.sol: ``if (profit < arbMem.minProfit || profit == 0)
   revert NoProfit();`` and the matching ``_processProfit`` check). A gas
   figure in native wei is not denominated in the profit token and must
   never be assigned to the guard. Only the gas LIMIT is refined from the
   measured ``gas_used``.
3. Missing-vs-invalid metadata semantics mirror
   ``optimizer.opportunity_economics.enrich``: a key that is GENUINELY
   ABSENT may fall back to the positive validated quote; a key that is
   EXPLICITLY PRESENT as zero, negative or non-numeric is invalid
   evidence and fails closed — nothing is built, signed or submitted.
4. The retained stricter value (detector metadata ``min_profit`` — the
   CycleDetector's estimated gas cost, written in tokenIn units only when
   tokenIn is the wrapped native, else 0) may only RAISE the floor, never
   lower it, and a malformed value is ignored rather than trusted.
5. The floor is denomination-agnostic: six-decimal (USDC-style) and
   eighteen-decimal profit-token floors pass through to the contract
   guard byte-identically, with no 1e18 normalisation anywhere.

The EXECUTE_ONCHAIN master switch is enabled ONLY inside the patched test
environment with every downstream boundary mocked — nothing real is ever
signed or submitted.
"""
import os
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from eth_utils import to_checksum_address

from config import CHAINS
from executor.relay import RelayResult
from executor.executor import ArbExecutor
from logger import Opportunity, OppType
from routing import ArbKind

SEPOLIA = 11155111
FAKE_ARB = to_checksum_address("0x" + "ab" * 20)
FAKE_AGENT = to_checksum_address("0x" + "cd" * 20)
TOKEN_18 = to_checksum_address("0x" + "11" * 20)   # WETH-style, 18 decimals
TOKEN_6 = to_checksum_address("0x" + "12" * 20)    # USDC-style, 6 decimals
TOKEN_B = to_checksum_address("0x" + "22" * 20)
POOL_1 = to_checksum_address("0x" + "33" * 20)
POOL_2 = to_checksum_address("0x" + "44" * 20)

FLOOR_18 = 1_234_567 * 10 ** 12          # 18-decimal floor (1.234567e6 scaled)
FLOOR_6 = 1_234_567                      # 6-decimal floor (raw base units)


def _ad_tuple(token_in: str, amount_in: int) -> tuple:
    """A yieldOut-shaped ArbData tuple for a 2-pool route (the ordinary
    path uses its sorted fields directly)."""
    return (
        amount_in,
        0,
        [token_in, TOKEN_B, token_in],
        [POOL_1, POOL_2],
        [3000, 3000],
        token_in,
        0,
    )


def _opp(*, is_mempool: bool, token_in: str, floor: int | None = None,
         quote: int | None = None, extra_meta: dict | None = None,
         simulation_profit: int = 0) -> Opportunity:
    meta: dict = {
        "is_mempool": is_mempool,
        "detector": "mempool_backrun" if is_mempool else "cycles",
        "arb_kind": (ArbKind.BACKRUN if is_mempool else ArbKind.CYCLE).value,
        "execution_simulated": True,
        "execution_ready": True,
        "execution_supported": True,
        "root_token": token_in,
        "fees_pip": [3000, 3000],
        "gas_units": 300_000,
        "economics_profitable": True,
    }
    if floor is not None:
        meta["validated_profit_floor_wei"] = floor
    if quote is not None:
        meta["quote_profit_wei"] = quote
    if is_mempool:
        meta["trigger_tx"] = "0x" + "ee" * 32
    else:
        # 2-pool ordinary candidates carry the sorted yieldOut ArbData.
        meta["arb_data"] = _ad_tuple(token_in, 10 ** 18)
        meta["profit_wei"] = (quote or floor or simulation_profit or 0)
        meta["min_profit"] = 0
    if extra_meta:
        meta.update(extra_meta)
    return Opportunity(
        opp_type=OppType.DEX_SPREAD,
        chain_id=SEPOLIA,
        arb_kind=ArbKind.BACKRUN if is_mempool else ArbKind.CYCLE,
        block_number=100,
        tokens=[token_in, TOKEN_B, token_in],
        pools=[POOL_1, POOL_2],
        fees=[3000, 3000],
        dexes=["uniswap_v3", "sushiswap_v3"],
        estimated_profit_usd=1.5,
        net_profit_usd=1.2,
        gas_cost_usd=0.3,
        simulation_profit_wei=simulation_profit,
        metadata=meta,
    )


class _EnabledExecutorTestMixin:
    """Armed executor + ENABLED master switch, every downstream boundary
    (sender lookup, builder, signer, relay, audit sink, lender selection,
    off-chain profit gate) mocked. Nothing real is ever built, signed or
    submitted."""

    def setUp(self):
        self._original = CHAINS[SEPOLIA].arb_exec_address
        CHAINS[SEPOLIA].arb_exec_address = FAKE_ARB
        self.addCleanup(setattr, CHAINS[SEPOLIA], "arb_exec_address", self._original)

        self._env = patch.dict(os.environ, {"EXECUTE_ONCHAIN": "true"})
        self._env.start()
        self.addCleanup(self._env.stop)

        audit_patcher = patch("executor.executor.execution_audit.record_execution",
                              MagicMock())
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        self.get_sender = patch("executor.executor.get_sender",
                                return_value=FAKE_AGENT)
        self.get_sender.start()
        self.addCleanup(self.get_sender.stop)

        build_patcher = patch("executor.executor.build_execute_tx",
                              return_value={"to": FAKE_ARB, "value": 0})
        self.build_tx = build_patcher.start()
        self.addCleanup(build_patcher.stop)

        sign_patcher = patch("executor.executor.sign_transaction",
                             return_value=b"\x01\x02\x03")
        self.sign = sign_patcher.start()
        self.addCleanup(sign_patcher.stop)

        relay_patcher = patch(
            "executor.executor.relay_transaction",
            return_value=RelayResult(success=True, tx_hash="0xabc", method="flashbots"),
        )
        self.relay = relay_patcher.start()
        self.addCleanup(relay_patcher.stop)

        # Ordinary-path dependencies: lender selection and the USD profit
        # gate are policy-neutral for these tests (the floor policy under
        # test is independent of which lender wins and of the USD gate).
        #
        # NOTE: patch(...).start() returns the MOCK, not the patcher — so
        # every cleanup below keeps an explicit reference to the PATCHER
        # (a MagicMock's .stop would be a silent no-op child attribute and
        # the patch would leak into other test modules).
        lender_patcher = patch(
            "executor.executor.select_lender",
            return_value=MagicMock(lender_name="aave", mode=0, force_balancer=True),
        )
        lender_patcher.start()
        self.addCleanup(lender_patcher.stop)

        gate_patcher = patch("executor.executor.is_profitable_after_gas",
                             return_value=True)
        gate_patcher.start()
        self.addCleanup(gate_patcher.stop)

        # Batch preparation converts the borrow amount through the first
        # pool; policy-neutral for these tests.
        amount_patcher = patch("executor.executor._get_amount_out",
                               return_value=10 ** 18)
        amount_patcher.start()
        self.addCleanup(amount_patcher.stop)

        self.executor = ArbExecutor(MagicMock(), chain_id=SEPOLIA)
        self.executor.use_flashbots = False


class TestOrdinaryPathValidatedFloor(_EnabledExecutorTestMixin, unittest.TestCase):
    """Ordinary opportunities get the SAME validated-floor protection as
    mempool backruns (DSS-WO-ENG-017 §2.2)."""

    def test_floor_is_the_contract_guard_on_the_ordinary_path(self):
        result = self.executor.submit(_opp(is_mempool=False, token_in=TOKEN_18,
                                           floor=FLOOR_18, quote=int(FLOOR_18 * 1.1)))
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], FLOOR_18)

    def test_floor_genuinely_absent_falls_back_to_positive_quote(self):
        opp = _opp(is_mempool=False, token_in=TOKEN_18, quote=999_999)
        result = self.executor.submit(opp)
        self.assertIsNotNone(result)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], 999_999)

    def test_explicit_zero_floor_fails_closed_on_the_ordinary_path(self):
        """An explicitly present zero floor is INVALID evidence, not a
        missing one — it must not silently fall back to the quote."""
        opp = _opp(is_mempool=False, token_in=TOKEN_18, floor=0, quote=999_999)
        self.assertIsNone(self.executor.submit(opp))
        self.build_tx.assert_not_called()
        self.sign.assert_not_called()
        self.relay.assert_not_called()

    def test_negative_floor_fails_closed_on_the_ordinary_path(self):
        opp = _opp(is_mempool=False, token_in=TOKEN_18, floor=-7, quote=999_999)
        self.assertIsNone(self.executor.submit(opp))
        self.relay.assert_not_called()

    def test_non_numeric_floor_fails_closed(self):
        opp = _opp(is_mempool=False, token_in=TOKEN_18, floor="not-a-number")
        self.assertIsNone(self.executor.submit(opp))
        self.relay.assert_not_called()

    def test_no_evidence_at_all_fails_closed(self):
        """No floor, no quote, no simulation profit — fail closed."""
        opp = _opp(is_mempool=False, token_in=TOKEN_18)
        self.assertIsNone(self.executor.submit(opp))
        self.relay.assert_not_called()

    def test_simulation_profit_is_the_last_evidence_tier(self):
        """With floor and quote genuinely absent, the detector's own
        token-denominated simulation profit is the last tier of the
        enrichment chain — used as the floor when positive."""
        opp = _opp(is_mempool=False, token_in=TOKEN_18, simulation_profit=555_555)
        result = self.executor.submit(opp)
        self.assertIsNotNone(result)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], 555_555)

    def test_stricter_meta_minimum_is_retained_but_never_lowers_floor(self):
        """The CycleDetector's gas-cost ``min_profit`` (tokenIn units when
        tokenIn is the wrapped native) may only raise the contract guard;
        a smaller value never weakens the validated floor."""
        opp = _opp(is_mempool=False, token_in=TOKEN_18, floor=FLOOR_18,
                   extra_meta={"min_profit": FLOOR_18 * 2})
        result = self.executor.submit(opp)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], FLOOR_18 * 2)

        opp = _opp(is_mempool=False, token_in=TOKEN_18, floor=FLOOR_18,
                   extra_meta={"min_profit": 1})
        result = self.executor.submit(opp)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], FLOOR_18)

    def test_malformed_meta_minimum_is_ignored_not_trusted(self):
        opp = _opp(is_mempool=False, token_in=TOKEN_18, floor=FLOOR_18,
                   extra_meta={"min_profit": "garbage"})
        result = self.executor.submit(opp)
        self.assertIsNotNone(result)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], FLOOR_18)


class TestFlashbotsSimulationFloorIntegrity(_EnabledExecutorTestMixin, unittest.TestCase):
    """A successful Flashbots simulation never overwrites or weakens the
    validated floor — on either path (DSS-WO-ENG-017 §2.3/§2.4)."""

    def _sim(self, gas_fee_wei: int) -> dict:
        return {
            "success": True,
            "gas_used": 210_000,
            "gas_fee_wei": gas_fee_wei,
            "target_block": 101,
            "target_raw_tx": None,
        }

    def test_ordinary_path_simulation_keeps_validated_floor(self):
        self.executor.use_flashbots = True
        # A mainnet-scale gas fee that would DWARF the floor if the old
        # gas_fee*1.2 overwrite were still in place.
        with patch.object(self.executor, "execution_simulation",
                          return_value=self._sim(10 ** 16)):
            result = self.executor.submit(_opp(is_mempool=False, token_in=TOKEN_18,
                                               floor=FLOOR_18))
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], FLOOR_18)
        # Only the gas LIMIT is refined from the simulation.
        self.assertEqual(self.build_tx.call_args.kwargs["gas_units"], 210_000)

    def test_mempool_path_simulation_keeps_validated_floor(self):
        self.executor.use_flashbots = True
        # A tiny gas fee that would WEAKEN the floor below the validated
        # minimum if the old overwrite were still in place.
        with patch.object(self.executor, "execution_simulation",
                          return_value=self._sim(1)):
            result = self.executor.submit(_opp(is_mempool=True, token_in=TOKEN_18,
                                               floor=FLOOR_18))
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], FLOOR_18)
        self.assertEqual(self.build_tx.call_args.kwargs["gas_units"], 210_000)


class TestTokenDenominationAgnosticism(_EnabledExecutorTestMixin, unittest.TestCase):
    """The floor is passed through in the profit token's own base units —
    six-decimal and eighteen-decimal tokens alike (DSS-WO-ENG-017 §2)."""

    def test_six_decimal_profit_token_floor_passthrough(self):
        result = self.executor.submit(_opp(is_mempool=True, token_in=TOKEN_6,
                                           floor=FLOOR_6))
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], FLOOR_6)
        self.assertEqual(arb_data["tokenIn"], TOKEN_6)
        # No 1e18 rescaling happened anywhere in the pipeline.
        self.assertEqual(result.profit_wei, FLOOR_6)

    def test_eighteen_decimal_profit_token_floor_passthrough(self):
        result = self.executor.submit(_opp(is_mempool=True, token_in=TOKEN_18,
                                           floor=FLOOR_18))
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], FLOOR_18)
        self.assertEqual(arb_data["tokenIn"], TOKEN_18)

    def test_batch_preparation_carries_the_same_floor(self):
        prepared = self.executor._execute(
            _opp(is_mempool=False, token_in=TOKEN_6, floor=FLOOR_6),
            for_batch=True,
        )
        arb_data, gas_units, gas_usd, tip_bps = prepared
        self.assertEqual(arb_data["minProfit"], FLOOR_6)


class TestMempoolMissingVersusInvalid(_EnabledExecutorTestMixin, unittest.TestCase):
    """Executor-side missing-vs-invalid parity with
    optimizer.opportunity_economics.enrich for promoted candidates."""

    def test_explicit_zero_floor_does_not_fall_back_to_quote(self):
        """The old ``or``-chain treated an explicit zero floor as missing
        and silently fell back to the quote — pinned as forbidden."""
        opp = _opp(is_mempool=True, token_in=TOKEN_18, floor=0, quote=999_999)
        self.assertIsNone(self.executor.submit(opp))
        self.build_tx.assert_not_called()
        self.sign.assert_not_called()
        self.relay.assert_not_called()

    def test_floor_genuinely_absent_uses_positive_quote(self):
        meta = _opp(is_mempool=True, token_in=TOKEN_18, quote=888_888).metadata
        meta.pop("validated_profit_floor_wei", None)
        opp = _opp(is_mempool=True, token_in=TOKEN_18)
        opp.metadata = dict(meta)
        result = self.executor.submit(opp)
        self.assertIsNotNone(result)
        arb_data = self.build_tx.call_args.kwargs["arb_data"]
        self.assertEqual(arb_data["minProfit"], 888_888)


if __name__ == "__main__":
    unittest.main()
