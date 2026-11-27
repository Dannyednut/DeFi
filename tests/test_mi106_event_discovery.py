"""DSS-WO-MI-106 — Crawler event-discovery queue reliability (offline).

Deterministic tests for WS-B, per work order §3C. Every test verifies ACTUAL
queue contents, graph registration, counters and in-flight deduplication
state — never only log messages. No live RPC, no network.
"""
from __future__ import annotations

import asyncio
import queue as queue_mod
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from config import get_chain
from crawler import PoolCrawler
from graph import TokenGraph, PoolEdge

T0, T1 = "0x" + "aa" * 20, "0x" + "bb" * 20


def make_crawler(queue_maxsize=1000):
    """PoolCrawler without __init__'s network-flavoured construction; only the
    event-discovery machinery is populated (offline deterministic)."""
    c = object.__new__(PoolCrawler)
    c._w3 = MagicMock()
    c._graph = TokenGraph()
    c._cache = MagicMock()
    c._price_oracle = None
    c._chain = SimpleNamespace(dexes=[
        SimpleNamespace(name="UniV2", protocol="uniswap_v2", version=2,
                        factory="0x" + "5a" * 20, fee_bps=30, fee_tiers=[],
                        enabled=True, deploy_block=1, router="0x" + "7a" * 20),
        SimpleNamespace(name="UniV3", protocol="uniswap_v3", version=3,
                        factory="0x" + "5b" * 20, fee_bps=3000,
                        fee_tiers=[3000], enabled=True, deploy_block=1,
                        router="0x" + "7b" * 20),
        SimpleNamespace(name="Curve", protocol="curve", version=2,
                        factory="0x" + "5c" * 20, fee_bps=0, fee_tiers=[],
                        enabled=True, deploy_block=1, router=""),
        SimpleNamespace(name="SyncSwap", protocol="syncswap", version=2,
                        factory="0x" + "5d" * 20, fee_bps=30, fee_tiers=[],
                        enabled=True, deploy_block=1, router=""),
    ])
    c._graph_client = MagicMock()
    c._subgraph_providers = {}
    c.running = False
    c._event_discovery_queue = queue_mod.Queue(maxsize=queue_maxsize)
    c._event_discovery_seen = set()
    c._event_discovery_enqueued_at = {}
    c._event_discovery_high_water = 0
    c.event_discovery_enqueued = 0
    c.event_discovery_processed = 0
    c.event_discovery_dropped = 0
    c.event_discovery_invalid = 0
    c.event_discovery_coalesced = 0
    c.event_discovery_already_registered = 0
    c.event_discovery_batches = 0
    c.event_discovery_batch_hints = 0
    c.event_discovery_generic_hints = 0
    c.event_discovery_specialized_hints = 0
    c.event_discovery_fallback_hints = 0
    c.event_discovery_registrations = 0
    c.event_discovery_verification_failures = 0
    c.event_discovery_released_registered = 0
    c.event_discovery_released_retry = 0
    c.event_discovery_batch_errors = 0
    c.event_discovery_batch_seconds = 0.0
    c.event_discovery_last_batch = {}
    c.v2_discovered = c.v2_filtered = c.v3_discovered = c.v3_filtered = 0
    c.stale_refreshed = 0
    return c


def hint(addr, protocol="uniswap_v2", topics=None):
    h = {"pool_address": addr, "protocol": protocol,
         "block_number": 100, "log_index": 1}
    if topics:
        h["topics"] = topics
    return h


def fake_dex(name="UniV2", version=2, fee_bps=30, protocol="uniswap_v2"):
    return SimpleNamespace(name=name, version=version, fee_bps=fee_bps,
                           protocol=protocol)


def register_into_graph(graph, addr):
    """The fake register_new_pool: really register both directions."""
    graph.add_or_update_pool(
        PoolEdge(addr.lower(), "UniV2", T0, T1, 10**18, 2 * 10**18, 30, 2,
                 tvl_usd=100_000.0, protocol="uniswap_v2",
                 pool_token0=T0, pool_token1=T1),
        PoolEdge(addr.lower(), "UniV2", T1, T0, 2 * 10**18, 10**18, 30, 2,
                 tvl_usd=100_000.0, protocol="uniswap_v2",
                 pool_token0=T0, pool_token1=T1),
    )


def addr(i):
    return "0x" + f"{i:040x}"


async def run_batch(c, batch, **patches):
    """Run ONE batch through the worker's processing path with the given
    patch mapping {module_attribute_path: replacement}."""
    async def _run():
        stack = []
        for target, replacement in patches.items():
            p = patch(target, replacement)
            stack.append(p)
            p.start()
        try:
            await c._process_event_batch(batch)
        finally:
            for p in reversed(stack):
                p.stop()
    await _run()


class AdapterStub:
    def __init__(self, batchable=True):
        self._batchable = batchable

    def supports_batched_discovery(self):
        return self._batchable


class RegistryStub:
    def __init__(self, batchable=True):
        self._batchable = batchable

    def get(self, dex):
        return AdapterStub(self._batchable)


# ── 1. Duplicate hints coalesced while pending ───────────────────────────────

def test_duplicate_hints_coalesced_while_pending():
    c = make_crawler()
    assert c.enqueue_event_discovery([hint(addr(1)), hint(addr(1)), hint(addr(1))]) == 1
    assert c._event_discovery_queue.qsize() == 1
    assert c.event_discovery_enqueued == 1
    assert c.event_discovery_coalesced == 2
    assert len(c._event_discovery_seen) == 1      # exactly one in-flight key
    assert c.event_discovery_stats()["in_flight_keys"] == 1


# ── 2. Successful generic V2/V3 batch registration ───────────────────────────

def test_generic_batch_registration_releases_key():
    c = make_crawler()
    pool = addr(2)
    c.enqueue_event_discovery([hint(pool)])

    async def fake_register(w3, graph, cache, gc, pool_addr, dex_name, version,
                            fee_bps, **kw):
        register_into_graph(graph, pool_addr)

    def fake_multicall(w3, hints):
        return [{"hint": hints[0], "pool_address": hints[0]["pool_address"],
                 "dex": fake_dex(), "fee": None, "token0": T0, "token1": T1}]

    asyncio.run(run_batch(c, [c._event_discovery_queue.get_nowait()], **{
        "crawler.register_new_pool": fake_register,
        "utils.multicall3.multicall3_discover_event_hints": fake_multicall,
        "protocols.ProtocolRegistry": RegistryStub,
    }))

    assert c._graph.has_pool(pool)                       # REAL graph registration
    assert c.event_discovery_processed == 1
    assert c.event_discovery_registrations == 1
    assert len(c._event_discovery_seen) == 0             # in-flight key RELEASED
    assert c.event_discovery_released_registered == 1
    assert c.event_discovery_released_retry == 0
    # a repeat hint for the registered pool is now skipped by has_pool
    assert c.enqueue_event_discovery([hint(pool)]) == 0
    assert c.event_discovery_already_registered == 1


# ── 3. Successful specialized batch registration ─────────────────────────────

def test_specialized_batch_registration():
    c = make_crawler()
    pool = addr(3)
    c.enqueue_event_discovery([hint(pool, protocol="curve",
                                    topics=["0x", "0x", "0x2", "0x1"])])

    async def fake_register(w3, graph, cache, gc, pool_addr, dex_name, version,
                            fee_bps, **kw):
        register_into_graph(graph, pool_addr)

    def fake_specialized(w3, pairs):
        ref, adapter = pairs[0]
        return {ref.address: {"token0": T0, "token1": T1, "r0": 10**18, "r1": 2 * 10**18}}

    asyncio.run(run_batch(c, [c._event_discovery_queue.get_nowait()], **{
        "crawler.register_new_pool": fake_register,
        "utils.multicall3.multicall3_batch_discover_specialized": fake_specialized,
        "protocols.ProtocolRegistry": RegistryStub,
    }))

    assert c._graph.has_pool(pool)
    assert c.event_discovery_processed == 1
    assert c.event_discovery_specialized_hints == 1
    assert len(c._event_discovery_seen) == 0


# ── 4. Sequential fallback success ───────────────────────────────────────────

def test_sequential_fallback_success():
    c = make_crawler()
    pool = addr(4)
    c.enqueue_event_discovery([hint(pool, protocol="syncswap")])

    async def fake_register(w3, graph, cache, gc, pool_addr, dex_name, version,
                            fee_bps, **kw):
        register_into_graph(graph, pool_addr)

    async def fake_discover_from_event(w3, graph, cache, gc, hint_):
        register_into_graph(graph, hint_["pool_address"])
        return True

    asyncio.run(run_batch(c, [c._event_discovery_queue.get_nowait()], **{
        "crawler.register_new_pool": fake_register,
        "registry.discover_from_event": fake_discover_from_event,
        # no batchable adapter for syncswap → straight to the sequential
        # fallback path
        "protocols.ProtocolRegistry": lambda w3: RegistryStub(batchable=False),
    }))

    assert c._graph.has_pool(pool)
    assert c.event_discovery_fallback_hints == 1
    assert c.event_discovery_processed == 1
    assert len(c._event_discovery_seen) == 0


# ── 5. Generic batch failure WITHOUT permanent deduplication poisoning ──────

def test_generic_batch_failure_releases_for_retry():
    c = make_crawler()
    pool = addr(5)
    c.enqueue_event_discovery([hint(pool)])

    def boom(w3, hints):
        raise RuntimeError("rpc down")

    asyncio.run(run_batch(c, [c._event_discovery_queue.get_nowait()], **{
        "utils.multicall3.multicall3_discover_event_hints": boom,
        "protocols.ProtocolRegistry": RegistryStub,
    }))

    assert not c._graph.has_pool(pool)
    assert c.event_discovery_verification_failures == 1
    assert len(c._event_discovery_seen) == 0            # RELEASED — retryable
    assert c.event_discovery_released_retry == 1
    # the SAME event arriving later is accepted again (no permanent poison)
    assert c.enqueue_event_discovery([hint(pool)]) == 1
    assert c._event_discovery_queue.qsize() == 1


# ── 6. Specialized verification returning no state, then a later retry ──────

def test_specialized_no_state_releases_for_retry():
    c = make_crawler()
    pool = addr(6)
    c.enqueue_event_discovery([hint(pool, protocol="curve",
                                    topics=["0x", "0x", "0x2", "0x1"])])

    def fake_specialized(w3, pairs):
        return {}   # verification found nothing

    asyncio.run(run_batch(c, [c._event_discovery_queue.get_nowait()], **{
        "utils.multicall3.multicall3_batch_discover_specialized": fake_specialized,
        "protocols.ProtocolRegistry": RegistryStub,
    }))

    assert not c._graph.has_pool(pool)
    assert len(c._event_discovery_seen) == 0
    assert c.event_discovery_released_retry == 1
    assert c.enqueue_event_discovery([hint(pool, protocol="curve",
                                           topics=["0x", "0x", "0x2", "0x1"])]) == 1


# ── 7. Registration exception, other candidates still register, later retry ─

def test_registration_exception_isolated_and_retryable():
    c = make_crawler()
    bad, good = addr(7), addr(8)
    c.enqueue_event_discovery([hint(bad), hint(good)])

    calls = []

    async def fake_register(w3, graph, cache, gc, pool_addr, dex_name, version,
                            fee_bps, **kw):
        calls.append(pool_addr.lower())
        if pool_addr.lower() == bad:
            raise RuntimeError("boom")
        register_into_graph(graph, pool_addr)

    def fake_multicall(w3, hints):
        return [{"hint": h, "pool_address": h["pool_address"], "dex": fake_dex(),
                 "fee": None, "token0": T0, "token1": T1} for h in hints]

    asyncio.run(run_batch(c, [c._event_discovery_queue.get_nowait(),
                              c._event_discovery_queue.get_nowait()], **{
        "crawler.register_new_pool": fake_register,
        "utils.multicall3.multicall3_discover_event_hints": fake_multicall,
        "protocols.ProtocolRegistry": RegistryStub,
    }))

    # the GOOD candidate of the same batch was NOT discarded by the bad one
    assert c._graph.has_pool(good)
    assert not c._graph.has_pool(bad)
    assert c.event_discovery_registrations == 1
    assert c.event_discovery_processed == 1
    assert sorted(calls) == sorted([bad, good])          # BOTH attempts made
    # failed key released for retry; successful key released as registered
    assert len(c._event_discovery_seen) == 0
    assert c.event_discovery_released_retry == 1
    assert c.event_discovery_released_registered == 1
    assert c.enqueue_event_discovery([hint(bad)]) == 1   # retry accepted


# ── 8. Queue capacity reached and overflow accounted for ─────────────────────

def test_queue_capacity_overflow_accounted():
    c = make_crawler(queue_maxsize=2)
    added = c.enqueue_event_discovery([hint(addr(i)) for i in range(10)])
    assert added == 2
    assert c.event_discovery_dropped == 8
    assert c._event_discovery_queue.qsize() == 2
    assert c._event_discovery_high_water == 2
    assert len(c._event_discovery_seen) == 2             # only accepted hints


# ── 9. Sustained burst with a mocked fast consumer: measured drain ──────────

def test_sustained_burst_drains_with_overlapped_registration():
    c = make_crawler(queue_maxsize=5000)
    pools = [addr(100 + i) for i in range(400)]
    c.enqueue_event_discovery([hint(p) for p in pools])
    assert c._event_discovery_queue.qsize() == 400

    REGISTER_LATENCY = 0.003

    async def fake_register(w3, graph, cache, gc, pool_addr, dex_name, version,
                            fee_bps, **kw):
        await asyncio.sleep(REGISTER_LATENCY)
        register_into_graph(graph, pool_addr)

    def fake_multicall(w3, hints):
        return [{"hint": h, "pool_address": h["pool_address"], "dex": fake_dex(),
                 "fee": None, "token0": T0, "token1": T1} for h in hints]

    async def main():
        c.running = True

        def factory(url, **kw):
            return _NoopCtx()
        worker = asyncio.create_task(c._event_discovery_worker())
        deadline = time.monotonic() + 30.0
        started = time.monotonic()
        with patch("crawler.register_new_pool", fake_register), \
             patch("utils.multicall3.multicall3_discover_event_hints", fake_multicall), \
             patch("protocols.ProtocolRegistry", RegistryStub):
            while (not c._event_discovery_queue.empty() or c._event_discovery_seen) \
                    and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            elapsed = time.monotonic() - started
        c.running = False
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        return elapsed

    elapsed = asyncio.run(main())

    assert c._event_discovery_queue.empty()
    assert len(c._event_discovery_seen) == 0             # all keys released
    assert c.event_discovery_registrations == 400
    assert c.event_discovery_dropped == 0
    # OVERLAP EVIDENCE: sequential registration would need 400×3ms = 1.2s of
    # pure registration time alone; the measured drain must beat it clearly.
    assert elapsed < 400 * REGISTER_LATENCY * 0.8, (
        f"drain took {elapsed:.3f}s — registration did not overlap"
    )
    stats = c.event_discovery_stats()
    assert stats["batches"] >= 8 and stats["batch_hints"] == 400


class _NoopCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


# ── 10. Cancellation and worker exceptions without inconsistent accounting ──

def test_cancellation_releases_keys_and_completes_task_done():
    c = make_crawler()
    pools = [addr(200 + i) for i in range(5)]
    c.enqueue_event_discovery([hint(p) for p in pools])

    async def slow_register(w3, graph, cache, gc, pool_addr, dex_name, version,
                            fee_bps, **kw):
        await asyncio.sleep(0.5)     # keep the batch in-flight when cancelled
        register_into_graph(graph, pool_addr)

    def fake_multicall(w3, hints):
        return [{"hint": h, "pool_address": h["pool_address"], "dex": fake_dex(),
                 "fee": None, "token0": T0, "token1": T1} for h in hints]

    async def main():
        c.running = True
        worker = asyncio.create_task(c._event_discovery_worker())
        await asyncio.sleep(0.15)    # the batch is being processed now
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    with patch("crawler.register_new_pool", slow_register), \
         patch("utils.multicall3.multicall3_discover_event_hints", fake_multicall), \
         patch("protocols.ProtocolRegistry", RegistryStub):
        asyncio.run(main())

    assert c._event_discovery_queue.unfinished_tasks == 0   # no leaked tasks
    assert len(c._event_discovery_seen) == 0                # keys released for retry
    assert not c._graph.has_pool(pools[0])                  # cancelled mid-flight


def test_worker_stage_exception_releases_keys_and_counts():
    c = make_crawler()
    pool = addr(9)
    c.enqueue_event_discovery([hint(pool)])

    class ExplodingRegistry:
        def __init__(self, w3):
            raise RuntimeError("registry init failed")

        def get(self, dex):
            return None

    # in the live worker the stage exception is caught by the worker loop
    # AFTER the batch finally-block released the keys; call it the same way
    with pytest.raises(RuntimeError):
        asyncio.run(run_batch(c, [c._event_discovery_queue.get_nowait()], **{
            "protocols.ProtocolRegistry": ExplodingRegistry,
        }))

    # the batch crashed AFTER the generic stage; the key was released and
    # task_done bookkeeping completed
    assert len(c._event_discovery_seen) == 0
    assert c._event_discovery_queue.unfinished_tasks == 0
    assert c.event_discovery_released_retry == 1


# ── 11. A registered pool is not registered twice ────────────────────────────

def test_registered_pool_not_registered_twice():
    c = make_crawler()
    pool = addr(10)
    register_into_graph(c._graph, pool)

    calls = []

    async def fake_register(w3, graph, cache, gc, pool_addr, dex_name, version,
                            fee_bps, **kw):
        calls.append(pool_addr.lower())
        register_into_graph(graph, pool_addr)

    def fake_multicall(w3, hints):
        return [{"hint": h, "pool_address": h["pool_address"], "dex": fake_dex(),
                 "fee": None, "token0": T0, "token1": T1} for h in hints]

    # the producer already skips registered pools at enqueue time
    assert c.enqueue_event_discovery([hint(pool)]) == 0
    assert c.event_discovery_already_registered == 1

    # and even if a hint slipped into the queue before registration, the
    # worker's verification path checks has_pool before registering
    c2 = make_crawler()
    c2.enqueue_event_discovery([hint(pool)])
    register_into_graph(c2._graph, pool)   # registered BETWEEN enqueue and drain
    asyncio.run(run_batch(c2, [c2._event_discovery_queue.get_nowait()], **{
        "crawler.register_new_pool": fake_register,
        "utils.multicall3.multicall3_discover_event_hints": fake_multicall,
        "protocols.ProtocolRegistry": RegistryStub,
    }))
    assert calls == []                                  # no duplicate registration
    assert c2._graph.has_pool(pool)
    assert len(c2._event_discovery_seen) == 0


# ── producer-side invariants ─────────────────────────────────────────────────

def test_enqueue_is_nonblocking_and_counts_all_dispositions():
    c = make_crawler(queue_maxsize=3)
    # invalid hints counted, not silently discarded
    n = c.enqueue_event_discovery([{"pool_address": "nothex", "protocol": "x"},
                                   {"pool_address": addr(1), "protocol": ""}])
    assert n == 0 and c.event_discovery_invalid == 2
    # mixed batch: accepted + duplicate + registered + dropped
    register_into_graph(c._graph, addr(20))
    hints = [hint(addr(i)) for i in range(5)] + [hint(addr(1)), hint(addr(20))]
    added = c.enqueue_event_discovery(hints)
    assert added == 3
    assert c.event_discovery_coalesced == 1
    assert c.event_discovery_already_registered == 1
    assert c.event_discovery_dropped == 2
    assert c.event_discovery_oldest_pending_age >= 0.0
    assert c.event_discovery_stats()["queue_high_water"] == 3
