from __future__ import annotations
from dataclasses import dataclass

@dataclass
class TriggerState:
    tx_hash: str
    first_seen_block: int | None = None
    mined_block: int | None = None
    status: str = "pending"  # pending/mined/replaced/dropped/reverted

class TriggerTracker:
    """Tracks replacement/dropped/mined trigger transactions by sender+nonce."""
    def __init__(self):
        self._by_hash: dict[str, TriggerState] = {}
        self._by_sender_nonce: dict[tuple[str, int], str] = {}

    def observe(self, tx_hash: str, sender: str, nonce: int) -> tuple[str | None, TriggerState]:
        key = (sender.lower(), int(nonce))
        previous = self._by_sender_nonce.get(key)
        if previous and previous != tx_hash:
            old = self._by_hash.get(previous)
            if old and old.status == "pending": old.status = "replaced"
        state = self._by_hash.setdefault(tx_hash, TriggerState(tx_hash))
        self._by_sender_nonce[key] = tx_hash
        return previous, state

    def mark_mined(self, tx_hash: str, block: int, reverted: bool = False):
        state = self._by_hash.setdefault(tx_hash, TriggerState(tx_hash))
        state.mined_block = block
        state.status = "reverted" if reverted else "mined"

    def mark_dropped(self, tx_hash: str):
        state = self._by_hash.setdefault(tx_hash, TriggerState(tx_hash))
        if state.status == "pending": state.status = "dropped"
