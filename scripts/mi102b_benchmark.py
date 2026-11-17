"""DSS-WO-MI-102B-R1 §6 benchmark: incremental scheduled-search cost and
registration-queue accounting on deterministic synthetic graphs.

Reproducible invocation (repo root):

    PYTHONDONTWRITEBYTECODE=1 python scripts/mi102b_benchmark.py \
        [--sizes 2000,20000] [--slice 2,1]

Deterministic (seeded RNG, no network, no RPC); contract quoting is disabled
(ARB_EXEC_ADDRESS -> "") exactly like the offline regression tests.

Scenarios
---------
A. Steady-state scheduled batch (per graph size, per slice size):
   build graph -> engine (ring drained: steady state) -> one cadence batch
   processed in slices -> per-invocation (slice) wall times, per-find_cycles
   call times, batch totals, pair checks, venues, seeds.

B. Cold-start registration burst (20k graph): 1,000 pools registered after
   engine creation -> queue accounting (received / consumed / retained /
   dropped) over successive invocations.

C. Simultaneous reactive + scheduled block (20k graph): 20 touched pools on
   a mid-batch block -> combined invocation wall time.
"""
import argparse
import random
import statistics
import sys
import time
from unittest import mock

sys.path.insert(0, "/home/z/my-project/repos/DeFi")

import detectors.base as detectors_base
import detectors.cycles as cycles_mod
from bounded_discovery import BoundedDiscoveryConfig
from detectors.cycles import CycleDetector
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
    engine = OpportunityEngine(
        w3=StubW3(), chain_id=1,
        detectors=[CycleDetector(StubW3(), g, None, None, 1)],
        graph=g, scheduled_pass_interval=50, bounded_config=cfg)
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
        t0 = time.perf_counter()
        engine.process(block, touched_pools=frozenset())
        first_ms = (time.perf_counter() - t0) * 1000
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

    lines.append(f"  universe: pools_scanned={start_info.get('pools_scanned')} "
                 f"unique_pairs_considered={start_info.get('unique_pairs')} "
                 f"anchor_pairs={start_info.get('anchor_pairs')} "
                 f"additional_pairs={start_info.get('additional_pairs')} "
                 f"total_pair_checks={start_info.get('total_pair_checks')} "
                 f"venues_retrieved={start_info.get('venues_retrieved')} "
                 f"seed_pools_selected={start_info.get('seed_pools_selected')} "
                 f"selection_build_ms={start_info.get('selection_build_ms', 'n/a')}")
    lines.append(f"  universe refresh+pair checks included in slice 1 "
                 f"(slice 1 wall={first_ms:.1f} ms; anchors via reactive=no)")
    lines.append(f"  batch: seeds_total={done['seeds_total']} slices={done['slices']} "
                 f"processed={done['seeds_processed']} skipped={done['seeds_skipped_reactive_overlap']} "
                 f"deduped={done['seeds_deduped']} complete={done['complete']}")
    lines.append(f"  per-invocation (slice) wall ms: median={statistics.median(slice_ms):.1f} "
                 f"p95={pct(slice_ms, 0.95):.1f} max={max(slice_ms):.1f}")
    fc = [t * 1000 for t in timer.calls]
    lines.append(f"  per-find_cycles call ms: n={len(fc)} median={statistics.median(fc):.1f} "
                 f"p95={pct(fc, 0.95):.1f} max={max(fc):.1f}")
    lines.append(f"  total batch wall time: {total_batch_s:.2f}s over {done['slices']} invocations "
                 f"(MI-102B single-pass baseline on this graph shape: 21.74s)")
    lines.append(f"  acceptance: per-invocation max {max(slice_ms) / 1000:.2f}s "
                 f"(target <= ~2s; slice_size={slice_size})")
    return {"graph": g.pool_count, "slice": slice_size, "done": done, "slice_ms": slice_ms, "fc": fc}


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
    for i in range(received):
        t = f"0x{0xa0000000 + i:064x}"
        r_in = 10**22
        common = dict(dex_name="UniswapV2", fee_bps=30, version=2, tvl_usd=50_000.0,
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
                 f"{received} registrations ---")
    consumed_total = 0
    with mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""), \
            mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""):
        for i in range(12):
            pending_before = len(ring)
            t0 = time.perf_counter()
            engine.process(100 + i, touched_pools=frozenset())
            wall = (time.perf_counter() - t0) * 1000
            info = engine.last_scheduled_batch_info
            processed_cum = info.get("seeds_processed", 0)
            consumed_total = received - len(ring)
            lines.append(f"  invocation {i + 1}: ring_pending_before={pending_before} "
                         f"ring_pending_after={len(ring)} "
                         f"seeds_processed_cumulative={processed_cum} "
                         f"priority_pending={info.get('priority_pending', 0)} wall_ms={wall:.1f}")
    dropped = received - consumed_total - len(ring)
    lines.append(f"  queue accounting: received={received} consumed_into_seeds={consumed_total} "
                 f"retained_in_backlog={len(ring)} dropped={dropped} (expected 0)")
    return dropped


def scenario_simultaneous(lines):
    print("\n=== simultaneous reactive + scheduled block (20k graph) ===", flush=True)
    g = build_synthetic_graph(20_000)
    engine = OpportunityEngine(
        w3=StubW3(), chain_id=1,
        detectors=[CycleDetector(StubW3(), g, None, None, 1)],
        graph=g, scheduled_pass_interval=50)
    engine.graph.recently_registered.drain()
    rng = random.Random(7)
    top = [e.pool_address for e in g.get_top_pools_by_tvl(400)]
    touched = set(rng.sample(top, min(20, len(top))))
    lines.append("--- scenario C: 20 touched pools on a mid-batch block ---")
    with mock.patch.object(detectors_base, "ARB_EXEC_ADDRESS", ""), \
            mock.patch.object(cycles_mod, "ARB_EXEC_ADDRESS", ""):
        engine.process(100, touched_pools=frozenset())          # batch starts
        t0 = time.perf_counter()
        engine.process(101, touched_pools=touched)               # reactive + slice 2
        combined = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        engine.process(130, touched_pools=touched)               # reactive only (batch done? if active: slice included)
        reactive_only = (time.perf_counter() - t0) * 1000
    lines.append(f"  combined reactive+slice invocation: {combined:.1f} ms")
    lines.append(f"  reactive-only reference (same 20 touched, later block): {reactive_only:.1f} ms")
    lines.append("  (reactive cost is the pre-existing touched-pool path, unchanged by R1)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="2000,20000")
    parser.add_argument("--slices", default="1,2")
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    slice_sizes = [int(s) for s in args.slices.split(",")]

    lines = []
    lines.append("DSS-WO-MI-102B-R1 §6 benchmark — incremental scheduled search + registration queue")
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
    import os
    os.makedirs("/home/z/my-project/download/DSS-WO-MI-102B-R1", exist_ok=True)
    with open("/home/z/my-project/download/DSS-WO-MI-102B-R1/benchmark_results_r1.txt", "w") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
