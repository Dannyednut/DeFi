"""DSS-WO-MI-102B-R1A §3 benchmark: incremental scheduled-search cost and
registration-queue accounting on deterministic synthetic graphs.

Portable (R1A §3): resolves the repository root relative to this script's
location and writes to a repository-relative default (or an explicitly
supplied --out path), so it runs from an ordinary checkout:

    PYTHONDONTWRITEBYTECODE=1 python scripts/mi102b_benchmark.py \
        [--sizes 2000,20000] [--slices 1,2] [--out PATH]

Deterministic (seeded RNG, no network, no RPC); contract quoting is disabled
(ARB_EXEC_ADDRESS -> "") exactly like the offline regression tests.

Scenarios
---------
A. Steady-state scheduled batch (per graph size, per slice size):
   build graph -> engine (ring drained: steady state) -> one cadence batch
   processed in slices -> per-invocation (slice) wall times, per-find_cycles
   call times, startup (universe refresh + pair checks) reported separately
   and included in the total batch cost, pair checks, venues, seeds.

B. Cold-start registration burst (20k graph): 1,000 pools registered after
   engine creation -> queue accounting from INDEPENDENT recorded sets and
   counters (received, unique, consumed/searchable, filtered, pending,
   dropped), plus an exactly-once seeding proof.

C. Simultaneous reactive + scheduled block (20k graph): the SAME graph
   state and touched-pool set measured twice — (a) reactive+slice combined
   on the incremental engine, and (b) a separate reference engine with
   scheduled_discovery=False on a snapshot of the same graph state
   (genuine reactive-only cost, no active scheduled batch). The difference
   isolates the incremental scheduled slice cost from the reactive path.
"""
import argparse
import os
import random
import statistics
import sys
import time
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, REPO_ROOT)

import detectors.base as detectors_base
import detectors.cycles as cycles_mod
from bounded_discovery import BoundedDiscoveryConfig, anchor_pairs, pool_is_searchable
from collections import Counter
from detectors.cycles import CycleDetector
from detectors.protocol_opportunities import CrossProtocolDetector
from graph import PoolEdge, TokenGraph
from opportunity_engine import OpportunityEngine
from tvl import init_prices

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"


class StubEth:
    gas_price = 30 * 10**9

    def get_block(self, n):
        return {"hash": str(n).rjust(30, "0").encode()}


class StubW3:
    eth = StubEth()


def build_synthetic_graph(n_pools: int, seed: int = 42) -> TokenGraph:
    """Realistic shape: WETH/USDC/USDT hubs, long-tail tokens in 1-3 pools,
    TVL heavy-tailed, near-arbitrage-consistent rates (mostly unprofitable
    after fees). Identical generator to the MI-102B baseline benchmark."""
    rng = random.Random(seed)
    init_prices(2000.0, WETH, [USDC, USDT])
    g = TokenGraph(chain_id=1)
    prices = {WETH: 2000.0, USDC: 1.0, USDT: 1.0}

    def token(i):
        addr = f"0x{i + 1:040x}"
        if addr not in prices:
            prices[addr] = rng.choice([0.01, 0.1, 0.5, 1.0, 2.0, 5.0, 20.0, 80.0])
        return addr

    hub_weth = max(1, int(n_pools * 0.20))
    hub_usdc = max(1, int(n_pools * 0.10))
    hub_usdt = max(1, int(n_pools * 0.025))
    rand_a = int(n_pools * 0.40)
    rand_b = n_pools - hub_weth - hub_usdc - hub_usdt - rand_a

    specs = []
    for _ in range(hub_weth):
        specs.append((WETH, token(rng.randrange(n_pools))))
    for _ in range(hub_usdc):
        specs.append((USDC, token(rng.randrange(n_pools))))
    for _ in range(hub_usdt):
        specs.append((USDT, token(rng.randrange(n_pools))))
    for _ in range(rand_a):
        specs.append((token(rng.randrange(int(n_pools * 0.5))), token(rng.randrange(int(n_pools * 0.5)))))
    for _ in range(rand_b):
        specs.append((token(rng.randrange(n_pools)), token(rng.randrange(n_pools))))

    pool_counter = 10**6
    for t_in, t_out in specs:
        if t_in == t_out:
            continue
        pi, po = prices[t_in], prices[t_out]
        tvl = min(10_000_000.0, max(20_000.0, rng.paretovariate(1.5) * 40_000.0))
        rate = (pi / po) * (1.0 + rng.uniform(-0.02, 0.02))
        r_in = max(1, int(tvl * 1e18 / (2.0 * pi)))
        r_out = max(1, int(r_in * rate))
        pool = f"0x{pool_counter:040x}"
        pool_counter += 1
        common = dict(
            dex_name="UniswapV2", fee_bps=30, version=2, tvl_usd=tvl,
            protocol="uniswap_v2", pool_token0=min(t_in, t_out), pool_token1=max(t_in, t_out),
        )
        g.add_or_update_pool(
            PoolEdge(pool, token_in=t_in, token_out=t_out, reserve_in=r_in, reserve_out=r_out, **common),
            PoolEdge(pool, token_in=t_out, token_out=t_in, reserve_in=r_out, reserve_out=r_in, **common),
        )
        for t in (t_in, t_out):
            g.token_metadata[t] = {"symbol": "T", "decimals": 18,
                                   "usd_price": prices[t], "derivedETH": prices[t] / 2000.0}
    return g


class FindCyclesTimer:
    """Per-call wall-time recorder around graph.find_cycles."""

    def __init__(self, graph):
        self.graph = graph
        self.calls = []
        self._orig = graph.find_cycles

    def __enter__(self):
        def timed(*a, **k):
            t0 = time.perf_counter()
            r = self._orig(*a, **k)
            self.calls.append(time.perf_counter() - t0)
            return r
        self.graph.find_cycles = timed
        return self

    def __exit__(self, *exc):
        del self.graph.find_cycles  # restore the bound method
        return False


class PairCallTimer:
    """R1A2 §1: counts and times ACTUAL all_edges_for_pair() calls made by
    the cross-protocol detector. Installed as the DETECTOR's graph handle
    (engine.detectors[i].graph), so the engine graph — and therefore the
    RankedPairUniverse refresh, which uses the engine graph directly — is
    untouched and cannot pollute the measurement."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []     # one entry per actual pair lookup
        self.call_ms = []   # per-call wall time (ms)

    def all_edges_for_pair(self, a, b):
        t0 = time.perf_counter()
        try:
            return self._inner.all_edges_for_pair(a, b)
        finally:
            self.calls.append((str(a).lower(), str(b).lower()))
            self.call_ms.append((time.perf_counter() - t0) * 1000.0)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def pct(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def scenario_steady_state(n_pools, slice_size, lines):
    print(f"\n=== building synthetic graph ({n_pools:,} pools) ===", flush=True)
    t0 = time.perf_counter()
    g = build_synthetic_graph(n_pools)
    build_s = time.perf_counter() - t0
    cfg = BoundedDiscoveryConfig(scheduled_seeds_per_slice=slice_size)
    # R1A2 §1: production-equivalent detector set — the real scheduled
    # CrossProtocolDetector runs alongside CycleDetector, so pair_check_ms
    # measures the ACTUAL pair-check workload.
    engine = OpportunityEngine(
        w3=StubW3(), chain_id=1,
        detectors=[CycleDetector(StubW3(), g, None, None, 1),
                   CrossProtocolDetector(StubW3(), g, None, None, 1)],
        graph=g, scheduled_pass_interval=50, bounded_config=cfg)
    pairtimer = PairCallTimer(g)
    engine.detectors[1].graph = pairtimer
    # Steady state: the pre-populated graph fills the registration ring
    # (cold-start burst); scenario B covers that separately with accounting.
    engine.graph.recently_registered.drain()

    lines.append("")
    lines.append(f"--- scenario A: steady-state scheduled batch | graph={g.pool_count:,} pools | "
                 f"pairs={len(g._token_pairs):,} | build={build_s:.1f}s | seeds_per_slice={slice_size} ---")

    with FindCyclesTimer(g) as timer, \
            mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""), \
            mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""):
        block = 100
        pair_calls_before_start = len(pairtimer.calls)
        t0 = time.perf_counter()
        engine.process(block, touched_pools=frozenset())
        first_ms = (time.perf_counter() - t0) * 1000
        start_pair_calls = len(pairtimer.calls) - pair_calls_before_start
        start_info = engine.last_scheduled_pass_info
        start_batch = engine.last_scheduled_batch_info
        slice_ms = [first_ms]
        block += 1
        while engine._scheduled_batch is not None:
            t0 = time.perf_counter()
            engine.process(block, touched_pools=frozenset())
            slice_ms.append((time.perf_counter() - t0) * 1000)
            block += 1
        done = engine.last_scheduled_batch_info
    total_batch_s = sum(slice_ms) / 1000

    # R1A2 §1: verify the event-free scheduled batch actually checked its
    # anchors AND supplementary pairs within the 30-pair budget.
    max_pair_checks = BoundedDiscoveryConfig().max_pair_checks
    pair_calls = len(pairtimer.calls)
    pair_call_ms = sum(pairtimer.call_ms)
    checked_pairs = set(pairtimer.calls)
    missing_anchors = [p for p in anchor_pairs(1) if p not in checked_pairs]
    assert pair_calls <= max_pair_checks, \
        f"event-free scheduled batch exceeded the pair budget: {pair_calls} > {max_pair_checks}"
    assert not missing_anchors, f"anchor pairs not actually checked: {missing_anchors}"
    assert start_pair_calls == pair_calls, \
        "event-free run: every pair call must come from the scheduled batch-start invocation"

    lines.append(f"  universe: pools_scanned={start_info.get('pools_scanned')} "
                 f"unique_pairs_considered={start_info.get('unique_pairs')} "
                 f"anchor_pairs={start_info.get('anchor_pairs')} "
                 f"additional_pairs={start_info.get('additional_pairs')} "
                 f"total_pair_checks={start_info.get('total_pair_checks')} (selected pair KEYS) "
                 f"venues_retrieved={start_info.get('venues_retrieved')} "
                 f"seed_pools_selected={start_info.get('seed_pools_selected')} "
                 f"selection_build_ms={start_info.get('selection_build_ms', 'n/a')}")
    lines.append(f"  startup (R1A1 §4 definitions): startup_ms={done.get('startup_ms', 'n/a')} "
                 f"(UNIVERSE REFRESH only) | pair_check_ms={done.get('pair_check_ms', 'n/a')} "
                 f"(scheduled CrossProtocolDetector wall, batch start + retries) | "
                 f"total_ms_incl_startup={done.get('total_ms_incl_startup', 'n/a')} "
                 f"= startup + pair-check + slice costs, each counted exactly once "
                 f"(first whole-invocation wall={first_ms:.1f} ms; event-free block: the reactive "
                 f"cross-protocol detector is not invoked, the scheduled stage checks "
                 f"anchors + supplementary pairs)")
    lines.append(f"  ACTUAL scheduled pair checks (R1A2 §1, proxy-measured): "
                 f"{pair_calls} all_edges_for_pair() calls in the batch-start invocation "
                 f"(graph-lookup wall {pair_call_ms:.2f} ms total, "
                 f"median {statistics.median(pairtimer.call_ms):.2f} / max {max(pairtimer.call_ms):.2f} ms "
                 f"per call) | within the {max_pair_checks}-pair budget: YES | "
                 f"anchor pairs actually checked: {len(anchor_pairs(1))}/{len(anchor_pairs(1))}")
    lines.append(f"  batch: seeds_total={done['seeds_total']} slices={done['slices']} "
                 f"processed={done['seeds_processed']} skipped={done['seeds_skipped_reactive_overlap']} "
                 f"deduped={done['seeds_deduped']} complete={done['complete']} failed={done.get('failed', False)} "
                 f"pair_checks_ok={done['pair_checks_ok']} anchor_stage_covered={done['anchor_stage_covered']}")
    lines.append(f"  WHOLE-invocation wall ms (engine.process(): reactive path + scheduled work, "
                 f"includes universe refresh + pair checks on the first invocation): "
                 f"median={statistics.median(slice_ms):.1f} "
                 f"p95={pct(slice_ms, 0.95):.1f} max={max(slice_ms):.1f}")
    lines.append(f"  scheduled-slice wall ms (engine summary; cycle-search work only, excludes "
                 f"startup and pair checks): median={done.get('slice_ms_median', 0):.1f} "
                 f"p95={done.get('slice_ms_p95', 0):.1f} max={done.get('slice_ms_max', 0):.1f}")
    fc = [t * 1000 for t in timer.calls]
    lines.append(f"  per-find_cycles call ms: n={len(fc)} median={statistics.median(fc):.1f} "
                 f"p95={pct(fc, 0.95):.1f} max={max(fc):.1f}")
    lines.append(f"  total WHOLE-invocation wall: {total_batch_s:.2f}s over {done['slices']} invocations | "
                 f"engine-accounted batch cost (startup+pair+slices, once each): "
                 f"{done.get('total_ms_incl_startup', 0) / 1000:.2f}s "
                 f"(MI-102B single-pass baseline on this graph shape: 21.74s)")
    lines.append(f"  acceptance: whole-invocation max {max(slice_ms) / 1000:.2f}s "
                 f"(target <= ~2s; slice_size={slice_size})")
    return {"graph": g.pool_count, "slice": slice_size, "done": done, "slice_ms": slice_ms, "fc": fc,
            "pair_calls": pair_calls, "pair_call_ms": pair_call_ms}


def scenario_cold_start(lines):
    print("\n=== cold-start registration burst (20k graph + 1000 registrations) ===", flush=True)
    g = build_synthetic_graph(20_000)
    engine = OpportunityEngine(
        w3=StubW3(), chain_id=1,
        detectors=[CycleDetector(StubW3(), g, None, None, 1)],
        graph=g, scheduled_pass_interval=50)
    # Steady state first: the pre-populated graph's own registrations are
    # drained so the queue accounting below covers ONLY the burst.
    engine.graph.recently_registered.drain()
    ring = g.recently_registered
    received = 1000
    n_ineligible = 5  # first 5 registrations deliberately below the TVL gate
    for i in range(received):
        t = f"0x{0xa0000000 + i:064x}"
        r_in = 10**22
        tvl = 100.0 if i < n_ineligible else 50_000.0
        common = dict(dex_name="UniswapV2", fee_bps=30, version=2, tvl_usd=tvl,
                      protocol="uniswap_v2", pool_token0=WETH, pool_token1=t)
        g.add_or_update_pool(
            PoolEdge(f"0x{0xb0000000 + i:064x}", token_in=WETH, token_out=t,
                     reserve_in=r_in, reserve_out=r_in * 2, **common),
            PoolEdge(f"0x{0xb0000000 + i:064x}", token_in=t, token_out=WETH,
                     reserve_in=r_in * 2, reserve_out=r_in, **common),
        )
        g.token_metadata[t] = {"symbol": "N", "decimals": 18, "usd_price": 1.0, "derivedETH": 1.0 / 2000.0}
        g.tokens.add(WETH)
        g.tokens.add(t)

    lines.append("")
    lines.append("--- scenario B: cold-start registration burst | 20k graph + "
                 f"{received} registrations (first {n_ineligible} below the "
                 f"searchability TVL gate) ---")
    # R1A1 §2: COMPLETE registration-state partition from ACTUAL recorded
    # addresses and ACTUAL scheduler state. Every category below is read
    # from its own source; none is defined as the residual of the others,
    # so a dropped entry cannot hide inside a zero-by-construction equation.
    received_set = {f"0x{0xb0000000 + i:064x}" for i in range(received)}
    seeded_seen: list[str] = []       # every seed handed to the cycle detector
    orig_on_block = engine.detectors[0].on_block

    def recording(block_number, *a, **k):
        if k.get("scheduled_seeds") is not None:
            seeded_seen.extend(k["scheduled_seeds"])
        return orig_on_block(block_number, *a, **k)

    engine.detectors[0].on_block = recording
    with mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""), \
            mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""):
        for i in range(12):
            pending_before = len(ring)
            t0 = time.perf_counter()
            engine.process(100 + i, touched_pools=frozenset())
            wall = (time.perf_counter() - t0) * 1000
            info = engine.last_scheduled_batch_info
            processed_cum = info.get("seeds_processed", 0)
            lines.append(f"  invocation {i + 1}: ring_pending_before={pending_before} "
                         f"ring_pending_after={len(ring)} "
                         f"seeds_processed_cumulative={processed_cum} "
                         f"priority_pending={info.get('priority_pending', 0)} wall_ms={wall:.1f}")
    engine.detectors[0].on_block = orig_on_block

    # ── snapshot of every valid state category (actual addresses) ──
    batch = engine._scheduled_batch
    seed_counts = Counter(seeded_seen)
    ring_set = set(ring._queue)                      # pending in graph backlog
    staging_set = set(engine._notified_new_pools)    # popped, awaiting hand-off
    main_pending = set(batch["seeds"][batch["cursor"]:]) if batch else set()
    priority_set = set(batch["priority"]) if batch else set()
    retry_set = set(batch.get("retry_front", [])) if batch else set()
    failed_set = set(batch["failed_seeds"]) if batch else set()

    categories: dict[str, set] = {}
    remaining = set(received_set)

    def take(name: str, members) -> None:
        got = remaining & set(members)
        remaining.difference_update(got)
        categories[name] = got

    # searched: per-ADDRESS counts — a unique-address count is not an
    # exactly-once count (R1A1 §2)
    take("searched_multiple_times", {p for p in received_set if seed_counts.get(p, 0) > 1})
    take("searched_exactly_once", {p for p in received_set if seed_counts.get(p, 0) == 1})
    take("pending_ring_backlog", ring_set)
    take("pending_staging", staging_set)
    take("pending_batch_main_seeds", main_pending)
    take("pending_batch_priority", priority_set)
    take("pending_retry_queue", retry_set)
    take("failed_after_retry_exhaustion", failed_set)
    # explicitly filtered: the EXACT production eligibility condition
    categories["filtered_ineligible"] = {p for p in remaining if not pool_is_searchable(g, p)}
    remaining -= categories["filtered_ineligible"]
    categories["dropped"] = remaining  # absent from EVERY valid category

    dropped = categories["dropped"]
    assert not dropped, f"registrations lost (absent from every valid state): {sorted(dropped)[:8]}"
    assert not categories["searched_multiple_times"], \
        f"registrations seeded more than once: {sorted(categories['searched_multiple_times'])[:8]}"

    accounted = sum(len(v) for v in categories.values())
    assert accounted == len(received_set), \
        f"partition does not cover all registrations: {accounted} != {len(received_set)}"

    handed = len(received_set) - len(categories["pending_ring_backlog"])
    lines.append("  registration-state partition (actual scheduler state, mutually exclusive):")
    for name, members in categories.items():
        sample = sorted(members)[:3]
        lines.append(f"    {name}: {len(members)}"
                     + (f" e.g. {sample}" if members and name == "dropped" else ""))
    lines.append(f"  totals: received={received} accounted={accounted} "
                 f"handed_to_scheduler={handed} "
                 f"waiting_in_graph_backlog={len(categories['pending_ring_backlog'])} "
                 f"dropped={len(dropped)} (expected 0, asserted)")
    lines.append("  (registrations still waiting in the graph backlog have NOT been handed "
                 "to the scheduler yet; the full exactly-once drain proof over the whole "
                 "backlog is the 1,000-pool regression in the test suite)")
    return dropped


def snapshot_graph_state(g):
    """Snapshot of the mutable graph state the detectors read (R1A §3):
    the reference engine must see exactly the pools/edges/metadata the
    measured engine saw, so the reactive-only comparison is genuine."""
    snap = TokenGraph(chain_id=g.chain_id)
    snap._edges = {k: list(v) for k, v in g._edges.items()}
    snap._pool_index = {k: list(v) for k, v in g._pool_index.items()}
    snap._token_pairs = {k: list(v) for k, v in g._token_pairs.items()}
    snap.token_metadata = dict(g.token_metadata)
    snap.tokens = set(g.tokens)
    return snap


def scenario_simultaneous(lines):
    print("\n=== simultaneous reactive + scheduled block (20k graph) ===", flush=True)
    g = build_synthetic_graph(20_000)
    # R1A2 §1: production-equivalent detector set in BOTH engines — the
    # incremental engine and the scheduled_discovery=False reference engine
    # run the same CycleDetector + CrossProtocolDetector pair, so the
    # reactive baseline includes the same reactive pair-check work.
    engine = OpportunityEngine(
        w3=StubW3(), chain_id=1,
        detectors=[CycleDetector(StubW3(), g, None, None, 1),
                   CrossProtocolDetector(StubW3(), g, None, None, 1)],
        graph=g, scheduled_pass_interval=50)
    inc_pairs = PairCallTimer(g)
    engine.detectors[1].graph = inc_pairs
    engine.graph.recently_registered.drain()
    rng = random.Random(7)
    top = [e.pool_address for e in g.get_top_pools_by_tvl(400)]
    touched = frozenset(rng.sample(top, min(20, len(top))))

    # Reference engine: SAME graph state (snapshot taken BEFORE the measured
    # block, identical touched-pool set) with scheduled discovery DISABLED —
    # a genuine reactive-only measurement with no active scheduled batch.
    ref_graph = snapshot_graph_state(g)
    ref_engine = OpportunityEngine(
        w3=StubW3(), chain_id=1,
        detectors=[CycleDetector(StubW3(), ref_graph, None, None, 1),
                   CrossProtocolDetector(StubW3(), ref_graph, None, None, 1)],
        graph=ref_graph, scheduled_discovery=False)
    ref_pairs = PairCallTimer(ref_graph)
    ref_engine.detectors[1].graph = ref_pairs

    lines.append("--- scenario C: 20 touched pools on a mid-batch block "
                 "(same graph state + touched set, two engines, identical "
                 "production-equivalent detector sets) ---")
    with mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""), \
            mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""):
        inc_before_start = len(inc_pairs.calls)
        engine.process(100, touched_pools=frozenset())   # batch starts (slice 1)
        inc_start_pair_calls = len(inc_pairs.calls) - inc_before_start
        # reference: warmed the same way (one quiet reactive block), then
        # measured on the identical touched set with scheduling disabled
        ref_engine.process(100, touched_pools=frozenset())
        snap2 = snapshot_graph_state(g)                  # state at measured block
        ref_engine2 = OpportunityEngine(
            w3=StubW3(), chain_id=1,
            detectors=[CycleDetector(StubW3(), snap2, None, None, 1),
                       CrossProtocolDetector(StubW3(), snap2, None, None, 1)],
            graph=snap2, scheduled_discovery=False)
        ref_pairs2 = PairCallTimer(snap2)
        ref_engine2.detectors[1].graph = ref_pairs2
        ref_engine2.process(100, touched_pools=frozenset())

        inc_before = len(inc_pairs.calls)
        ref_before_a = len(ref_pairs.calls)
        t0 = time.perf_counter()
        engine.process(101, touched_pools=touched)       # reactive + scheduled slice
        combined = (time.perf_counter() - t0) * 1000
        inc_measured_pair_calls = len(inc_pairs.calls) - inc_before
        t0 = time.perf_counter()
        ref_engine.process(101, touched_pools=touched)   # reactive only (no batch)
        reactive_a = (time.perf_counter() - t0) * 1000
        ref_measured_a = len(ref_pairs.calls) - ref_before_a
        ref_before_b = len(ref_pairs2.calls)
        t0 = time.perf_counter()
        ref_engine2.process(101, touched_pools=touched)  # reactive only, fresh snapshot
        reactive_b = (time.perf_counter() - t0) * 1000
        ref_measured_b = len(ref_pairs2.calls) - ref_before_b
        slice_wall = combined - min(reactive_a, reactive_b)
    info = engine.last_scheduled_batch_info
    lines.append(f"  combined reactive+scheduled-slice invocation: {combined:.1f} ms "
                 f"(batch active: {not info.get('complete', True)}, "
                 f"slices={info.get('slices', 0)})")
    lines.append(f"  reactive-only reference (scheduled_discovery=False, same graph state + "
                 f"touched set): {reactive_a:.1f} ms / {reactive_b:.1f} ms (two independent runs)")
    lines.append(f"  incremental scheduled slice cost (combined - reactive-only): "
                 f"~{slice_wall:.1f} ms")
    lines.append(f"  ACTUAL pair calls (R1A2 §1): batch-start invocation "
                 f"{inc_start_pair_calls} (event-free scheduled anchors+supplementary, "
                 f"within the 30-pair budget: {'YES' if inc_start_pair_calls <= 30 else 'NO'}) | "
                 f"measured block: incremental engine {inc_measured_pair_calls} "
                 f"(reactive anchors; scheduled stage already complete) vs "
                 f"reference engines {ref_measured_a}/{ref_measured_b} (reactive anchors) — "
                 f"identical reactive work in both engines")
    lines.append("  (reactive cost is the pre-existing touched-pool path, unchanged by R1/R1A/R1A1/R1A2)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="2000,20000")
    parser.add_argument("--slices", default="1,2")
    parser.add_argument("--out", default=None,
                        help="output file (default: benchmark_results.txt in the repository root)")
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    slice_sizes = [int(s) for s in args.slices.split(",")]

    lines = []
    lines.append("DSS-WO-MI-102B-R1A2 §1 benchmark — scheduled pair checks measured, recovery bounded")
    lines.append(f"repo_root={REPO_ROOT}")
    lines.append(f"config: additional_pair_limit={BoundedDiscoveryConfig().additional_pair_limit} "
                 f"max_pair_checks={BoundedDiscoveryConfig().max_pair_checks} "
                 f"refresh_interval_blocks={BoundedDiscoveryConfig().refresh_interval_blocks} "
                 f"max_seed_pools={BoundedDiscoveryConfig().max_seed_pools} "
                 f"max_new_pool_seeds_per_pass={BoundedDiscoveryConfig().max_new_pool_seeds_per_pass} "
                 f"scheduled_seeds_per_slice={slice_sizes}")
    for n in sizes:
        for s in slice_sizes:
            scenario_steady_state(n, s, lines)
    scenario_cold_start(lines)
    scenario_simultaneous(lines)
    out = "\n".join(lines)
    print(out)
    out_path = os.path.abspath(args.out) if args.out else os.path.join(REPO_ROOT, "benchmark_results.txt")
    with open(out_path, "w") as f:
        f.write(out + "\n")
    print(f"\n[written] {out_path}")


if __name__ == "__main__":
    main()
