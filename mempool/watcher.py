"""
mempool/watcher.py — WebSocket mempool subscriber.

Flow (DSS-WO-MI-106 WS-A):
  1. Opens a raw WebSocket to the provider endpoint
  2. Sends eth_subscribe and WAITS for the JSON-RPC acknowledgement whose
     id matches the request id; a JSON-RPC error response is a subscription
     FAILURE (never a successful connection), and "Subscribed" is logged only
     after the acknowledgement carries the subscription id
  3. Accepts only notifications whose params.subscription matches the
     acknowledged subscription id; acks, keepalive responses, notifications,
     malformed messages and provider errors are counted separately
  4. For each hash notification: dedup, then fetch via eth_getTransactionByHash
     (bounded in-flight tasks + token bucket) or use the full tx object when
     the provider filter already delivered it
  5. Filters tx.to against the local target set and pushes surviving txs onto
     a bounded asyncio.Queue for the decoder pipeline

Subscription/filter lifecycle
─────────────────────────────
Per the provider documentation the filtered pending subscription's address
filter is FIXED at subscribe time and cannot be modified afterwards (the only
lifecycle operation is unsubscribe/resubscribe). The server-side filter is
therefore snapshotted from the configured ROUTER set (stable for the process
lifetime); addresses learned later (mempool.discovered pools) remain LOCAL
acceptance filters only, and every dynamic add is counted so the server/local
mismatch stays observable. No silent fallback to a broader subscription.

CU budget
─────────
Alchemy free tier: ~300M CU/month ≈ 10M CU/day ≈ 115 CU/second sustained.
eth_getTransactionByHash costs ~20 CU.  That allows ~5 fetches/second
sustained without touching block-watching budget.  The fetch cap therefore
defaults to MEMPOOL_MAX_FETCHES_PER_SECOND=5 (config.py).  When the cap is
hit, incoming hashes are dropped AND COUNTED — a dropped transaction is never
reported as a successful fetch or queue insertion.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, TYPE_CHECKING

import websockets
from config import CHAIN_ID
from mempool.dedup import TxDeduper
from mempool.providers import build_subscription_params, ALCHEMY_FILTER_ADDRESS_LIMIT

def _normalise_tx(tx) -> dict:
    """Convert web3 AttributeDict with HexBytes values to a plain str dict."""
    out = {}
    for k, v in tx.items():
        if isinstance(v, (bytes, bytearray)):
            out[k] = v.hex()
        elif hasattr(v, "hex") and callable(v.hex) and not isinstance(v, int):
            # HexBytes subclass
            try:
                out[k] = v.hex()
            except Exception:
                out[k] = str(v)
        else:
            out[k] = v
    return out


if TYPE_CHECKING:
    from web3 import Web3

from log import get_logger
log = get_logger("watcher")


class SubscriptionError(Exception):
    """The provider REJECTED the subscription (JSON-RPC error, ack timeout, or
    a closed connection before the acknowledgement arrived). Distinct from a
    plain connection failure so the reconnect path can preserve the reason."""


# Watcher state machine (observable via .status()):
#   disconnected → connecting → awaiting_ack → subscribed ⇄ (reconnect)
WS_DISCONNECTED = "disconnected"
WS_CONNECTING = "connecting"
WS_AWAITING_ACK = "awaiting_ack"
WS_SUBSCRIBED = "subscribed"


class MempoolWatcher:
    """
    Async WebSocket mempool watcher.

    Usage:
        watcher = MempoolWatcher(rpc_ws_url, w3, router_addresses)
        asyncio.create_task(watcher.run())
        # consume from watcher.queue
    """

    def __init__(
        self,
        rpc_ws_url: str,
        w3: "Web3",
        router_addresses: set[str],
        stalked_addresses: set[str] = None,
        max_fetches_per_second: float = 5.0,
        queue_size: int = 500,
        on_hash_callback: Optional[callable] = None,
        chain_id: Optional[int] = None,
        max_inflight_fetches: int = 100,
        ack_timeout: float = 15.0,
        backoff_base: float = 3.0,
    ):
        self._url              = rpc_ws_url
        self._w3               = w3
        self._chain_id         = int(chain_id if chain_id is not None else w3.eth.chain_id)
        self._routers          = {a.lower() for a in router_addresses}
        self._stalked          = {a.lower() for a in (stalked_addresses or [])}
        self._on_hash          = on_hash_callback
        self._max_fps          = max_fetches_per_second
        self._queue: Optional[asyncio.Queue] = None
        self._queue_size       = queue_size
        self._targets          = set(self._routers)
        self._deduper          = TxDeduper(ttl=180.0)
        self._running          = False

        # DSS-WO-MI-106 WS-A: subscription lifecycle state
        self._ws_state         = WS_DISCONNECTED
        self._subscription      = None          # subscription method name
        self._subscription_id   = None          # id returned by the provider ack
        self._subscription_filter_size = 0     # server-side filter snapshot size
        self._filter_truncated  = 0             # targets dropped by the 1000-address limit
        self._ack_timeout       = ack_timeout
        self._backoff_base      = max(0.05, float(backoff_base))
        self._request_id        = 0             # monotonic JSON-RPC request ids
        self._keepalive_id      = None          # id of the in-flight keepalive request

        # CU / rate tracking
        self._hashes_received  = 0      # notifications carrying a hash/tx payload
        self._notifications_received = 0
        self._notifications_wrong_sub = 0
        self._notifications_unmatched = 0   # eth_subscription messages before/without our ack
        self._duplicates_dropped = 0
        self._hashes_accepted  = 0      # post-dedup hashes handed to fetch work
        self._fetches_attempted = 0
        self._fetches_succeeded = 0
        self._fetches_failed    = 0     # retrieval errors (incl. mined-before-fetch)
        self._fetches_filtered  = 0     # fetched but not a target/stalked tx
        self._fetches_dropped   = 0     # dropped due to rate limit
        self._transactions_queued = 0   # successful queue insertions
        self._queue_full_drops  = 0     # bounded queue overflow (explicit, never silent)
        self._fetch_tasks_overflow = 0  # in-flight cap reached — hash dropped, counted
        self._inflight_fetches  = 0
        self._inflight_fetches_max = 0
        self._max_inflight      = max(1, int(max_inflight_fetches))
        self._malformed_messages = 0
        self._unrelated_responses = 0   # JSON-RPC responses that are neither our ack nor keepalive
        self._provider_errors   = 0     # JSON-RPC error objects received post-ack
        self._keepalives_sent   = 0
        self._subscriptions_confirmed = 0
        self._subscription_failures = 0
        self._subscription_error = ""
        self._targets_dynamic_added = 0
        self._last_notification_ts = 0.0
        self._cu_estimate       = 0     # rough CU consumed

        # Token bucket for rate limiting: refills at max_fps tokens/second
        self._tokens           = max_fetches_per_second
        self._last_refill      = time.monotonic()
        self._semaphore        = None                   # created in run()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def queue(self) -> Optional[asyncio.Queue]:
        return self._queue

    @property
    def stats(self) -> dict:
        return {
            "ws_state":          self._ws_state,
            "hashes_received":   self._hashes_received,
            "notifications_received": self._notifications_received,
            "notifications_wrong_sub": self._notifications_wrong_sub,
            "duplicates_dropped": self._duplicates_dropped,
            "hashes_accepted":   self._hashes_accepted,
            "fetches_attempted": self._fetches_attempted,
            "fetches_succeeded": self._fetches_succeeded,
            "fetches_failed":    self._fetches_failed,
            "fetches_filtered":  self._fetches_filtered,
            "fetches_dropped":   self._fetches_dropped,
            "transactions_queued": self._transactions_queued,
            "queue_full_drops":  self._queue_full_drops,
            "fetch_tasks_overflow": self._fetch_tasks_overflow,
            "inflight_fetches":  self._inflight_fetches,
            "inflight_fetches_max": self._inflight_fetches_max,
            "malformed_messages": self._malformed_messages,
            "unrelated_responses": self._unrelated_responses,
            "provider_errors":   self._provider_errors,
            "keepalives_sent":   self._keepalives_sent,
            "subscriptions_confirmed": self._subscriptions_confirmed,
            "subscription_failures": self._subscription_failures,
            "subscription_error": self._subscription_error,
            "subscription_id":   self._subscription_id,
            "filter_addresses":  self._subscription_filter_size,
            "filter_truncated":  self._filter_truncated,
            "targets_dynamic_added": self._targets_dynamic_added,
            "seconds_since_last_notification": (
                round(time.time() - self._last_notification_ts, 1)
                if self._last_notification_ts else None
            ),
            "queue_size":        self._queue.qsize() if self._queue else 0,
            "cu_estimate":       self._cu_estimate,
            "stalked_count":     len(self._stalked),
            "watched_targets":   len(self._targets),
        }

    def status(self) -> dict:
        """Compact evidence snapshot distinguishing the mempool observation
        stages required by DSS-WO-MI-106 §2D. `stage` names the FURTHEST stage
        that has produced evidence, `stage_detail` the specific counters."""
        s = self.stats
        if self._subscription_failures and self._ws_state != WS_SUBSCRIBED:
            stage = "subscription_rejected"
        elif self._ws_state in (WS_CONNECTING, WS_AWAITING_ACK):
            stage = "connected_not_subscribed"
        elif self._ws_state != WS_SUBSCRIBED:
            stage = "disconnected"
        elif not s["notifications_received"]:
            stage = "subscribed_no_notifications"
        elif not s["hashes_accepted"]:
            stage = "notifications_no_hashes_accepted"
        elif s["fetches_failed"] and not s["fetches_succeeded"] and not s["transactions_queued"]:
            stage = "hashes_accepted_retrieval_failed"
        elif s["fetches_dropped"] and not s["transactions_queued"]:
            stage = "hashes_accepted_rate_limited"
        elif not s["transactions_queued"]:
            stage = "fetched_but_filtered"
        else:
            stage = "transactions_queued"
        s["stage"] = stage
        s["queue_overflow_observed"] = bool(s["queue_full_drops"] or s["fetch_tasks_overflow"])
        return s

    def set_target_addresses(self, addresses: set[str]) -> None:
        """Replace the LOCAL acceptance target set for this chain.

        NOTE (DSS-WO-MI-106 WS-A §2B): this does NOT update the server-side
        subscription filter — the provider documents no post-subscribe filter
        modification. Newly configured addresses take effect as local
        acceptance filters; txs addressed to them are only DELIVERED if they
        also match the fixed server-side filter."""
        new_targets = {a.lower() for a in addresses if a}
        self._targets_dynamic_added += max(0, len(new_targets - self._targets) - 0)
        self._targets = new_targets

    def add_target_addresses(self, addresses: set[str]) -> None:
        fresh = {a.lower() for a in addresses if a} - self._targets
        self._targets_dynamic_added += len(fresh)
        self._targets.update(fresh)

    def set_stalked_addresses(self, addresses: set[str]) -> None:
        """Update the list of Alpha Bot addresses to stalk."""
        self._stalked = {a.lower() for a in addresses}

    def stop(self) -> None:
        self._running = False
        self._queue = None

    # ── Main coroutine ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop — reconnects on disconnect with exponential backoff."""
        # Always recreate asyncio primitives to ensure they bind to the new event loop if restarted
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._semaphore = asyncio.Semaphore(10)

        self._running = True
        backoff = self._backoff_base
        while self._running:
            try:
                await self._connect_and_listen()
                backoff = self._backoff_base  # Reset backoff on successful connection that eventually closes
            except Exception as e:
                # Preserve the failure reason (subscription rejection vs plain
                # disconnect) — MI-106 WS-A requirement 8.
                self._ws_state = WS_DISCONNECTED
                log.warning(f"Mempool stream ended: {type(e).__name__}: {e} — reconnecting in {backoff:.2f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60)

    async def _connect_and_listen(self) -> None:
        log.info(f"Connecting to {self._url[:50]}...")
        self._ws_state = WS_CONNECTING
        async with websockets.connect(
            self._url,
            ping_interval=20,
            ping_timeout=30,
            max_size=2**20,  # 1MB max message
            logger=None,  # Suppress internal websockets asyncio connection warnings
        ) as ws:
            # Server-side filter: the configured ROUTER set, snapshotted once
            # per subscription (provider docs: no post-subscribe modification).
            subscription, params = build_subscription_params(self._chain_id, self._routers)
            filter_obj = params[1] if len(params) > 1 else None
            self._subscription = subscription
            if filter_obj:
                self._subscription_filter_size = len(filter_obj.get("toAddress") or [])
                configured = len({a.lower() for a in self._routers if a})
                if configured > self._subscription_filter_size:
                    self._filter_truncated += configured - self._subscription_filter_size
                    log.warning(
                        "Server-side filter truncated to the documented %d-address limit "
                        "(%d configured addresses dropped): the provider rejects more",
                        ALCHEMY_FILTER_ADDRESS_LIMIT, configured - self._subscription_filter_size,
                    )

            self._request_id += 1
            req_id = self._request_id
            subscribe_msg = {"jsonrpc": "2.0", "id": req_id, "method": "eth_subscribe", "params": params}
            await ws.send(json.dumps(subscribe_msg))
            self._ws_state = WS_AWAITING_ACK
            # NO "Subscribed" log here — the acknowledgement has not arrived.

            # 1) Wait for the JSON-RPC acknowledgement BEFORE declaring the
            #    subscription active. An error response (or a closure/timeout
            #    before the ack) is a SUBSCRIPTION FAILURE.
            subscription_id = await self._await_subscription_ack(ws, req_id)

            # 2) Acknowledged: record the subscription id and only NOW log.
            self._subscription_id = subscription_id
            self._ws_state = WS_SUBSCRIBED
            self._subscriptions_confirmed += 1
            log.info(
                f"Subscribed to {subscription} | chain={self._chain_id} | "
                f"sub_id={subscription_id} | server_filter={self._subscription_filter_size} | "
                f"local_targets={len(self._targets)}"
            )

            # 3) Notification loop — accept only OUR subscription id.
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    self._handle_message(raw)

                except asyncio.TimeoutError:
                    # Keepalive ping — tracked so its response (or error) is
                    # distinguishable from unrelated traffic.
                    self._request_id += 1
                    self._keepalive_id = self._request_id
                    try:
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": self._keepalive_id,
                            "method": "eth_chainId", "params": []
                        }))
                        self._keepalives_sent += 1
                    except Exception:
                        raise  # triggers reconnect

                except websockets.ConnectionClosed:
                    raise

                except json.JSONDecodeError as e:
                    self._malformed_messages += 1
                    log.debug(f"malformed websocket payload: {e}")

    # ── Subscription handshake ────────────────────────────────────────────────

    async def _await_subscription_ack(self, ws, req_id: int) -> str:
        """Wait for the eth_subscribe acknowledgement matching `req_id`.

        Classifies every pre-ack message: the ack (result == subscription id),
        JSON-RPC errors (SUBSCRIPTION FAILURE), unrelated responses, and
        malformed payloads. Raises SubscriptionError on rejection — the caller
        must not report an active mempool stream."""
        deadline = time.monotonic() + self._ack_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._subscription_failures += 1
                self._subscription_error = f"ack timeout after {self._ack_timeout:.0f}s (request id {req_id})"
                raise SubscriptionError(self._subscription_error)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                self._subscription_failures += 1
                self._subscription_error = f"ack timeout after {self._ack_timeout:.0f}s (request id {req_id})"
                raise SubscriptionError(self._subscription_error)
            except websockets.ConnectionClosed as exc:
                self._subscription_failures += 1
                self._subscription_error = f"connection closed before acknowledgement: {exc}"
                raise SubscriptionError(self._subscription_error)
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                self._malformed_messages += 1
                continue
            if not isinstance(data, dict):
                self._malformed_messages += 1
                continue
            if "error" in data:
                self._subscription_failures += 1
                self._subscription_error = f"provider rejected eth_subscribe (id={data.get('id')}): {data['error']}"
                raise SubscriptionError(self._subscription_error)
            if data.get("id") == req_id and isinstance(data.get("result"), str):
                return data["result"]          # the subscription id
            if data.get("method") == "eth_subscription":
                # A notification cannot belong to us before the ack arrived.
                self._notifications_unmatched += 1
                continue
            self._unrelated_responses += 1     # other RPC replies on the socket

    def _handle_message(self, raw) -> None:
        """Classify one post-ack message and update counters/state.

        Distinguishes: subscription notifications (accepted only when
        params.subscription matches the acknowledged id), keepalive/unrelated
        JSON-RPC responses, provider error objects, and malformed payloads."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._malformed_messages += 1
            return
        if not isinstance(data, dict):
            self._malformed_messages += 1
            return

        if "error" in data:
            self._provider_errors += 1
            if data.get("id") is not None and data.get("id") == self._keepalive_id:
                # The keepalive itself failed — the socket is unhealthy.
                raise websockets.ConnectionClosed(None, None)
            log.debug(f"provider error response (id={data.get('id')}): {data['error']}")
            return

        if data.get("method") == "eth_subscription":
            params = data.get("params") or {}
            sub_id = params.get("subscription")
            result = params.get("result")
            if sub_id != self._subscription_id:
                self._notifications_wrong_sub += 1
                return
            self._notifications_received += 1
            self._last_notification_ts = time.time()
            if isinstance(result, dict):
                # Full transaction object received (provider filter with
                # hashesOnly=false)
                tx_hash = result.get("hash")
                if not tx_hash:
                    self._malformed_messages += 1
                    return
                self._hashes_received += 1
                self._accept_hash(str(tx_hash), tx_obj=result)
            elif isinstance(result, str):
                # Hash-only notification
                self._hashes_received += 1
                self._accept_hash(result)
            else:
                self._malformed_messages += 1
            return

        if "result" in data:
            # A JSON-RPC response that is not our subscription ack (e.g. the
            # eth_chainId keepalive reply): keepalive evidence, not a tx.
            self._unrelated_responses += 1
            return

        self._malformed_messages += 1

    def _accept_hash(self, tx_hash: str, tx_obj: Optional[dict] = None) -> None:
        """Dedup + hand off one accepted hash to bounded fetch work."""
        if not self._deduper.add(tx_hash):
            self._duplicates_dropped += 1
            return
        self._hashes_accepted += 1
        if self._on_hash:
            self._on_hash(tx_hash)
        self._spawn_fetch(tx_hash, tx_obj)

    # ── Bounded fetch work ────────────────────────────────────────────────────

    def _spawn_fetch(self, tx_hash: str, tx_obj: Optional[dict] = None) -> None:
        """Create a fetch task, bounded by the in-flight cap.

        DSS-WO-MI-106 WS-A §2C: per-notification task creation previously grew
        without bound — the semaphore only limited CONCURRENT fetches, while
        tasks waiting on it piled up. When the cap is reached the hash is
        dropped AND COUNTED (never silently, never disguised as a fetch)."""
        if self._inflight_fetches >= self._max_inflight:
            self._fetch_tasks_overflow += 1
            return
        task = asyncio.create_task(self._fetch_and_enqueue(tx_hash, tx_obj))
        self._inflight_fetches += 1
        if self._inflight_fetches > self._inflight_fetches_max:
            self._inflight_fetches_max = self._inflight_fetches

        def _release(t: asyncio.Task) -> None:
            self._inflight_fetches = max(0, self._inflight_fetches - 1)

        task.add_done_callback(_release)

    async def _fetch_and_enqueue(self, tx_hash: str, tx_obj: Optional[dict] = None) -> None:
        """Fetch full tx if not provided, filter by target, push to queue."""
        # Rate limit check ONLY if we need to fetch
        if tx_obj is None and not self._consume_token():
            self._fetches_dropped += 1
            return

        async with self._semaphore:
            try:
                if tx_obj is None:
                    self._fetches_attempted += 1
                    tx = await asyncio.get_event_loop().run_in_executor(
                        None,
                        self._w3.eth.get_transaction,
                        tx_hash,
                    )
                    self._cu_estimate += 20  # eth_getTransactionByHash ≈ 20 CU
                    self._fetches_succeeded += 1
                else:
                    tx = tx_obj
                    self._fetches_succeeded += 1
            except Exception as e:
                # Retrieval failed — counted, never disguised as a fetch or a
                # queue insertion. "tx not found" (already mined/dropped) is
                # common and stays quiet, but it is still a failed retrieval.
                self._fetches_failed += 1
                if "not found" not in str(e).lower():
                    log.debug(f"fetch error {tx_hash[:10]}: {e}")
                return

            # Filter: keep if to a target, OR if from/to is stalked
            tx_to = str(tx.get("to") or "").lower()
            tx_from = str(tx.get("from") or "").lower()

            is_target = tx_to in self._targets
            is_stalked = tx_to in self._stalked or tx_from in self._stalked

            if not (is_target or is_stalked):
                self._fetches_filtered += 1
                return

            # Normalise web3 AttributeDict → plain dict with str values.
            # HexBytes fields (input, hash, etc.) must be strings so the
            # decoder can do isinstance(data, str) checks reliably.
            norm_tx = _normalise_tx(tx)

            # Tag stalked hits for the pipeline
            if is_stalked and not is_target:
                norm_tx["_stalked_hit"] = True  # Hidden tag

            try:
                self._queue.put_nowait(norm_tx)
                self._transactions_queued += 1
            except asyncio.QueueFull:
                # Explicit, counted overflow — MI-106 WS-A §2C.
                self._queue_full_drops += 1
                log.warning(
                    "Transaction queue full (size=%d): dropping tx %s… — "
                    "queue_full_drops=%d", self._queue_size, tx_hash[:10],
                    self._queue_full_drops,
                )

    async def _connect_blockchain_mempool(self):
        uri = "wss://ws.blockchain.info/coins"

        headers = {
            "Origin": "https://www.blockchain.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        }

        now = int(time.time())

        async with websockets.connect(uri, extra_headers=headers) as ws:
            log.info("Connected to Blockchain.com Coins WebSocket")

            # Subscribe to ETH pending transactions
            subscribe_msg = {
                "coin": "eth",
                "command": "subscribe",
                "entity": "pending_transaction"
            }

            await ws.send(json.dumps(subscribe_msg))
            log.info("Subscribed to ETH pending transactions")

            async def ping_loop():
                while True:
                    try:
                        await ws.send(json.dumps({"command": "ping"}))
                        await asyncio.sleep(30)
                    except:
                        break

            asyncio.create_task(ping_loop())

            # Listen for messages
            while self._running:
                try:
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            if data.get("transaction", {}).get("timestamp", 0) > now:
                                tx = data.get("transaction") or data.get("data") or data
                                tx_hash = tx.get("hash") or tx.get("tx_hash")
                                if tx_hash and tx.get("to", "").lower() in self._targets:
                                    self._hashes_received += 1
                                    if self._on_hash: self._on_hash(tx_hash)
                                    self._spawn_fetch(tx_hash)

                        except json.JSONDecodeError:
                            log.warning("Raw message:", message)

                except websockets.exceptions.ConnectionClosed as e:
                    log.warning("Connection closed by server: %s", e)
                    raise
                except Exception as e:
                    log.error("Error:", e)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _extract_hash(self, data: dict) -> Optional[str]:
        """Extract tx hash from subscription message."""
        # Standard subscription notification
        if "params" in data and "result" in data.get("params", {}):
            return data["params"]["result"]
        # Some providers return result directly on first response
        if "result" in data and isinstance(data["result"], str) and len(data["result"]) == 66:
            return data["result"]
        return None

    def _refill_tokens(self) -> None:
        """Token bucket refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._max_fps,
            self._tokens + elapsed * self._max_fps,
        )
        self._last_refill = now

    def _consume_token(self) -> bool:
        """Returns True if a fetch token is available."""
        self._refill_tokens()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False
