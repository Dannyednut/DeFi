"""Nonce allocation and replacement tracking for concurrent searcher workers."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any

@dataclass
class NonceReservation:
    nonce: int
    tx_hash: str = ""
    state: str = "reserved"  # reserved/submitted/included/dropped/replaced
    metadata: dict[str, Any] = field(default_factory=dict)

class NonceManager:
    def __init__(self, w3, address: str):
        self.w3 = w3
        self.address = address
        self._lock = asyncio.Lock()
        self._next: int | None = None
        self._reservations: dict[int, NonceReservation] = {}

    async def reserve(self) -> NonceReservation:
        async with self._lock:
            if self._next is None:
                self._next = int(self.w3.eth.get_transaction_count(self.address, "pending"))
            nonce = self._next
            self._next += 1
            r = NonceReservation(nonce)
            self._reservations[nonce] = r
            return r

    async def refresh(self) -> None:
        async with self._lock:
            chain_pending = int(self.w3.eth.get_transaction_count(self.address, "pending"))
            if self._next is None or chain_pending > self._next:
                self._next = chain_pending
            for nonce, reservation in list(self._reservations.items()):
                if nonce < chain_pending and reservation.state in {"reserved", "submitted"}:
                    reservation.state = "included"

    def mark_submitted(self, nonce: int, tx_hash: str) -> None:
        if nonce in self._reservations:
            self._reservations[nonce].tx_hash = tx_hash
            self._reservations[nonce].state = "submitted"

    def mark_replaced(self, nonce: int, tx_hash: str) -> None:
        if nonce in self._reservations:
            self._reservations[nonce].tx_hash = tx_hash
            self._reservations[nonce].state = "replaced"

    def mark_dropped(self, nonce: int) -> None:
        if nonce in self._reservations:
            self._reservations[nonce].state = "dropped"

    def snapshot(self) -> tuple[NonceReservation, ...]:
        return tuple(self._reservations.values())
