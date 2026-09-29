"""Canonical block information and protocol state synchronization.

Phase 19: separates event detection from protocol-specific state extraction.
The block processor should identify *what changed*; adapters remain the authority
for *how to read the resulting state*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List

from utils.multicall3 import multicall3_bulk_refresh

from log import get_logger

log = get_logger("block_information")


def _topic(w3, signature: str) -> str:
    return w3.keccak(text=signature).hex().lower()


@dataclass(frozen=True)
class ProtocolEventSpec:
    protocol: str
    event: str
    topic: str


@dataclass(frozen=True)
class PoolStateChange:
    chain_id: int
    block_number: int
    block_hash: str
    pool_address: str
    protocol: str
    event: str
    transaction_hash: str = ""
    log_index: int = -1
    removed: bool = False
    is_trade: bool = False


@dataclass
class BlockContext:
    chain_id: int
    number: int
    block_hash: str
    parent_hash: str
    timestamp: int
    base_fee: int | None = None
    gas_limit: int = 0
    gas_used: int = 0
    coinbase: str = ""
    state_changes: list[PoolStateChange] = field(default_factory=list)
    discovery_events: list[dict[str, Any]] = field(default_factory=list)
    raw_log_count: int = 0
    ignored_log_count: int = 0

    @property
    def touched_pools(self) -> set[str]:
        return {x.pool_address for x in self.state_changes if not x.removed}


class ProtocolEventRegistry:
    """Protocol-aware event signatures used to identify changed pools.

    Logs are subsequently gated against the known-pool registry, so generic
    signatures such as Sync/Swap do not cause unrelated contracts to refresh.
    """

    SIGNATURES = (
        ("uniswap_v2", "Sync", "Sync(uint112,uint112)"),
        ("uniswap_v3", "Swap", "Swap(address,address,int256,int256,uint160,uint128,int24)"),
        ("uniswap_v3", "Mint", "Mint(address,address,int24,int24,uint128,uint256,uint256)"),
        ("uniswap_v3", "Burn", "Burn(address,int24,int24,uint128,uint256,uint256)"),
        ("curve", "TokenExchange", "TokenExchange(address,int128,uint256,int128,uint256)"),
        ("curve", "TokenExchangeUnderlying", "TokenExchangeUnderlying(address,int128,uint256,int128,uint256)"),
        ("balancer_v2", "Swap", "Swap(bytes32,address,address,uint256,uint256)"),
        ("syncswap", "Swap", "Swap(address,uint256,uint256,uint256,uint256)"),
        ("aerodrome_v2", "Swap", "Swap(address,uint256,uint256,uint256,uint256)"),
        ("aerodrome_v2", "Sync", "Sync(uint112,uint112)"),
        ("aerodrome_v2", "Mint", "Mint(address,uint256,uint256)"),
        ("aerodrome_v2", "Burn", "Burn(address,uint256,uint256)"),
    )

    def __init__(self, w3):
        self.w3 = w3
        self.by_topic: dict[str, list[ProtocolEventSpec]] = {}
        for protocol, event, signature in self.SIGNATURES:
            spec = ProtocolEventSpec(protocol, event, _topic(w3, signature))
            self.by_topic.setdefault(spec.topic, []).append(spec)

    @property
    def topics(self) -> list[str]:
        return list(self.by_topic)

    @staticmethod
    def _topic_key(topic0: Any) -> str:
        """Normalize HexBytes/bytes/string topics to one canonical lookup key."""
        if topic0 is None:
            return ""
        if hasattr(topic0, "hex"):
            return topic0.hex().lower()
        return str(topic0).lower()

    def classify(self, topic0: Any) -> list[ProtocolEventSpec]:
        return self.by_topic.get(self._topic_key(topic0), [])


class BlockInformationCollector:
    """Build a canonical BlockContext and identify protocol state changes."""

    def __init__(self, w3, chain_id: int, graph):
        self.w3 = w3
        self.chain_id = chain_id
        self.graph = graph
        self.events = ProtocolEventRegistry(w3)

    @staticmethod
    def _hex(value: Any) -> str:
        if value is None:
            return ""
        return value.hex() if hasattr(value, "hex") else str(value)

    def collect(self, block_number: int) -> BlockContext:
        header = self.w3.eth.get_block(block_number)
        block_hash = self._hex(getattr(header, "hash", header.get("hash")))
        parent_hash = self._hex(getattr(header, "parentHash", header.get("parentHash")))
        ctx = BlockContext(
            chain_id=self.chain_id,
            number=block_number,
            block_hash=block_hash,
            parent_hash=parent_hash,
            timestamp=int(getattr(header, "timestamp", header.get("timestamp", 0))),
            base_fee=getattr(header, "baseFeePerGas", header.get("baseFeePerGas")),
            gas_limit=int(getattr(header, "gasLimit", header.get("gasLimit", 0))),
            gas_used=int(getattr(header, "gasUsed", header.get("gasUsed", 0))),
            coinbase=self._hex(getattr(header, "miner", header.get("miner", ""))).lower(),
        )
        logs = self.w3.eth.get_logs({
            "fromBlock": block_number,
            "toBlock": block_number,
            "topics": [self.events.topics],
        })
        ctx.raw_log_count = len(logs)
        known = {a.lower(): edges for a, edges in self.graph._pool_index.items()}
        for entry in logs:
            address = str(entry["address"]).lower()
            specs = self.events.classify(entry["topics"][0]) if entry.get("topics") else []
            if not specs:
                ctx.ignored_log_count += 1
                continue
            if address not in known:
                # A recognized protocol event from an unknown contract is a
                # high-confidence discovery signal.  Do not perform RPC work
                # on the block thread; the crawler consumes these hints in the
                # background.
                for spec in specs:
                    ctx.discovery_events.append({
                        "pool_address": address,
                        "protocol": spec.protocol,
                        "event": spec.event,
                        "transaction_hash": self._hex(entry.get("transactionHash")),
                        "block_number": block_number,
                        "block_hash": block_hash,
                        "topics": [self._hex(x) for x in entry.get("topics", [])],
                        "data": self._hex(entry.get("data")),
                    })
                ctx.ignored_log_count += 1
                continue
            tx = self._hex(entry.get("transactionHash"))
            li = int(entry.get("logIndex", -1))
            removed = bool(entry.get("removed", False))
            for spec in specs:
                # A known address can theoretically be represented by multiple
                # protocol labels; retain the graph's canonical protocol.
                graph_protocols = {getattr(e, "protocol", "") for e in known[address]}
                if spec.protocol not in graph_protocols:
                    ctx.ignored_log_count += 1
                    continue
                ctx.state_changes.append(PoolStateChange(
                    self.chain_id, block_number, block_hash, address,
                    spec.protocol, spec.event, tx, li, removed,
                    is_trade=spec.event in {"Swap", "TokenExchange", "TokenExchangeUnderlying"},
                ))
                break
        return ctx


    def collect_range(self, start_block: int, end_block: int, *, chunk_size: int = 100) -> list[BlockContext]:
        """Collect canonical state-change information over a range.

        Catch-up uses the same event registry/gating semantics as live blocks,
        but batches log transport. Headers are only required for the tip in the
        catch-up path because historical state is synchronized once at the tip.

        NOTE: catch-up intentionally does NOT perform new-pool discovery.
        Discovery is a live-block/crawler responsibility (see collect() and
        crawler.py) — during catch-up we only care about refreshing pools we
        already know about, so logs from unknown addresses are simply
        ignored here rather than being queued (a previous version of this
        method built a discovery_events dict for them, but nothing ever
        consumed it during catch-up — _perform_fast_catchup in main.py only
        reads ctx.touched_pools — so it was dead bookkeeping, not a real
        capability).
        """
        if end_block < start_block:
            return []
        known = {a.lower(): edges for a, edges in self.graph._pool_index.items()}
        grouped: dict[int, list[PoolStateChange]] = {}
        for chunk_start in range(start_block, end_block + 1, chunk_size):
            chunk_end = min(end_block, chunk_start + chunk_size - 1)
            logs = self.w3.eth.get_logs({
                "fromBlock": chunk_start,
                "toBlock": chunk_end,
                "topics": [self.events.topics],
            })
            for entry in logs:
                address = str(entry["address"]).lower()
                specs = self.events.classify(entry["topics"][0]) if entry.get("topics") else []
                if not specs or address not in known:
                    continue
                graph_protocols = {getattr(e, "protocol", "") for e in known[address]}
                spec = next((x for x in specs if x.protocol in graph_protocols), None)
                if spec is None:
                    continue
                bn = int(entry.get("blockNumber", 0))
                bh = self._hex(entry.get("blockHash"))
                grouped.setdefault(bn, []).append(PoolStateChange(
                    self.chain_id, bn, bh, address, spec.protocol, spec.event,
                    self._hex(entry.get("transactionHash")), int(entry.get("logIndex", -1)),
                    bool(entry.get("removed", False)),
                    is_trade=spec.event in {"Swap", "TokenExchange", "TokenExchangeUnderlying"},
                ))
        contexts = []
        for bn in sorted(grouped):
            changes = grouped[bn]
            # The block hash comes from the canonical log itself. We deliberately
            # avoid N historical header RPCs during catch-up.
            contexts.append(BlockContext(self.chain_id, bn, changes[0].block_hash, "", 0, state_changes=changes, raw_log_count=len(changes)))
        log.info("[CATCHUP] range=%s-%s tracked_events=%d touched_pools=%d blocks_with_events=%d",
                 start_block, end_block, sum(len(c.state_changes) for c in contexts),
                 len({p for c in contexts for p in c.touched_pools}), len(contexts))
        return contexts


class ProtocolStateSynchronizer:
    """Refresh touched pools using protocol-specific state readers."""

    def __init__(self, w3, graph, cache=None):
        self.w3 = w3
        self.graph = graph
        self.cache = cache

    def refresh(self, ctx: BlockContext) -> set[str]:
        """Refresh touched pools, batching ALL protocols through Multicall3.

        Native V2/V3 pools use the fast getReserves()/slot0()/liquidity()
        path. Other protocols (Curve, Balancer, SyncSwap, Aerodrome, ...) are
        batched separately through their adapter's build_refresh_calls()/
        decode_refresh_result() (see registry.refresh_pools_batched and
        protocols/base.py) — previously these were refreshed one pool at a
        time via individual RPC round-trips.
        """
        refreshed: set[str] = set()
        v2v3: set[str] = set()
        other: set[str] = set()
        for address in ctx.touched_pools:
            protocol = self._protocol(address)
            if protocol in {"uniswap_v2", "uniswap_v3"}:
                v2v3.add(address)
            else:
                other.add(address)

        if v2v3:
            try:
                done = self._refresh_multicall3(v2v3) or set()
                refreshed.update(done)
            except Exception as exc:
                log.warning("Multicall3 state refresh failed for %d pools: %s", len(v2v3), exc)
                for address in v2v3:
                    self.graph.mark_pool_state(address, status="UNCERTAIN", source=f"multicall_error:{type(exc).__name__}")

        if other:
            try:
                from registry import refresh_pools_batched
                done = refresh_pools_batched(self.w3, self.graph, self.cache, other)
                refreshed.update(done)
                for address in other - done:
                    self.graph.mark_pool_state(address, status="UNCERTAIN", source="adapter_refresh_incomplete")
            except Exception as exc:
                log.warning("Batched adapter refresh failed for %d pools: %s", len(other), exc)
                for address in other:
                    self.graph.mark_pool_state(address, status="UNCERTAIN", source=f"refresh_error:{type(exc).__name__}")

        # Stamp only pools that actually refreshed. The state block is the block
        # whose event triggered the refresh, while Multicall3 supplies a single
        # coherent RPC read context for the batch.
        for address in refreshed:
            protocol = self._protocol(address)
            self._stamp(address, ctx, protocol)
        return refreshed

    def refresh_by_addresses(self, addresses: set[str], ctx: BlockContext | None = None) -> set[str]:
        """Refresh a set of known pools, regardless of whether they were touched.

        This is used for explicit discovery events, where the pool is known but
        the event was not emitted in the current block.
        """
        refreshed: set[str] = set()
        v2v3: set[str] = set()
        other: set[str] = set()
        for address in addresses:
            protocol = self._protocol(address)
            if protocol in {"uniswap_v2", "uniswap_v3"}:
                v2v3.add(address)
            else:
                other.add(address)
        log.info("refresh_by_addresses: v2v3=%d other=%d", len(v2v3), len(other))
        if v2v3:
            try:
                done = self._refresh_multicall3(v2v3) or set()
                refreshed.update(done)
            except Exception as exc:
                log.warning("Multicall3 state refresh failed for %d pools: %s", len(v2v3), exc)
                for address in v2v3:
                    self.graph.mark_pool_state(address, status="UNCERTAIN", source=f"multicall_error:{type(exc).__name__}")

        if other:
            try:
                from registry import refresh_pools_batched
                done = refresh_pools_batched(self.w3, self.graph, self.cache, other)
                refreshed.update(done)
                for address in other - done:
                    self.graph.mark_pool_state(address, status="UNCERTAIN", source="adapter_refresh_incomplete")
            except Exception as exc:
                log.warning("Batched adapter refresh failed for %d pools: %s", len(other), exc)
                for address in other:
                    self.graph.mark_pool_state(address, status="UNCERTAIN", source=f"refresh_error:{type(exc).__name__}")

        # Stamp only pools that actually refreshed. The state block is the block
        # whose event triggered the refresh, while Multicall3 supplies a single
        # coherent RPC read context for the batch.
        if ctx is not None:
            for address in refreshed:
                self._stamp(address, ctx, self._protocol(address))
        return refreshed

    def _version(self, address: str) -> int:
        edges = self.graph._pool_index.get(address.lower(), [])
        return int(edges[0].version) if edges else 0

    def _protocol(self, address: str) -> str:
        edges = self.graph._pool_index.get(address.lower(), [])
        return edges[0].protocol if edges else ""

    def _refresh_multicall3(self, addresses: set[str]) -> set[str]:
        """Refresh V2/V3 pools using Multicall3.

        Multicall3 is the canonical batch reader for V2/V3 pools.  It is not
        used for other protocols because their state shape is not reducible to
        the V2/V3 reserve ABI.
        """
        return multicall3_bulk_refresh(self.w3, self.graph, self.cache, addresses) or set()
    
    def _refresh_one(self, address: str, ctx: BlockContext) -> None:
        address = address.lower()
        protocol = self._protocol(address)
        log.info("refresh_one: pool=%s protocol=%s block=%d", address, protocol, ctx.number)
        # Registry owns the existing V2/V3/Curve/Aerodrome/SyncSwap state
        # construction. Import lazily so this information layer remains usable
        # in source-level/unit tests without importing the Web3-heavy graph.
        from registry import update_pool_reserves
        update_pool_reserves(self.w3, self.graph, address, self.cache)

    def _stamp(self, address: str, ctx: BlockContext, protocol: str) -> None:
        edges = self.graph._pool_index.get(address.lower(), [])
        if not edges:
            return
        # Stamp every direction after the protocol-specific refresh. Re-add via
        # the graph's canonical mutation path so adjacency indexes remain equal.
        from dataclasses import replace
        stamped = []
        for e in edges:
            stamped.append(replace(
                e,
                state_block=ctx.number,
                state_block_hash=ctx.block_hash,
                state_source=f"onchain:{protocol}:block_log",
            ))
        if stamped:
            self.graph.upsert_pool_edges(stamped)
