from execution.route_ir import ExecutionPlan, ExecutionLeg, ExecutionKind


def test_execution_plan_rejects_empty_legs():
    try:
        ExecutionPlan(1, '0xA', 1, (), 1)
    except ValueError as e:
        assert 'at least one leg' in str(e)
    else:
        raise AssertionError('expected validation failure')


def test_phase11_module_is_importable_without_constructing_web3():
    import execution.atomic as atomic
    assert hasattr(atomic, 'AtomicExecutionService')
    assert hasattr(atomic, 'AtomicEnvelope')
    assert hasattr(atomic, 'AtomicPreflight')
