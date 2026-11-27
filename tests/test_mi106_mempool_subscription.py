"""DSS-WO-MI-106 — Mempool subscription lifecycle (offline, fake WebSocket).

Deterministic tests for WS-A: the subscription lifecycle is validated against
a scripted in-memory WebSocket — no real provider connection or API key is
used anywhere. Covers, per work order §2D:

1.  Successful subscription acknowledgement + valid hash notification.
2.  Successful acknowledgement + full transaction-object notification.
3.  Explicit JSON-RPC subscription error (rejection, not connection success).
4.  Delayed acknowledgement / connection closure before acknowledgement.
5.  Unrelated JSON-RPC responses and keepalive replies.
6.  Notification with an incorrect subscription ID.
7.  Duplicate notifications.
8.  Reconnection after a failed subscription.
9.  Rate-limit and transaction-queue overflow accounting.
10. Configuration values actually reaching the watcher.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import websockets
from mempool.watcher import MempoolWatcher, SubscriptionError, WS_SUBSCRIBED, WS_AWAITING_ACK

ROUTER = "0x" + "11" * 20
SENDER = "0x" + "22" * 20
POOL = "0x" + "33" * 20
HASH = "0x" + "ab" * 32
HASH2 = "0x" + "cd" * 32
SUB_ID = "0x7e"


class FakeWebSocket:
    """Scripted in-memory stand-in for a websockets client connection."""

    def __init__(self, script):
        self._script = list(script)
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if not self._script:
            await asyncio.sleep(60)  # park; tests end scripts with a close
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeConnectContext:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


def make_watcher(**kw):
    defaults = dict(rpc_ws_url="wss://fake.example", w3=MagicMock(),
                    router_addresses={ROUTER}, chain_id=1,
                    ack_timeout=0.05, backoff_base=0.01)
    defaults.update(kw)
    return MempoolWatcher(**defaults)


def ack(sub_id=SUB_ID, req_id=1):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": sub_id})


def error(code=-32600, message="filtered subscription not supported", req_id=1):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def notification(result, sub=SUB_ID):
    return json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                       "params": {"subscription": sub, "result": result}})


def tx_dict(hash_=HASH, to=ROUTER):
    return {"hash": hash_, "to": to, "from": SENDER, "value": 1,
            "input": "0x", "nonce": 1, "gas": 100000, "gasPrice": 10**9}


def drive(watcher, script, *, drain=True):
    """Run _connect_and_listen once against a scripted fake connection.
    The script must end with websockets.ConnectionClosed (or raise a
    SubscriptionError, which is returned instead of propagated)."""
    async def main():
        watcher._running = True   # production run() sets this; direct-drive tests set it here
        watcher._queue = asyncio.Queue(maxsize=watcher._queue_size)
        watcher._semaphore = asyncio.Semaphore(10)
        ws = FakeWebSocket(script)

        def _connect(url, **kwargs):
            return _FakeConnectContext(ws)

        caught = None
        with patch("mempool.watcher.websockets.connect", side_effect=_connect):
            try:
                await watcher._connect_and_listen()
            except websockets.ConnectionClosed:
                pass
            except SubscriptionError as exc:
                caught = exc
        if drain:
            deadline = time.monotonic() + 5.0
            while watcher._inflight_fetches > 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        return ws, caught

    return asyncio.run(main())


def tx_return(watcher, payload=None):
    """Make the mocked eth_getTransactionByHash return a target tx."""
    watcher._w3.eth.get_transaction = MagicMock(return_value=payload or tx_dict())


# ── 1. Successful subscription acknowledgement + hash notification ───────────

def test_ack_then_hash_notification_end_to_end():
    w = make_watcher()
    tx_return(w)
    ws, caught = drive(w, [ack(), notification(HASH), websockets.ConnectionClosed(None, None)])
    assert caught is None
    assert w._ws_state == WS_SUBSCRIBED
    assert w._subscription_id == SUB_ID
    assert w._subscriptions_confirmed == 1
    assert w._subscription_failures == 0
    # the subscribe request was the ONLY protocol frame sent before the ack
    assert ws.sent[0]["method"] == "eth_subscribe"
    assert ws.sent[0]["id"] == 1
    assert w.stats["notifications_received"] == 1
    assert w.stats["hashes_received"] == 1
    assert w.stats["hashes_accepted"] == 1
    assert w.stats["fetches_succeeded"] == 1
    assert w.stats["transactions_queued"] == 1
    tx = w._queue.get_nowait()
    assert tx["hash"].lower() == HASH
    assert w.stats["queue_full_drops"] == 0
    # on_hash callback evidence (pipeline public-memory path)
    assert w.stats["notifications_wrong_sub"] == 0


def test_on_hash_callback_invoked_for_accepted_hashes():
    seen = []
    w = make_watcher(on_hash_callback=seen.append)
    drive(w, [ack(), notification(HASH), websockets.ConnectionClosed(None, None)])
    assert seen == [HASH]


# ── 2. Full transaction-object notification ──────────────────────────────────

def test_ack_then_full_tx_object_notification():
    w = make_watcher()
    ws, caught = drive(w, [ack(), notification(tx_dict()),
                           websockets.ConnectionClosed(None, None)])
    assert caught is None
    assert w._ws_state == WS_SUBSCRIBED
    assert w.stats["hashes_received"] == 1
    assert w.stats["hashes_accepted"] == 1
    # the tx object came WITH the notification — no eth_getTransactionByHash
    assert w.stats["fetches_attempted"] == 0
    assert w.stats["fetches_succeeded"] == 1
    assert w.stats["transactions_queued"] == 1
    tx = w._queue.get_nowait()
    assert tx["to"] == ROUTER


# ── 3. Explicit JSON-RPC subscription error ──────────────────────────────────

def test_subscription_error_is_rejection_not_success():
    w = make_watcher()
    ws, caught = drive(w, [error()])
    assert isinstance(caught, SubscriptionError)
    assert "not supported" in str(caught)
    # never reported as an active mempool stream
    assert w._ws_state != WS_SUBSCRIBED
    assert w._subscription_id is None
    assert w._subscriptions_confirmed == 0
    assert w.stats["subscription_failures"] == 1
    assert w.stats["subscription_error"]
    # failure evidence includes the provider's message
    assert "filtered subscription not supported" in w.stats["subscription_error"]
    assert w.stats["notifications_received"] == 0


def test_error_response_mid_stream_counts_as_provider_error():
    w = make_watcher()
    ws, caught = drive(w, [ack(), error(req_id=2),
                           notification(HASH), websockets.ConnectionClosed(None, None)])
    assert caught is None
    assert w._ws_state == WS_SUBSCRIBED          # the stream itself stays up
    assert w.stats["provider_errors"] == 1
    assert w.stats["notifications_received"] == 1  # the notification still accepted


# ── 4. Delayed acknowledgement / closure before acknowledgement ──────────────

def test_closure_before_acknowledgement_is_subscription_failure():
    w = make_watcher()
    ws, caught = drive(w, [websockets.ConnectionClosed(None, None)])
    assert isinstance(caught, SubscriptionError)
    assert "closed before acknowledgement" in str(caught)
    assert w._subscription_id is None
    assert w.stats["subscription_failures"] == 1


def test_ack_timeout_is_subscription_failure():
    w = make_watcher(ack_timeout=0.05)
    ws, caught = drive(w, [])   # server never answers the subscribe request
    assert isinstance(caught, SubscriptionError)
    assert "ack timeout" in str(caught)
    assert w._subscription_id is None
    assert w.stats["subscription_failures"] == 1


# ── 5. Unrelated responses and keepalive replies ─────────────────────────────

def test_unrelated_and_keepalive_replies_do_not_disturb_the_stream():
    w = make_watcher()
    tx_return(w)
    ws, caught = drive(w, [
        ack(),
        json.dumps({"jsonrpc": "2.0", "id": 999, "result": "0x1"}),   # keepalive reply
        json.dumps({"jsonrpc": "2.0", "id": 42, "result": "whatever"}),  # unrelated
        notification(HASH),
        websockets.ConnectionClosed(None, None),
    ])
    assert caught is None
    assert w._ws_state == WS_SUBSCRIBED
    assert w.stats["unrelated_responses"] == 2
    assert w.stats["notifications_received"] == 1
    assert w.stats["hashes_accepted"] == 1
    assert w.stats["transactions_queued"] == 1


def test_malformed_payloads_are_counted_not_fatal():
    w = make_watcher()
    tx_return(w)
    ws, caught = drive(w, [ack(), "not json at all", notification(HASH),
                           websockets.ConnectionClosed(None, None)])
    assert caught is None
    assert w.stats["malformed_messages"] == 1
    assert w.stats["transactions_queued"] == 1


def test_subscription_notification_without_result_payload_counted_malformed():
    w = make_watcher()
    tx_return(w)
    ws, caught = drive(w, [ack(), notification(None), notification(HASH),
                           websockets.ConnectionClosed(None, None)])
    assert caught is None
    assert w.stats["malformed_messages"] == 1
    assert w.stats["transactions_queued"] == 1


# ── 6. Incorrect subscription ID ─────────────────────────────────────────────

def test_notification_with_wrong_subscription_id_is_rejected():
    w = make_watcher()
    tx_return(w, tx_dict(hash_=HASH2))
    ws, caught = drive(w, [ack(SUB_ID), notification(HASH, sub="0x99"),
                           notification(HASH2, sub=SUB_ID),
                           websockets.ConnectionClosed(None, None)])
    assert caught is None
    assert w._subscription_id == SUB_ID
    assert w.stats["notifications_wrong_sub"] == 1
    assert w.stats["notifications_received"] == 1   # only OUR subscription
    assert w._queue.get_nowait()["hash"].lower() == HASH2


# ── 7. Duplicate notifications ───────────────────────────────────────────────

def test_duplicate_notifications_counted_and_dropped():
    w = make_watcher()
    tx_return(w)
    ws, caught = drive(w, [ack(), notification(HASH), notification(HASH),
                           notification(HASH), notification(HASH2),
                           websockets.ConnectionClosed(None, None)])
    assert caught is None
    assert w.stats["duplicates_dropped"] == 2
    assert w.stats["hashes_accepted"] == 2
    assert w.stats["transactions_queued"] == 2


# ── 8. Reconnection after a failed subscription ──────────────────────────────

def test_reconnect_after_failed_subscription_succeeds_and_reports_reason():
    w = make_watcher(max_fetches_per_second=50.0)
    tx_return(w)
    failed = FakeWebSocket([error()])           # first connect: provider rejects
    connects = []

    def _good_socket():
        # every reconnect re-subscribes on a fresh connection; the request id
        # increments per connect attempt, so the ack must match it
        return FakeWebSocket([ack(req_id=len(connects)), notification(HASH),
                              websockets.ConnectionClosed(None, None)])

    import contextlib

    async def main():
        def _connect_factory(url, **kwargs):
            connects.append(1)
            if len(connects) == 1:
                return _FakeConnectContext(failed)
            return _FakeConnectContext(_good_socket())

        with patch("mempool.watcher.websockets.connect", side_effect=_connect_factory):
            watcher_task = asyncio.create_task(w.run())
            try:
                deadline = time.monotonic() + 5.0
                # the subscribed window between the notification and the
                # scripted close is sub-millisecond — poll for the DURABLE
                # evidence: a queued transaction and confirmed re-subscriptions
                while w.stats["transactions_queued"] < 1 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
            finally:
                w.stop()
                watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher_task

    asyncio.run(main())

    assert len(connects) >= 2, "the watcher must reconnect after the failed subscription"
    assert w.stats["subscriptions_confirmed"] >= 1   # re-subscription SUCCEEDED
    assert w.stats["subscription_failures"] >= 1     # the rejection is preserved
    assert "not supported" in w.stats["subscription_error"]
    assert w.stats["transactions_queued"] == 1


# ── 9. Rate-limit and transaction-queue overflow accounting ──────────────────

def test_rate_limited_hash_is_dropped_and_counted():
    w = make_watcher(max_fetches_per_second=5.0)
    tx_return(w)

    async def main():
        w._queue = asyncio.Queue(maxsize=10)
        w._tokens = 0.0                       # exhaust the token bucket
        w._last_refill = time.monotonic()     # no refill window yet
        await w._fetch_and_enqueue(HASH)      # needs a fetch → no token

    asyncio.run(main())
    assert w.stats["fetches_dropped"] == 1
    assert w.stats["fetches_attempted"] == 0   # NOT disguised as a fetch
    assert w.stats["fetches_succeeded"] == 0
    assert w.stats["transactions_queued"] == 0


def test_failed_fetch_is_counted_never_disguised():
    w = make_watcher()
    w._w3.eth.get_transaction = MagicMock(side_effect=RuntimeError("tx not found"))

    async def main():
        w._queue = asyncio.Queue(maxsize=10)
        w._semaphore = asyncio.Semaphore(10)
        await w._fetch_and_enqueue(HASH)

    asyncio.run(main())
    assert w.stats["fetches_failed"] == 1
    assert w.stats["fetches_succeeded"] == 0
    assert w.stats["transactions_queued"] == 0


def test_transaction_queue_overflow_is_explicit_and_counted():
    w = make_watcher(queue_size=1)

    async def main():
        w._queue = asyncio.Queue(maxsize=1)
        w._semaphore = asyncio.Semaphore(10)
        await w._fetch_and_enqueue(HASH, tx_obj=tx_dict())    # fills the queue
        assert w._transactions_queued == 1
        await w._fetch_and_enqueue(HASH2, tx_obj=tx_dict(HASH2))  # overflow

    asyncio.run(main())
    assert w.stats["transactions_queued"] == 1
    assert w.stats["queue_full_drops"] == 1


def test_inflight_fetch_cap_drops_excess_hashes():
    w = make_watcher(max_inflight_fetches=2)

    async def main():
        w._queue = asyncio.Queue(maxsize=10)
        w._semaphore = asyncio.Semaphore(10)
        w._w3.eth.get_transaction = MagicMock(return_value=tx_dict())

        hold = asyncio.Event()

        async def fake_executor(executor, fn, *args):
            await hold.wait()          # keep the fetches in-flight
            return tx_dict()

        with patch.object(asyncio, "get_event_loop") as mock_loop:
            mock_loop.return_value = SimpleNamespace(run_in_executor=fake_executor)
            w._spawn_fetch(HASH)
            w._spawn_fetch(HASH2)
            await asyncio.sleep(0.02)                       # both parked in-flight
            assert w.stats["inflight_fetches"] == 2
            w._spawn_fetch("0x" + "ee" * 32)                # third → over the cap
            assert w.stats["fetch_tasks_overflow"] == 1
            hold.set()                                      # release all

    asyncio.run(main())
    assert w.stats["inflight_fetches_max"] == 2
    assert w.stats["fetch_tasks_overflow"] == 1


# ── 10. Configuration values actually reaching the watcher ───────────────────

def test_config_values_reach_the_watcher():
    import mempool.pipeline as pipeline_mod

    captured = {}

    class StubWatcher:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    env = {"MEMPOOL_FETCHES_PER_SECOND": "7.5"}   # legacy override set
    with patch.object(pipeline_mod, "MEMPOOL_MAX_FETCHES_PER_SECOND", 3.5), \
         patch.object(pipeline_mod, "MEMPOOL_QUEUE_SIZE", 123), \
         patch.object(pipeline_mod, "MEMPOOL_MAX_INFLIGHT_FETCHES", 17), \
         patch.object(pipeline_mod, "MempoolWatcher", StubWatcher), \
         patch.dict("os.environ", env, clear=False):
        fps, qsize, inflight, source = pipeline_mod.resolve_mempool_settings(env)
        assert (fps, qsize, inflight) == (7.5, 123, 17)
        assert "legacy" in source

        w3 = MagicMock()
        w3.eth.chain_id = 1
        pipeline = pipeline_mod.MempoolPipeline(w3, MagicMock(), MagicMock())
        assert captured["max_fetches_per_second"] == 7.5
        assert captured["queue_size"] == 123
        assert captured["max_inflight_fetches"] == 17
        assert captured["chain_id"] == 1


def test_config_default_path_without_legacy_override():
    import mempool.pipeline as pipeline_mod
    with patch.object(pipeline_mod, "MEMPOOL_MAX_FETCHES_PER_SECOND", 5.0), \
         patch.object(pipeline_mod, "MEMPOOL_QUEUE_SIZE", 500), \
         patch.object(pipeline_mod, "MEMPOOL_MAX_INFLIGHT_FETCHES", 100), \
         patch.dict("os.environ", {}, clear=True):
        fps, qsize, inflight, source = pipeline_mod.resolve_mempool_settings({})
        assert (fps, qsize, inflight) == (5.0, 500, 100)
        assert source == "config.MEMPOOL_MAX_FPS"


# ── Filter lifecycle (WS-A §2B) ──────────────────────────────────────────────

def test_server_filter_is_router_snapshot_and_dynamic_targets_stay_local():
    w = make_watcher()
    tx_return(w)
    ws, caught = drive(w, [ack(), notification(HASH),
                           websockets.ConnectionClosed(None, None)])
    assert caught is None
    # server-side filter snapshotted from the ROUTER set at subscribe time
    to_filter = ws.sent[0]["params"][1]["toAddress"]
    assert to_filter == [ROUTER]
    assert w.stats["filter_addresses"] == 1
    # dynamic adds after subscribe: local acceptance only, no new subscribe
    frames_before = len(ws.sent)
    w.add_target_addresses({POOL})
    assert len(ws.sent) == frames_before          # no resubscribe churn
    assert POOL in w._targets
    assert w.stats["targets_dynamic_added"] == 1
    # a pending tx to the dynamically added pool is filtered LOCALLY (it would
    # not be delivered by the fixed server filter, and that mismatch is
    # observable via counters rather than hidden)
    assert w.stats["watched_targets"] == 2


def test_filter_truncation_at_documented_limit_is_recorded():
    routers = {f"0x{i:040x}" for i in range(1200)}
    w = make_watcher(router_addresses=routers)
    ws, caught = drive(w, [ack(), websockets.ConnectionClosed(None, None)])
    assert caught is None
    to_filter = ws.sent[0]["params"][1]["toAddress"]
    assert len(to_filter) == 1000                    # documented provider limit
    assert w.stats["filter_truncated"] == 200
    assert w.stats["filter_addresses"] == 1000


def test_unsupported_chain_falls_back_to_standard_subscription():
    from mempool.providers import subscription_for
    # Arbitrum (42161) is NOT in the documented filtered set → standard sub
    assert subscription_for(42161) == "newPendingTransactions"
    assert subscription_for(10) == "newPendingTransactions"
    # documented chains keep the filtered subscription
    assert subscription_for(1) == "alchemy_pendingTransactions"
    assert subscription_for(11155111) == "alchemy_pendingTransactions"
    assert subscription_for(137) == "alchemy_pendingTransactions"


def test_status_snapshot_distinguishes_stages():
    # connected but not subscribed
    w = make_watcher()
    ws, caught = drive(w, [error()])
    snap = w.status()
    assert snap["stage"] == "subscription_rejected"

    # subscribed but no notifications received
    w2 = make_watcher()
    drive(w2, [ack(), websockets.ConnectionClosed(None, None)])
    snap2 = w2.status()
    assert snap2["stage"] == "subscribed_no_notifications"
    assert snap2["ws_state"] == WS_SUBSCRIBED

    # subscribed, notifications, but nothing accepted (all wrong-sub)
    w3 = make_watcher()
    drive(w3, [ack(), notification(HASH, sub="0xdead"),
               websockets.ConnectionClosed(None, None)])
    snap3 = w3.status()
    # a wrong-subscription message is NOT one of our notifications — the
    # stage stays at subscribed_no_notifications and the rejection evidence
    # lives in the counter
    assert snap3["stage"] == "subscribed_no_notifications"
    assert snap3["notifications_wrong_sub"] == 1

    # notifications received but no hashes accepted (our subscription's
    # notification carried an unusable payload → nothing handed to fetch work)
    w3b = make_watcher()
    drive(w3b, [ack(), notification(None), websockets.ConnectionClosed(None, None)])
    snap3b = w3b.status()
    assert snap3b["stage"] == "notifications_no_hashes_accepted"
    assert snap3b["notifications_received"] == 1
    assert snap3b["hashes_accepted"] == 0

    # hashes accepted but retrieval failed
    w4 = make_watcher()
    w4._w3.eth.get_transaction = MagicMock(side_effect=RuntimeError("tx not found"))

    async def fail_fetch():
        w4._queue = asyncio.Queue(maxsize=10)
        w4._semaphore = asyncio.Semaphore(10)
        await w4._fetch_and_enqueue(HASH)

    asyncio.run(fail_fetch())
    # accepted-hash evidence exists from an earlier notification; probe the
    # counters on a watcher whose stream is (as the counters imply) subscribed
    w4._notifications_received = 1
    w4._hashes_accepted = 1
    w4._ws_state = WS_SUBSCRIBED
    snap4 = w4.status()
    assert snap4["stage"] == "hashes_accepted_retrieval_failed"

    # fetched but filtered
    w5 = make_watcher()
    w5._w3.eth.get_transaction = MagicMock(return_value=tx_dict(to=POOL))  # not a target

    async def filtered_fetch():
        w5._queue = asyncio.Queue(maxsize=10)
        w5._semaphore = asyncio.Semaphore(10)
        await w5._fetch_and_enqueue(HASH)

    asyncio.run(filtered_fetch())
    w5._notifications_received = 1
    w5._hashes_accepted = 1
    w5._fetches_succeeded = 1
    w5._ws_state = WS_SUBSCRIBED
    snap5 = w5.status()
    assert snap5["stage"] == "fetched_but_filtered"
    assert snap5["fetches_filtered"] == 1

    # queued
    w6 = make_watcher()

    async def queued():
        w6._queue = asyncio.Queue(maxsize=10)
        w6._semaphore = asyncio.Semaphore(10)
        await w6._fetch_and_enqueue(HASH, tx_obj=tx_dict())

    asyncio.run(queued())
    w6._notifications_received = 1
    w6._hashes_accepted = 1
    w6._ws_state = WS_SUBSCRIBED
    snap6 = w6.status()
    assert snap6["stage"] == "transactions_queued"
    assert snap6["transactions_queued"] == 1

    # queue overflow observed
    w7 = make_watcher(queue_size=1)

    async def overflow():
        w7._queue = asyncio.Queue(maxsize=1)
        w7._semaphore = asyncio.Semaphore(10)
        await w7._fetch_and_enqueue(HASH, tx_obj=tx_dict())
        await w7._fetch_and_enqueue(HASH2, tx_obj=tx_dict(HASH2))

    asyncio.run(overflow())
    w7._notifications_received = 1
    w7._hashes_accepted = 1
    w7._ws_state = WS_SUBSCRIBED
    snap7 = w7.status()
    assert snap7["queue_overflow_observed"] is True
    assert snap7["queue_full_drops"] == 1
