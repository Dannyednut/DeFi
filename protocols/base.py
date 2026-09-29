from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import PoolRef, Quote


class ProtocolAdapter(ABC):
    """Common contract for pool discovery/state/quoting adapters."""

    protocol: str = "unknown"

    def __init__(self, w3, dex_config):
        self.w3 = w3
        self.config = dex_config

    @abstractmethod
    def discover_pair(self, token_in: str, token_out: str) -> list[PoolRef]:
        raise NotImplementedError

    @abstractmethod
    def load_pool(self, pool: PoolRef) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    def quote_exact_in(self, pool: PoolRef, token_in: str, amount_in: int) -> Quote | None:
        raise NotImplementedError

    def supports_graph_reserves(self) -> bool:
        return False

    # ── Batched refresh (Multicall3) ────────────────────────────────────────
    #
    # load_pool() above always does its own individual RPC round-trip(s) —
    # fine for on-demand quoting, but when the block loop needs to refresh
    # many pools of this protocol at once, that becomes one sequential
    # round-trip per pool. Adapters that override the two methods below let
    # the caller (utils.multicall3) fold this protocol's refresh calls into
    # the *same* Multicall3 aggregate3 batch used for every other protocol —
    # Multicall3 only needs (target, callData) per call, so calls for
    # completely different protocols/ABIs can be batched together in one
    # request. Adapters that don't override these (the default here) are
    # refreshed one at a time via load_pool() as a fallback.

    def supports_batched_refresh(self) -> bool:
        return False

    def build_refresh_calls(self, pool: PoolRef) -> list[dict]:
        """Return Multicall3 Call3 dicts ({target, allowFailure, callData})
        needed to refresh this pool's volatile state. Only the minimum
        needed for a *refresh* (not first-time load) — e.g. reserves/
        balances, not token0/token1, which the graph already has cached."""
        return []

    def decode_refresh_result(self, pool: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        """Decode the (success, returnData) tuples for the calls returned by
        build_refresh_calls, in the same order, into the same shape
        load_pool() would return. Return None if any required call failed."""
        return None

    # ── Batched discovery (Multicall3) ──────────────────────────────────────
    #
    # discover_from_event()/discover_from_swap() previously verified each
    # candidate pool one at a time via individual RPC calls (token0/token1
    # introspection, then a factory.getPool() check), even when a whole
    # batch of candidates arrived from the same block or the same drained
    # queue. The two methods below let that verification run as one or two
    # Multicall3 batches across the *whole* candidate set instead — mirroring
    # build_refresh_calls/decode_refresh_result above, but for "is this a
    # real pool, and what are its canonical tokens" rather than "refresh this
    # known pool's reserves".
    #
    # Some protocols need a second, dependent round of calls (e.g. SyncSwap/
    # Aerodrome must read a candidate's own token0()/token1() before they can
    # even build the factory.getPool(token0, token1) call that verifies it).
    # needs_discovery_verification()/build_discovery_verification_calls()/
    # decode_discovery_verification() express that second round; adapters
    # that can verify in one round (Curve, Balancer) simply don't override
    # them, and decode_discovery_verification()'s default passes the first
    # round's result through unchanged.

    def supports_batched_discovery(self) -> bool:
        return False

    def build_discovery_calls(self, candidate: PoolRef) -> list[dict]:
        """Return Multicall3 Call3 dicts needed to read enough state off an
        unverified candidate address to (a) decide if it's a real pool of
        this protocol and (b) recover its canonical token0/token1."""
        return []

    def decode_discovery_result(self, candidate: PoolRef, raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        """Decode build_discovery_calls' results. Return None if the
        candidate doesn't look like a valid pool of this protocol at all."""
        return None

    def needs_discovery_verification(self) -> bool:
        """True if decode_discovery_result's output must still be confirmed
        against a factory/registry before it's trusted (e.g. an attacker can
        deploy a contract exposing the right function signatures without it
        actually being a real pool of this protocol)."""
        return False

    def build_discovery_verification_calls(self, candidate: PoolRef, discovered: dict[str, Any]) -> list[dict]:
        return []

    def decode_discovery_verification(self, candidate: PoolRef, discovered: dict[str, Any], raw_results: list[tuple[bool, bytes]]) -> dict[str, Any] | None:
        """Default: no verification round needed, pass the discovery result
        through unchanged. Adapters with needs_discovery_verification()==True
        must override this to actually check raw_results and return None on
        a mismatch."""
        return discovered

    # ── Batched pair lookup (Multicall3) ─────────────────────────────────────
    #
    # A different direction from discovery above: here token0/token1 are
    # already known (e.g. from a decoded mempool swap path) and we need to
    # ask "does this protocol have a pool for this exact pair, and if so
    # where" — discover_from_swap() previously did this with one
    # adapter.discover_pair(t0, t1) RPC call per (pair, protocol) combination,
    # sequentially. These two let that batch too.

    def supports_batched_pair_lookup(self) -> bool:
        return False

    def build_pair_lookup_calls(self, token0: str, token1: str) -> list[dict]:
        return []

    def decode_pair_lookup(self, token0: str, token1: str, raw_results: list[tuple[bool, bytes]]) -> list[PoolRef]:
        return []
