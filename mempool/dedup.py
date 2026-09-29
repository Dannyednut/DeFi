"""Correct mempool deduplication.

Never drop a distinct transaction merely because calldata looks similar.  Two
traders can submit identical swaps with different sizes/nonces and both can
create distinct opportunities.
"""
from __future__ import annotations
import time
from collections import OrderedDict


class TxDeduper:
    def __init__(self, ttl: float = 180.0, max_items: int = 100_000):
        self.ttl = ttl
        self.max_items = max_items
        self._seen: OrderedDict[str, float] = OrderedDict()

    def add(self, tx_hash: str) -> bool:
        key = tx_hash.lower()
        now = time.monotonic()
        old = self._seen.get(key)
        if old is not None and now - old < self.ttl:
            return False
        self._seen[key] = now
        self._seen.move_to_end(key)
        self._trim(now)
        return True

    def _trim(self, now: float) -> None:
        while self._seen and (now - next(iter(self._seen.values())) > self.ttl or len(self._seen) > self.max_items):
            self._seen.popitem(last=False)
