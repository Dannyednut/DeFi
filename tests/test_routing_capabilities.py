from routing import ArbKind, RouteLeg, RoutePlan, execution_capability


def test_native_v2_v3_route_is_executable():
    ok, reason = execution_capability(["uniswap_v2", "uniswap_v3"])
    assert ok is True
    assert "native" in reason


def test_curve_route_is_quote_only_until_executor_exists():
    ok, reason = execution_capability(["curve", "uniswap_v3"])
    assert ok is False
    assert "curve" in reason


def test_route_plan_protocols_are_deduplicated():
    plan = RoutePlan(
        kind=ArbKind.CYCLE,
        chain_id=1,
        legs=(
            RouteLeg("uniswap_v3", "UniswapV3", "0x1", "0xa", "0xb"),
            RouteLeg("curve", "Curve", "0x2", "0xb", "0xa"),
        ),
        amount_in=100,
        expected_amount_out=110,
    )
    assert plan.protocols == ("uniswap_v3", "curve")
    assert plan.gross_profit_wei == 10
