from mev.policy import ChainMEVPolicy


def test_target_block_policy():
    p = ChainMEVPolicy.for_chain(1)
    p.validate_target(100, 101)
    try:
        p.validate_target(100, 104)
    except ValueError:
        pass
    else:
        raise AssertionError("stale/far target block accepted")


# DSS-WO-ENG-016 §4: the retired alternative MEV bundle models/orchestration
# tests were removed with their modules. ChainMEVPolicy (retained — the relay
# consumes it at private-submission time) is covered by
# test_target_block_policy above and tests/test_phase6_execution.py.

# DSS-WO-ENG-017 §3D: mempool/reorg.py (TriggerTracker) was retired — it had
# no production caller. The canonical pipeline already handles trigger
# replacement and reorg behaviour without it:
#   * mempool/outcome.py resolves every pending trigger honestly against the
#     mined block (mined → receipt-based outcome; not mined → aged out as
#     "dropped" after MAX_PENDING_BLOCKS), and a REPLACED trigger tx re-enters
#     the pipeline as a fresh pending transaction through the watcher
#     (new hash → decoded → simulated → possibly promoted);
#   * a promoted backrun's validity is enforced fail-closed at the execution
#     boundary (execution_simulation fetches the trigger tx and includes its
#     raw payload in the bundle — a replaced/dropped trigger fails the
#     simulation or the bundle is simply not included), not by a separate
#     bookkeeping tracker.
# See docs/DEPENDENCIES.md §11 for the full disposition table.
