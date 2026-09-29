"""
mempool/watcher.py — WebSocket mempool subscriber.

Flow:
  1. Opens a raw WebSocket to the Alchemy endpoint (same URL as main RPC)
  2. Subscribes to newPendingTransactions — receives tx hashes only (free tier)
  3. For each hash: checks a CU budget, then fetches full tx via eth_getTransactionByHash
  4. Filters tx.to against known router addresses immediately
  5. Pushes surviving txs onto an asyncio.Queue for the decoder pipeline

CU budget
─────────
Alchemy free tier: ~300M CU/month ≈ 10M CU/day ≈ 115 CU/second sustained.
eth_getTransactionByHash costs ~20 CU.
That allows ~5 fetches/second sustained without touching block-watching budget.

We therefore cap mempool fetches at MAX_FETCHES_PER_SECOND (configurable).
When the cap is hit, incoming hashes are discarded (not queued) — we lose
some pending txs but protect the RPC budget. Since we only care about DEX
swaps (a small fraction of mempool), selective loss is acceptable.
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
from mempool.providers import subscription_for

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

        # CU / rate tracking
        self._hashes_received  = 0
        self._fetches_attempted = 0
        self._fetches_succeeded = 0
        self._fetches_filtered  = 0   # fetched but not a router tx
        self._fetches_dropped   = 0   # dropped due to rate limit
        self._cu_estimate       = 0   # rough CU consumed

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
            "hashes_received":   self._hashes_received,
            "fetches_attempted": self._fetches_attempted,
            "fetches_succeeded": self._fetches_succeeded,
            "fetches_filtered":  self._fetches_filtered,
            "fetches_dropped":   self._fetches_dropped,
            "queue_size":        self._queue.qsize() if self._queue else 0,
            "cu_estimate":       self._cu_estimate,
            "stalked_count":     len(self._stalked),
            "watched_targets":   len(self._targets),
        }

    def set_target_addresses(self, addresses: set[str]) -> None:
        """Replace the watched protocol/pool target set for this chain."""
        self._targets = {a.lower() for a in addresses if a}

    def add_target_addresses(self, addresses: set[str]) -> None:
        self._targets.update(a.lower() for a in addresses if a)

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
        backoff = 3
        while self._running:
            try:
                # if CHAIN_ID == 1:
                #     await self._connect_blockchain_mempool()
                # else:
                await self._connect_and_listen()
                backoff = 3  # Reset backoff on successful connection that eventually closes
            except Exception as e:
                log.warning(f"Disconnected: {e} — reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60)

    async def _connect_and_listen(self) -> None:
        log.info(f"Connecting to {self._url[:50]}...")
        async with websockets.connect(
            self._url,
            ping_interval=20,
            ping_timeout=30,
            max_size=2**20,  # 1MB max message
            logger=None,  # Suppress internal websockets asyncio connection warnings
        ) as ws:
            subscription = subscription_for(self._chain_id, prefer_filtered=True)
            params = [subscription]
            if subscription == "alchemy_pendingTransactions":
                # Start narrow. Full tx payloads are requested only for target addresses.
                params.append({"toAddress": list(self._targets)[:1000], "hashesOnly": False})
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": params,
            }
            await ws.send(json.dumps(subscribe_msg))
            log.info(f"Subscribed to {subscription} | chain={self._chain_id} | targets={len(self._targets)}")

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data = json.loads(raw)
                    
                    # subscription params.result can be a hash (standard) or a dict (Alchemy)
                    params = data.get("params", {})
                    result = params.get("result")
                    
                    if isinstance(result, dict):
                        # Full transaction object received
                        tx_hash = result.get("hash")
                        if tx_hash:
                            self._hashes_received += 1
                            if not self._deduper.add(tx_hash):
                                continue
                            if self._on_hash: self._on_hash(tx_hash)
                            asyncio.create_task(self._fetch_and_enqueue(tx_hash, tx_obj=result))
                    elif isinstance(result, str):
                        # Only hash received
                        self._hashes_received += 1
                        if not self._deduper.add(result):
                            continue
                        if self._on_hash: self._on_hash(result)
                        asyncio.create_task(self._fetch_and_enqueue(result))
                    elif "result" in data and isinstance(data["result"], str):
                        # Initial subscription response (subscription ID)
                        pass

                except asyncio.TimeoutError:
                    # Keepalive ping
                    try:
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": 999,
                            "method": "eth_chainId", "params": []
                        }))
                    except Exception:
                        raise  # triggers reconnect

                except websockets.ConnectionClosed:
                    raise

                except Exception as e:
                    log.warning(f"recv error: {e}")
                    await asyncio.sleep(0.1)

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
                                    asyncio.create_task(self._fetch_and_enqueue(tx_hash))
                            
                        except json.JSONDecodeError:
                            log.warning("Raw message:", message)
                            
                except websockets.exceptions.ConnectionClosed as e:
                    log.warning(f"Connection closed by server: {e}")
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

    async def _fetch_and_enqueue(self, tx_hash: str, tx_obj: Optional[dict] = None) -> None:
        """Fetch full tx if not provided, filter by router, push to queue."""
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

                # Filter: keep if to router, OR if from/to is stalked
                tx_to = (tx.get("to") or "").lower()
                tx_from = (tx.get("from") or "").lower()
                
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
                    norm_tx["_stalked_hit"] = True # Hidden tag
                
                self._queue.put_nowait(norm_tx)

            except Exception as e:
                # tx not found (already mined/dropped) is common — silence it
                if "not found" not in str(e).lower():
                    log.debug(f"fetch error {tx_hash[:10]}: {e}")