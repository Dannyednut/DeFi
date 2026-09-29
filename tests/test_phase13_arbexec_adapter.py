from execution.arbexec_adapter import build_arbexec_route
from execution.route_ir import ExecutionKind, ExecutionLeg, ExecutionPlan


class FakeEth:
    chain_id = 1


class FakeW3:
    eth = FakeEth()

    @staticmethod
    def to_checksum_address(value):
        return value


def plan_for(*legs, amount=1000, min_profit=7):
    return ExecutionPlan(
        1, "A", amount, tuple(legs), 999, min_profit=min_profit
    )


def leg(kind, protocol, pool, a, b, fee_bps=30):
    return ExecutionLeg(1, kind, protocol, "dex", pool, a, b, 100, fee_bps=fee_bps)


def test_build_native_arbexec_route_mode0():
    p = plan_for(
        leg(ExecutionKind.V2_SWAP, "uniswap_v2", "P1", "A", "B"),
        leg(ExecutionKind.V3_SWAP, "uniswap_v3", "P2", "B", "A", 5),
    )
    r = build_arbexec_route(p, FakeW3(), mode=0)
    assert r.amount_in == 1000
    assert r.min_profit == 7
    assert r.mode == 0
    assert r.tokens == ("A", "B", "A")
    assert r.fees == (3000, 500)


def test_build_native_arbexec_route_mode1_uses_borrowed_amount():
    p = plan_for(
        leg(ExecutionKind.V2_SWAP, "uniswap_v2", "P1", "A", "B"),
        leg(ExecutionKind.V2_SWAP, "uniswap_v2", "P2", "B", "A"),
    )
    r = build_arbexec_route(p, FakeW3(), mode=1, borrowed_amount=777)
    assert r.amount_in == 777
    assert r.mode == 1


def test_reject_non_native_protocol():
    p = plan_for(
        leg(ExecutionKind.CURVE_EXCHANGE, "curve", "P1", "A", "B"),
        leg(ExecutionKind.V2_SWAP, "uniswap_v2", "P2", "B", "A"),
    )
    try:
        build_arbexec_route(p, FakeW3(), mode=0)
    except ValueError as exc:
        assert "cannot execute" in str(exc)
    else:
        raise AssertionError("Curve route must not be sent through ArbExec")


def test_accept_common_25bps_v2():
    p = plan_for(
        leg(ExecutionKind.V2_SWAP, "uniswap_v2", "P1", "A", "B", 25),
        leg(ExecutionKind.V2_SWAP, "uniswap_v2", "P2", "B", "A", 30),
    )
    r = build_arbexec_route(p, FakeW3(), mode=0)
    assert r.fees == (2500, 3000)


def test_arbexec_abi_matches_supplied_four_argument_swap():
    from config import ARB_EXEC_ABI
    swap = next(x for x in ARB_EXEC_ABI if x.get("name") == "swap")
    assert [x["name"] for x in swap["inputs"]] == [
        "arb", "lender", "builderFeeBps", "validUntilBlock"
    ]
