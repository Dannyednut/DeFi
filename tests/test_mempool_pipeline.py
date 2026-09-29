"""
Mempool backrun pipeline test: a pending swap moves a pool's price ->
MempoolSimulator detects the resulting arbitrage -> promotion to a canonical
Opportunity -> validation -> economics -> execution submission.

This is the counterpart to test_full_pipeline_end_to_end (CycleDetector) for
the mempool/backrun detection path, exercising the real code at every stage
with math cross-checked against an independent reference computation.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import MagicMock, patch
from eth_utils import to_checksum_address


def v2_out(amount_in, r_in, r_out, fee_bps=30):
    fee_num = 10_000 - fee_bps
    aif = amount_in * fee_num
    return aif * r_out // (r_in * 10_000 + aif)


def test_mempool_backrun_pipeline_end_to_end():
    W = 10**18
    WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2".lower()
    USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower()
    DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F".lower()
    POOL_A = to_checksum_address("0x" + "a1" * 20).lower()  # WETH/USDC
    POOL_B = to_checksum_address("0x" + "b2" * 20).lower()  # USDC/DAI
    POOL_C = to_checksum_address("0x" + "c3" * 20).lower()  # DAI/WETH

    # ── Stage 0: a perfectly balanced triangle -- NO static arbitrage ──────
    from graph import TokenGraph, PoolEdge

    g = TokenGraph(w3=None, chain_id=1)
    edges = [
        PoolEdge(POOL_A, "UniswapV2", WETH, USDC, 1000 * W, 2_000_000 * W, 30, 2,
                 tvl_usd=4_000_000, protocol="uniswap_v2", pool_token0=WETH, pool_token1=USDC, chain_id=1),
        PoolEdge(POOL_A, "UniswapV2", USDC, WETH, 2_000_000 * W, 1000 * W, 30, 2,
                 tvl_usd=4_000_000, protocol="uniswap_v2", pool_token0=WETH, pool_token1=USDC, chain_id=1),
        PoolEdge(POOL_B, "UniswapV2", USDC, DAI, 1_000_000 * W, 1_000_000 * W, 30, 2,
                 tvl_usd=2_000_000, protocol="uniswap_v2", pool_token0=USDC, pool_token1=DAI, chain_id=1),
        PoolEdge(POOL_B, "UniswapV2", DAI, USDC, 1_000_000 * W, 1_000_000 * W, 30, 2,
                 tvl_usd=2_000_000, protocol="uniswap_v2", pool_token0=USDC, pool_token1=DAI, chain_id=1),
        PoolEdge(POOL_C, "UniswapV2", DAI, WETH, 2_000_000 * W, 1000 * W, 30, 2,
                 tvl_usd=4_000_000, protocol="uniswap_v2", pool_token0=DAI, pool_token1=WETH, chain_id=1),
        PoolEdge(POOL_C, "UniswapV2", WETH, DAI, 1000 * W, 2_000_000 * W, 30, 2,
                 tvl_usd=4_000_000, protocol="uniswap_v2", pool_token0=DAI, pool_token1=WETH, chain_id=1),
    ]
    g.upsert_pool_edges(edges)
    assert len(g.find_cycles(source_tokens=[WETH], max_hops=3, min_profit_ratio=0.0001)) == 0, \
        "test setup should have NO static arbitrage before the pending swap"

    # ── Stage 1: a large pending swap arrives, moving Pool A's price ───────
    from mempool.decoder import DecodedSwap
    from mempool.simulator import MempoolSimulator

    pending_amount_in = 50 * W
    swap = DecodedSwap(
        tx_hash="0x" + "aa" * 32, router="0x" + "aa" * 20, router_name="UniswapV2", version=2,
        function_name="swapExactTokensForTokens", token_path=[WETH, USDC],
        amount_in=pending_amount_in, amount_out_min=0, amount_in_is_exact=True,
        sender="0x" + "ff" * 20, gas_price=20_000_000_000,
    )

    r_in_before, r_out_before = 1000 * W, 2_000_000 * W
    usdc_out = v2_out(pending_amount_in, r_in_before, r_out_before)
    r_in_after_ref = r_in_before + pending_amount_in
    r_out_after_ref = r_out_before - usdc_out

    sim = MempoolSimulator(graph=g, native_price_usd=2000.0)
    with patch("mempool.simulator.token_price", side_effect=lambda a: 2000.0 if a.lower() == WETH else None), \
         patch("mempool.simulator.token_decimals", return_value=18), \
         patch("tvl.estimate_tvl_from_edge", return_value=4_000_000.0):
        opps = sim.simulate(swap)

    real_opp = next((o for o in opps if o.opp_type.startswith("VERIFIED_")), None)
    assert real_opp is not None, (
        f"expected a VERIFIED_* backrun opportunity, got types: {[o.opp_type for o in opps]} -- "
        "this is exactly the scenario the DFS pruning bug (fixed in graph.py) used to silently miss"
    )
    assert abs(real_opp.reserve_in_after - r_in_after_ref) < 2
    assert abs(real_opp.reserve_out_after - r_out_after_ref) < 2
    assert real_opp.estimated_profit_ratio > 0.05, "expected a substantial (~9%) real profit ratio"

    # ── Stage 2: promote to canonical Opportunity, validate, apply economics ─
    from validation.opportunity import OpportunityValidator
    from optimizer.opportunity_economics import enrich, EconomicsConfig

    tracker_opp = real_opp.to_opportunity(chain_id=1, block_number=101, graph=g)
    assert tracker_opp.metadata["execution_supported"] is True

    # Scale to a $2000-notional trade (a downstream sizing optimizer's job in
    # the real pipeline; the detector's own probe is deliberately tiny) using
    # the exact same AMM formula, so validation exercises a size that can
    # actually clear real gas costs.
    usdc_in_scaled = 2000 * W
    weth_leg = v2_out(usdc_in_scaled, r_out_after_ref, r_in_after_ref)  # USDC -> WETH (Pool A, post-swap, reverse)
    dai_leg = v2_out(weth_leg, 1000 * W, 2_000_000 * W)                # WETH -> DAI (Pool C, unaffected)
    usdc_leg = v2_out(dai_leg, 1_000_000 * W, 1_000_000 * W)           # DAI -> USDC (Pool B, unaffected)
    contract_quote_profit = usdc_leg - usdc_in_scaled
    assert contract_quote_profit > 0

    ARB_EXEC = to_checksum_address("0x" + "ee" * 20)
    arb_data_tuple = (usdc_in_scaled, 0,
                       [to_checksum_address(t) for t in tracker_opp.tokens],
                       [to_checksum_address(p) for p in tracker_opp.pools],
                       [30, 30, 30], to_checksum_address(tracker_opp.tokens[0]), 0)
    fake_contract = MagicMock()
    fake_contract.functions.yieldOut.return_value.call.return_value = (arb_data_tuple, contract_quote_profit)
    fake_contract.functions.swap.return_value.build_transaction.return_value = {"data": "0x", "to": ARB_EXEC}

    w3v = MagicMock()
    w3v.eth.chain_id = 1
    w3v.to_checksum_address = lambda a: to_checksum_address(a)
    w3v.eth.contract.return_value = fake_contract
    w3v.eth.get_block.return_value = {"number": 101}
    w3v.eth.block_number = 101
    w3v.eth.estimate_gas.return_value = 320_000
    w3v.eth.call.return_value = b""

    with patch("validation.opportunity.get_extended_arb_exec_address", return_value=ARB_EXEC), \
         patch("validation.opportunity.get_chain") as mock_get_chain, \
         patch("executor.signer.get_sender", return_value=to_checksum_address("0x" + "11" * 20)):
        mock_get_chain.return_value = MagicMock(arb_exec_address=ARB_EXEC)
        validator = OpportunityValidator(w3v, chain_id=1)
        result = validator.validate(tracker_opp, block_number=101)

    assert result.ok is True
    assert result.quote_profit_wei == contract_quote_profit

    tracker_opp.metadata["validated_profit_floor_wei"] = result.validated_floor_wei
    tracker_opp.metadata["root_token"] = tracker_opp.tokens[0]
    tracker_opp.metadata["gas_units"] = result.gas_estimate
    tracker_opp.metadata["gas_price_wei"] = swap.gas_price

    with patch("optimizer.opportunity_economics.token_price",
               side_effect=lambda a: 1.0 if a.lower() in (USDC, DAI) else 2000.0), \
         patch("optimizer.opportunity_economics.token_decimals", return_value=18):
        enriched = enrich(tracker_opp, cfg=EconomicsConfig(execution_buffer_bps=300), native_price_usd=2000.0)

    assert enriched.metadata["economics_profitable"] is True
    assert enriched.net_profit_usd > 5

    # ── Stage 3: execution submission through the real ExecutionRouter ─────
    from execution_router import create_router, OppSource
    from tracker import OpportunityTracker
    from logger import OppLogger
    import tempfile
    import time as _t

    logger_obj = OppLogger(log_dir=tempfile.mkdtemp())
    tracker = OpportunityTracker(logger_obj)
    tracker.process_block(101, [enriched], filled_pools={})

    fake_executor = MagicMock()
    fake_executor.chain_id = 1
    fake_executor._arb_exec = MagicMock()
    exec_result = MagicMock()
    exec_result.success = True
    exec_result.tx_hash = "0x" + "cd" * 32
    exec_result.profit_wei = result.quote_profit_wei
    exec_result.gas_cost_usd = 10.0
    exec_result.lender = "balancer"
    exec_result.method = "swap"
    exec_result.block_number = 101
    fake_executor.submit.return_value = exec_result

    router = create_router(tracker=tracker, executor=fake_executor, config={"EXECUTE_ONCHAIN": True})
    enriched.metadata["execution_simulated"] = True
    assert router.submit_opportunity(enriched, OppSource.BACKRUN, priority=2.0) is True

    router.start()
    _t.sleep(0.3)
    router.process_results()

    assert fake_executor.submit.called, "the router never invoked the executor"
    submitted = fake_executor.submit.call_args[0][0]
    assert submitted.tokens == enriched.tokens
    assert submitted.metadata.get("is_mempool") is True
