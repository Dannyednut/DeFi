"""
Enhanced Token Graph with Bellman-Ford Cycle Detection
=====================================================
- Every token is a node
- Every pool is a directed edge (both directions)
- Edge weight = -log(fee_adjusted_exchange_rate)
- Negative weight cycle = profitable arbitrage
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Optional
from contextlib import suppress
from web3 import Web3
from cache import PoolCache

from log import get_logger
from config import MAX_HOPS, MIN_POOL_LIQUIDITY_USD
from utils.multicall3 import multicall3_bulk_refresh, MULTICALL_CHUNK
from tvl import _WETH_ADDRS, STABLE_TOKEN_SETS, token_decimals, token_price, _NATIVE_PRICE_USD

log = get_logger("graph")


@dataclass
class PoolEdge:
    """Represents a directed edge in the token graph."""
    pool_address: str
    dex_name: str
    token_in: str
    token_out: str
    reserve_in: int
    reserve_out: int
    fee_bps: int
    version: int
    sqrt_price_x96: int = 0
    liquidity: int = 0
    last_updated: float = field(default_factory=time.time)
    
    # NEW: Enhanced metadata
    tvl_usd: float = 0.0
    volume_24h: float = 0.0
    is_stable_pair: bool = False
    is_pending: bool = False
    # Canonical protocol metadata. ``version`` remains for backwards compatibility.
    protocol: str = ""
    pool_token0: str = ""
    pool_token1: str = ""
    metadata: dict = field(default_factory=dict)
    chain_id: int = 0
    state_block: int = 0
    state_block_hash: str = ""
    state_source: str = ""
    state_status: str = "UNKNOWN"

    @property
    def token0_price(self) -> float:
        """Mathematical ratio of token1_wei per token0_wei (Spec 4.1 - Spot Price)."""
        token0 = (self.pool_token0 or self.token_in).lower()

        if self.protocol == "curve":
            return float(self.metadata.get("spot_rate", 0.0) or 0.0)

        if self.version == 2:
            if self.reserve_in == 0:
                return 0.0
            return self.reserve_out / self.reserve_in if self.token_in.lower() == token0 else self.reserve_in / self.reserve_out

        if self.sqrt_price_x96 == 0:
            return 0.0
        price_token1_per_token0 = (self.sqrt_price_x96 / (2 ** 96)) ** 2
        if self.token_in.lower() == token0:
            return price_token1_per_token0
        return 1.0 / price_token1_per_token0 if price_token1_per_token0 > 0 else 0.0

    @property
    def token1_price(self) -> float:
        """Mathematical ratio of token0_wei per token1_wei (Spec 4.1 - Spot Price)."""
        p0 = self.token0_price
        return 1.0 / p0 if p0 > 0 else 0.0

    @property
    def exchange_rate(self) -> float:
        """Fee-adjusted rate: how many tokenOut per 1 tokenIn."""
        fee_mult = 1.0 - (self.fee_bps / 10_000.0)

        # Protocol-specific spot rates are populated by adapters for AMMs whose
        # pricing curve cannot be represented by x*y=k or V3 sqrtPrice alone:
        # Curve's StableSwap invariant, Balancer's weighted constant-product
        # (whose true spot price needs the pool's token weights, not just its
        # raw reserve ratio -- reserve_out/reserve_in is only correct for
        # Balancer in the special case of exactly equal weights), and
        # Aerodrome/Solidly-style "stable" pools specifically (their
        # x^3*y + x*y^3 = k invariant means reserve_out/reserve_in is wrong
        # the same way it would be for Curve -- Aerodrome VOLATILE pools are
        # plain constant-product and correctly fall through to the version==2
        # branch below unchanged).
        #
        # NOTE: this used to check only protocol == "curve" -- meaning every
        # Balancer pool, and every Aerodrome pool with stable=True, fell
        # through to the plain reserve-ratio branch below, silently pricing
        # them as if they were constant-product/equal-weight pools. Adapters
        # already compute and store the correct spot_rate in metadata on
        # every load/refresh; nothing was reading it for these two cases
        # until this branch was widened to include them.
        if self.protocol == "curve" or self.protocol == "balancer_v2" or \
           (self.protocol == "aerodrome_v2" and self.metadata.get("stable")):
            rate = float(self.metadata.get("spot_rate", 0.0) or 0.0)
            return rate

        if self.version == 2:
            if self.reserve_in == 0:
                return 0.0
            return (self.reserve_out / self.reserve_in) * fee_mult
        else:
            # V3: derive strictly from sqrtPriceX96
            if self.sqrt_price_x96 == 0:
                return 0.0
            try:
                # Math: price = (sqrtPriceX96 / 2^96)^2 = price of token1 per token0.
                price = (self.sqrt_price_x96 / (2 ** 96)) ** 2
                # If swapping token1 -> token0, invert the price mathematically.
                #
                # Uses pool_token0 explicitly (matching token0_price above)
                # rather than comparing token_in > token_out lexicographically.
                # The two happen to always agree today, since Uniswap V3
                # factories assign pool_token0 = min(address) — but that's an
                # implicit dependency on that sorting convention, not
                # something this property should need to know about. Using
                # pool_token0 directly is correct regardless of whether that
                # convention holds, and matches how every other branch on
                # this class already determines direction.
                token0 = (self.pool_token0 or self.token_in).lower()
                if self.token_in.lower() != token0:
                    price = 1.0 / price if price > 0 else 0.0
                return price * fee_mult
            except:
                return 0.0

    @property
    def log_weight(self) -> float:
        """Negative log of exchange rate — Bellman-Ford weight."""
        r = self.exchange_rate
        if r <= 0:
            return float("inf")
        return -math.log(r)
    
    @property
    def liquidity_usd_estimate(self) -> float:
        """Very rough USD liquidity estimate from reserves."""
        if self.reserve_in == 0 and self.reserve_out == 0:
            return 0.0
        return math.sqrt(self.reserve_in * self.reserve_out)
    
    def to_dict(self) -> dict:
        return {
            "pool_address": self.pool_address,
            "dex_name": self.dex_name,
            "token_in": self.token_in,
            "token_out": self.token_out,
            "reserve_in": self.reserve_in,
            "reserve_out": self.reserve_out,
            "fee_bps": self.fee_bps,
            "version": self.version,
            "protocol": self.protocol,
            "pool_token0": self.pool_token0,
            "pool_token1": self.pool_token1,
            "metadata": self.metadata,
            "exchange_rate": self.exchange_rate,
            "tvl_usd": self.tvl_usd,
            "is_stable_pair": self.is_stable_pair,
            "is_pending": self.is_pending,
            "chain_id": self.chain_id,
            "state_block": self.state_block,
            "state_block_hash": self.state_block_hash,
            "state_source": self.state_source,
            "state_status": self.state_status,
        }


@dataclass
class ArbitrageCycle:
    """Represents a profitable arbitrage cycle."""
    tokens: list[str]
    pools: list[str]
    dexes: list[str]
    fees: list[int]
    versions: list[int]
    log_weight_sum: float
    estimated_profit_ratio: float
    hop_count: int
    
    # NEW: Enhanced metadata
    is_cross_dex: bool = False
    is_cross_chain: bool = False
    simulated_via_contract: bool = False
    amount_in_wei: int = 0
    estimated_gas_usd: float = 0.0
    net_profit_usd: float = 0.0

    def set_amount_in_wei(self, target_usd: float = 10.0):
        # Calculate the amount in wei based on the first token in the cycle
        first_token = self.tokens[0]
        first_token_decimals = token_decimals(first_token)
        price = token_price(first_token) or _NATIVE_PRICE_USD
        self.amount_in_wei = int((target_usd / price) * (10 ** first_token_decimals))
        if self.amount_in_wei <= 0:
            self.amount_in_wei = 10 ** (first_token_decimals // 2) # Fallback to a safe fraction

    def rotate_to_best_start(self, graph: "TokenGraph") -> "ArbitrageCycle":
        """
        Rotate the cycle so it starts with the most liquid/standard base token.
        Priority: WETH > Stablecoins > Top TVL.
        """
        best_idx = 0
        best_score = -1.0
        
        # We only consider tokens that are actually in the path (excluding the closing duplicate)
        unique_tokens = self.tokens[:-1]
        
        for i, token in enumerate(unique_tokens):
            addr = token.lower()
            score = 0.0
            
            # Priority 1: WETH
            if addr in _WETH_ADDRS:
                score = 1000.0
            # Priority 2: Stables
            elif addr in STABLE_TOKEN_SETS:
                score = 500.0
            # Priority 3: Derived ETH / TVL weight
            else:
                token_meta = graph.token_metadata.get(addr, {})
                score = token_meta.get("derived_eth", 0.0) * 100.0
            
            if score > best_score:
                best_score = score
                best_idx = i
        
        if best_idx == 0:
            return self
            
        # Perform rotation
        new_tokens = self.tokens[best_idx:-1] + self.tokens[:best_idx]
        new_tokens.append(new_tokens[0]) # Close the cycle
        
        new_pools = self.pools[best_idx:] + self.pools[:best_idx]
        new_dexes = self.dexes[best_idx:] + self.dexes[:best_idx]
        new_fees = self.fees[best_idx:] + self.fees[:best_idx]
        new_versions = self.versions[best_idx:] + self.versions[:best_idx]

        self.tokens = new_tokens
        self.pools = new_pools
        self.dexes = new_dexes
        self.fees = new_fees
        self.versions = new_versions
        
        return ArbitrageCycle(
            tokens=new_tokens,
            pools=new_pools,
            dexes=new_dexes,
            fees=new_fees,
            versions=new_versions,
            log_weight_sum=self.log_weight_sum,
            estimated_profit_ratio=self.estimated_profit_ratio,
            hop_count=self.hop_count,
            is_cross_dex=self.is_cross_dex,
            is_cross_chain=self.is_cross_chain,
            simulated_via_contract=self.simulated_via_contract,
            estimated_gas_usd=self.estimated_gas_usd,
            net_profit_usd=self.net_profit_usd
        )

    @property
    def cycle_type(self) -> str:
        if self.hop_count == 2:
            return "DEX-DEX" if self.is_cross_dex else "SAME-DEX"
        elif self.hop_count == 3:
            return "CROSS-DEX-TRI" if self.is_cross_dex else "TRIANGULAR"
        return f"CROSS-DEX-MULTI({self.hop_count})" if self.is_cross_dex else f"MULTI-HOP({self.hop_count})"
    
    @property
    def path_string(self) -> str:
        return " → ".join([t[:8] for t in self.tokens])

    def __repr__(self) -> str:
        return (
            f"[{self.cycle_type}] {self.path_string} | "
            f"profit≈{self.estimated_profit_ratio*100:.4f}% | "
            f"dexes={self.dexes}"
        )


class TokenGraph:
    """
    Directed multigraph: token → token, multiple edges per pair (different pools).
    Supports Bellman-Ford style cycle detection.
    """
    
    def __init__(self, w3: Web3 = None, cache: PoolCache =None, chain_id: int | None = None):
        self.w3 = w3
        self.chain_id = int(chain_id or (w3.eth.chain_id if w3 is not None else 0))        
        self.cache = cache
        # token_address → list of outgoing edges
        self._edges: dict[str, list[PoolEdge]] = {}
        # pool_address → list of edges (for fast updates)
        self._pool_index: dict[str, list[PoolEdge]] = {}
        # all known tokens
        self.tokens: set[str] = set()
        
        # NEW: Token metadata (decimals, derivedETH, symbol)
        self.token_metadata: dict[str, dict] = {}
        
        # NEW: Additional indexes for faster lookups
        self._token_pairs: dict[str, list[PoolEdge]] = {}  # "token0-token1" → edges
        
        # Stats
        self.total_cycles_found = 0
        self.last_scan_time = 0

        # Long-lived ProtocolRegistry, reused across every best_rate_for_amount()
        # call. Previously best_rate_for_amount() constructed a fresh
        # ProtocolRegistry(self.w3) inline on every single call — since
        # ProtocolRegistry.get() memoizes adapter instances in self._adapters,
        # a registry that's thrown away and rebuilt every call means that
        # cache is always empty, defeating its own purpose. Held here instead
        # so adapter instances are actually reused across calls.
        self._protocol_registry = None

    # ═════════════════════════════════════════════════════════════════════════════
    # GRAPH MUTATION
    # ═════════════════════════════════════════════════════════════════════════════
    
    def add_or_update_pool(self, edge_a: PoolEdge, edge_b: PoolEdge) -> None:
        """Add/update the two canonical directions of a pool."""
        self.upsert_pool_edges((edge_a, edge_b))

    def upsert_pool_edges(self, edges: list[PoolEdge] | tuple[PoolEdge, ...]) -> None:
        """Upsert arbitrary directed edges belonging to one canonical pool.

        This is intentionally separate from ``add_or_update_pool`` because
        protocols such as Curve/Balancer can expose more than two useful
        token directions for one pool contract. Pool identity is the contract,
        while edges are derived views of that pool.
        """
        for edge in edges:
            if self.chain_id and edge.chain_id not in (0, self.chain_id):
                raise ValueError(f"cross-chain pool rejected: edge={edge.chain_id}, graph={self.chain_id}")
            if self.chain_id and edge.chain_id == 0:
                edge = replace(edge, chain_id=self.chain_id)
            self.tokens.add(edge.token_in)
            self.tokens.add(edge.token_out)
            
            # Main adjacency list. Keyed by lowercase token address -- same
            # rationale as _make_pair_key's normalization below: this used to
            # key on edge.token_in as-given, so a pool constructed with
            # checksummed-case tokens (confirmed: NewPoolDetector did this,
            # now fixed at the source too) would be indexed under a
            # different key than the lowercased tokens DFS traversal and
            # every other lookup uses, making it silently invisible to cycle
            # detection entirely -- not just pair lookups.
            edge_in_key = edge.token_in.lower()
            if edge_in_key not in self._edges:
                self._edges[edge_in_key] = []
            
            existing = self._edges[edge_in_key]
            replaced = False
            for i, e in enumerate(existing):
                if e.pool_address == edge.pool_address and e.token_out == edge.token_out:
                    existing[i] = edge
                    replaced = True
                    break
            if not replaced:
                existing.append(edge)
            
            # Pool index
            if edge.pool_address not in self._pool_index:
                self._pool_index[edge.pool_address] = []
            idx = self._pool_index[edge.pool_address]
            for i, e in enumerate(idx):
                if e.token_in == edge.token_in:
                    idx[i] = edge
                    break
            else:
                idx.append(edge)
            
            # Token pair index
            pair_key = self._make_pair_key(edge.token_in, edge.token_out)
            if pair_key not in self._token_pairs:
                self._token_pairs[pair_key] = []
            pair_edges = self._token_pairs[pair_key]
            for i, e in enumerate(pair_edges):
                # Must match BOTH pool_address AND direction (token_in) --
                # matching on pool_address alone meant inserting a pool's
                # second (reverse) direction overwrote its first, since both
                # directions of one pool share the same pool_address. Every
                # pair in this index held at most one direction per pool,
                # silently dropping the other -- confirmed to make
                # CrossProtocolDetector/StableImbalanceDetector's spread
                # comparisons incomplete (comparing whichever direction
                # happened to be inserted last, not necessarily the correct
                # buy/sell pairing) with no error or indication anything was
                # missing.
                if e.pool_address == edge.pool_address and e.token_in.lower() == edge.token_in.lower():
                    pair_edges[i] = edge
                    break
            else:
                pair_edges.append(edge)

    def update_token_metadata(self, token: str, metadata: dict) -> None:
        """Update metadata for a token (e.g. from TheGraph)."""
        addr = token.lower()
        if addr not in self.token_metadata:
            self.token_metadata[addr] = {}
        self._graph_metadata_merge(self.token_metadata[addr], metadata)

    def _graph_metadata_merge(self, dest: dict, src: dict) -> None:
        for k, v in src.items():
            if v is not None:
                # Keep existing if new is zero/empty unless it was empty
                if k in dest and (v == 0 or v == "") and (dest[k] != 0 and dest[k] != ""):
                    continue
                dest[k] = v
    
    def _make_pair_key(self, t1: str, t2: str) -> str:
        """Create a canonical pair key.

        Normalizes case here rather than trusting every caller to have
        already lowercased t1/t2. Confirmed real impact: NewPoolDetector
        (detectors/specialized.py) constructs PoolEdges directly from
        web3.py's ABI-decoded event args, which are checksummed (mixed
        case), while every other discovery path (registry.py, the
        protocols/*.py adapters) consistently lowercases token addresses
        before constructing a PoolEdge. Since this method previously did
        f"{min(t1, t2)}-{max(t1, t2)}" on the raw strings with no
        normalization, a pool discovered via NewPoolDetector and a query
        for the same pair via .lower()-ed addresses (e.g.
        CrossProtocolDetector/StableImbalanceDetector, or any other caller
        of get_edges_for_pair/all_edges_for_pair) would silently produce
        different pair keys and never find each other -- no error, just a
        pool invisible to every pair-based lookup in the system.
        """
        a, b = t1.lower(), t2.lower()
        return f"{min(a, b)}-{max(a, b)}"
    
    def remove_pool(self, pool_address: str) -> None:
        """Remove a pool from the graph."""
        edges = self._pool_index.pop(pool_address, [])
        
        for edge in edges:
            # Remove from adjacency list (keyed by lowercase token, see the
            # matching note in upsert_pool_edges above -- edge.token_in
            # itself may still be mixed-case; normalize here too).
            edge_in_key = edge.token_in.lower()
            bucket = self._edges.get(edge_in_key, [])
            self._edges[edge_in_key] = [
                e for e in bucket if e.pool_address != pool_address
            ]
            
            # Remove from pair index
            pair_key = self._make_pair_key(edge.token_in, edge.token_out)
            if pair_key in self._token_pairs:
                self._token_pairs[pair_key] = [
                    e for e in self._token_pairs[pair_key]
                    if e.pool_address != pool_address
                ]
    
    def get_edges_from(self, token: str) -> list[PoolEdge]:
        """Get all outgoing edges from a token."""
        return self._edges.get(token.lower(), [])
    
    def get_edges_for_pair(self, token_in: str, token_out: str) -> list[PoolEdge]:
        """Get all edges for a token pair."""
        pair_key = self._make_pair_key(token_in, token_out)
        return self._token_pairs.get(pair_key, [])
    
    @property
    def pool_count(self) -> int:
        return len(self._pool_index)
    
    @property
    def token_count(self) -> int:
        return len(self.tokens)
    
    @property
    def edge_count(self) -> int:
        return sum(len(v) for v in self._edges.values())

    def pool_identity(self, pool_address: str) -> tuple[int, str, str] | None:
        """Return canonical (chain_id, protocol, address) identity for a pool."""
        edges = self.get_edges_for_pool(pool_address)
        if not edges:
            return None
        protocol = edges[0].protocol or ("uniswap_v3" if edges[0].version == 3 else "uniswap_v2")
        return (self.chain_id, protocol, edges[0].pool_address.lower())

    def mark_pool_state(self, pool_address: str, *, status: str, source: str = "") -> None:
        """Mark every derived edge for a pool with a common state status."""
        edges = self.get_edges_for_pool(pool_address)
        if not edges:
            return
        self.upsert_pool_edges([replace(e, state_status=status, state_source=source or e.state_source) for e in edges])

    def has_pool(self, pool_address: str) -> bool:
        """Check if pool exists in graph."""
        return pool_address.lower() in self._pool_index or pool_address in self._pool_index

    def get_edges_for_pool(self, pool_address: str) -> list[PoolEdge]:
        """Public accessor for all edges belonging to a pool.

        Replaces direct access to the private ``_pool_index`` attribute
        (e.g. from detectors/cycles.py) so internal refactors don't
        silently break targeted cycle searches.
        """
        if pool_address in self._pool_index:
            return self._pool_index[pool_address]
            
        pool_lower = pool_address.lower()
        for k, v in self._pool_index.items():
            if k.lower() == pool_lower:
                return v
                
        return []

    def get_edge_by_pool_and_tokens(self, pool_address: str, t_in: str, t_out: str) -> Optional[PoolEdge]:
        """Find a specific edge by pool address and the tokens in/out."""
        edges = self.get_edges_for_pool(pool_address)
        t_in_lower = t_in.lower()
        t_out_lower = t_out.lower()
        
        for e in edges:
            if e.token_in.lower() == t_in_lower and e.token_out.lower() == t_out_lower:
                return e
        return None

    # ═════════════════════════════════════════════════════════════════════════════
    # CYCLE DETECTION
    # ═════════════════════════════════════════════════════════════════════════════
    
    def find_cycles(
        self,
        source_tokens: Optional[list[str]] = None,
        max_hops: int = MAX_HOPS,
        min_profit_ratio: float = 0.0001,
        must_include_pool: Optional[str] = None,
        with_refresh: bool = False
    ) -> list[ArbitrageCycle]:
        """
        PRODUCTION SPEC 3.2: BFS-DFS Iterative Deepening (The "Ask Algorithm")
        Scans strictly for target depths sequentially (2-hop -> 3-hop -> 4-hop).
        """
        sources = source_tokens or list(self.tokens)
        cycles: list[ArbitrageCycle] = []
        seen_cycles: set[frozenset] = set()
        
        # PERFORMANCE GUARD: If doing a full graph scan,
        # limit to the most active/important tokens to avoid O(V^k) explosion.
        if source_tokens is None and not must_include_pool and len(sources) > 50:
            sources = sources[:50]
            
        if must_include_pool:
            # If we must include a pool, any cycle containing it can be found by
            # starting the DFS exactly FROM that pool.
            pool_edges = self.get_edges_for_pool(must_include_pool)
            sources = list({e.token_in for e in pool_edges})
        
        target_max = min(max_hops, 4)
        for target_depth in range(2, target_max + 1):
            depth_cycles = []
            for source in sources:
                self._dfs_cycles(
                    source=source,
                    current=source,
                    path_tokens=[source],
                    path_pools=[],
                    path_dexes=[],
                    path_fees=[],
                    path_versions=[],
                    log_weight=0.0,
                    depth=0,
                    max_hops=target_depth,
                    min_profit_ratio=min_profit_ratio,
                    results=depth_cycles,
                    seen=seen_cycles,
                    must_include_pool=must_include_pool,
                    with_refresh=with_refresh,
                    exact_depth=target_depth
                )
            cycles.extend(depth_cycles)
        
        self.total_cycles_found += len(cycles)
        self.last_scan_time = time.time()

        log.debug(
            f"find_cycles IDDFS: sources={len(sources)} found={len(cycles)} "
            f"total={self.total_cycles_found} "
            f"pools={self.pool_count} tokens={self.token_count}"
        )

        # Sort by most profitable
        cycles.sort(key=lambda c: c.log_weight_sum)
        return cycles

    def find_2hop_spreads(self, min_profit_ratio: float = 0.0001) -> list[ArbitrageCycle]:
        """
        Fast-path for direct DEX-DEX arbitrage (2 hops).
        Iterates through token pairs and finds price discrepancies between any two pools.
        """
        results = []
        seen_keys = set()
        
        for pair_key, edges in self._token_pairs.items():
            if len(edges) < 2: continue
            
            # Group edges by direction
            try:
                t1, t2 = pair_key.split('-')
            except: continue
            
            dir1 = [e for e in edges if e.token_in == t1]
            dir2 = [e for e in edges if e.token_in == t2]
            
            if not dir1 or not dir2: continue
            
            # Check all combinations (usually only 2-3 pools per pair)
            for e1 in dir1:
                if e1.tvl_usd < MIN_POOL_LIQUIDITY_USD * 0.1: continue
                for e2 in dir2:
                    if e2.tvl_usd < MIN_POOL_LIQUIDITY_USD * 0.1: continue
                    if e1.pool_address == e2.pool_address: continue # Cannot arb a pool against itself
                    
                    # Log weight sum (negative = profit)
                    w_sum = e1.log_weight + e2.log_weight
                    if w_sum < -1e-9: # Tiny epsilon for float precision
                        profit_ratio = math.exp(-w_sum) - 1.0
                        if profit_ratio >= min_profit_ratio:
                            key = frozenset([(e1.pool_address, t1), (e2.pool_address, t2)])
                            if key not in seen_keys:
                                seen_keys.add(key)
                                results.append(ArbitrageCycle(
                                    tokens=[t1, t2, t1],
                                    pools=[e1.pool_address, e2.pool_address],
                                    dexes=[e1.dex_name, e2.dex_name],
                                    fees=[e1.fee_bps, e2.fee_bps],
                                    versions=[e1.version, e2.version],
                                    log_weight_sum=w_sum,
                                    estimated_profit_ratio=profit_ratio,
                                    hop_count=2,
                                    is_cross_dex=(e1.dex_name != e2.dex_name)
                                ))
        
        results.sort(key=lambda c: c.log_weight_sum)
        return results
    
    def _dfs_cycles(
        self,
        source: str,
        current: str,
        path_tokens: list[str],
        path_pools: list[str],
        path_dexes: list[str],
        path_fees: list[int],
        path_versions: list[int],
        log_weight: float,
        depth: int,
        max_hops: int,
        min_profit_ratio: float,
        results: list[ArbitrageCycle],
        seen: set[frozenset],
        must_include_pool: Optional[str] = None,
        with_refresh: bool = False,
        exact_depth: Optional[int] = None,
    ) -> None:
        # Check if cycle completed
        if depth > 0 and current == source:
            # IDDFS Constraint: Only collect cycles exactly matching the current target depth
            if exact_depth is not None and depth != exact_depth:
                return
                
            if log_weight < -1e-12: # Small epsilon to avoid ghost arbs from float noise
                profit_ratio = math.exp(-log_weight) - 1.0

                # Cap at a sane maximum: profit ratios beyond this are almost
                # always mathematical artifacts of near-zero liquidity pools
                # or decimals mismatches, not real opportunities. This must
                # match the "discard extreme profits" filter below — they
                # were previously two different values (100x vs 10x), which
                # made the cap here dead code since the filter below was
                # always stricter.
                MAX_SANE_PROFIT_RATIO = 10.0
                profit_ratio = min(profit_ratio, MAX_SANE_PROFIT_RATIO)

                if min_profit_ratio <= profit_ratio:
                    # must_include_pool check moved to DFS edge selection (depth == 0 constraint)
                    
                    # Include pool+direction pairs in the key so that
                    # forward (A→B via pool1, B→A via pool2) and reverse
                    # (A→B via pool2, B→A via pool1) are BOTH discovered.
                    # Previously frozenset(pools) collapsed both into one,
                    # causing the simulator to miss the correct arb direction.
                    # Key uses (pool, token_in) to uniquely define a directed jump.
                    # This allows finding forward/reverse arbs with same pools.
                    # tuple(sorted(...)) is used to deduplicate rotations.
                    edges_key = sorted([(p, path_tokens[i]) for i, p in enumerate(path_pools)])
                    key = tuple(edges_key)
                    if key not in seen:
                        seen.add(key)
                        
                        # Discard extreme profits (bad data)
                        if MAX_SANE_PROFIT_RATIO > profit_ratio >= min_profit_ratio:
                            # Check if cross-DEX
                            is_cross_dex = len(set(path_dexes)) > 1
                            # print(f"DEBUG: Found cycle! {path_tokens} | profit={profit_ratio*100:.4f}%")
                            results.append(ArbitrageCycle(
                            tokens=path_tokens[:],
                            pools=path_pools[:],
                            dexes=path_dexes[:],
                            fees=path_fees[:],
                            versions=path_versions[:],
                            log_weight_sum=log_weight,
                            estimated_profit_ratio=profit_ratio,
                            hop_count=depth,
                            is_cross_dex=is_cross_dex,
                        ))
            return
        
        if depth >= max_hops:
            return
        
        # Continue DFS
        edges_to_visit = self.get_edges_from(current)

        # Massive optimization: if must_include_pool is provided, force it to be the FIRST hop
        if depth == 0 and must_include_pool:
            orig_len = len(edges_to_visit)
            must_include_lower = str(must_include_pool).lower()
            edges_to_visit = [e for e in edges_to_visit if e.pool_address.lower() == must_include_lower]
            with suppress(Exception):
                # log.debug(f"DFS depth 0 filter: {orig_len} -> {len(edges_to_visit)} edges (pool={must_include_lower[:8]})")
                pass

        # Refresh edges from graph to get latest weights (in case of updates during DFS)
        pools = set(e.pool_address for e in edges_to_visit if e.pool_address != must_include_pool)
        # We intentionally do not refresh the must_include_pool edge here to preserve the integrity of the DFS path and avoid mid-search mutations that could cause it to be missed or duplicated.
        if with_refresh and pools and self.w3 and self.cache:
            log.debug(f"DFS depth {depth}: Refreshing {len(pools)} pools before visiting edges...")
            multicall3_bulk_refresh(self.w3, self, self.cache, pools, chunk_size=1000)


        for edge in edges_to_visit:
            # LIQUIDITY PRUNING:
            # Skip pools with ultra-low liquidity compared to our minimum threshold.
            # This prunes noise and "paper profits" from near-empty pools.
            if edge.tvl_usd < MIN_POOL_LIQUIDITY_USD * 0.05:
                continue

            next_token = edge.token_out
            w = edge.log_weight
            
            if math.isinf(w):
                continue
            
            # WEIGHT-BASED PRUNING: If cumulative weight is too positive, 
            # this branch cannot possibly yield profit even with best remaining edges
            # A cycle needs total log_weight < 0 (negative) to be profitable
            # At each depth, estimate minimum achievable remaining weight
            cumulative_weight = log_weight + w
            remaining_hops = max_hops - depth - 1
            
            # Optimistic bound: even if all remaining edges had the best
            # conceivable weight, would we still have a chance?
            #
            # IMPORTANT: this bound must be large enough that a single real
            # hop's log_weight can never exceed it on its own -- otherwise
            # this prunes genuinely profitable cycles, not just hopeless
            # ones. log_weight is -log(exchange_rate), and exchange_rate for
            # a single hop is driven entirely by the PER-UNIT VALUE RATIO of
            # the two tokens (e.g. WETH at ~$2000-4000/unit vs USDC at
            # ~$1/unit gives a single-hop log_weight around +/-7.5, with no
            # relation whatsoever to whether the overall cycle is
            # profitable). A previous value of -1.0 per remaining hop (an
            # "optimistic" bound of just e^1 ~= 2.7x per hop) was smaller in
            # magnitude than the log_weight of an ORDINARY, everyday
            # WETH<->stablecoin conversion -- confirmed via a constructed
            # test: a genuinely profitable 3-hop USDC->WETH->DAI->USDC cycle
            # (verified independently profitable) was silently discarded at
            # the very first hop, purely because that hop's log_weight
            # (~+7.5, from converting the "cheap" token to the "expensive"
            # one) exceeded the old bound, with the pruning check having no
            # way to know a compensating hop later in the path would bring
            # the total back down. Whether this fires depends on which
            # token the search happens to be rooted at (going
            # expensive->cheap first gives a negative first hop and was
            # never pruned) -- meaning real opportunities were being missed
            # asymmetrically depending on traversal order, which is exactly
            # the rooting mempool/simulator.py's backrun search deliberately
            # uses (root at the token the victim swap made cheap, which is
            # very often a stablecoin, with WETH still ahead in the path).
            #
            # -25.0 covers per-unit value ratios up to roughly 7*10^10 in a
            # single hop -- comfortably beyond any realistic token pair --
            # while still pruning branches whose cumulative weight is
            # hopeless by a wide margin. The MAX_SANE_PROFIT_RATIO cap at
            # cycle completion (elsewhere in this method) is what actually
            # bounds how extreme a *reported* result can be; this bound only
            # needs to avoid rejecting real candidates before they're
            # scored, not to enforce sanity on the result itself.
            if remaining_hops > 0:
                best_possible_remaining = -25.0 * remaining_hops
                if cumulative_weight + best_possible_remaining > -0.001:  # Can't achieve min profit
                    continue
            
            # Avoid revisiting tokens (except source to close cycle)
            if next_token != source and next_token in path_tokens:
                continue
            
            # Avoid reusing the same pool (can't arb a pool against itself)
            if edge.pool_address in path_pools:
                continue
            
            self._dfs_cycles(
                source=source,
                current=next_token,
                path_tokens=path_tokens + [next_token],
                path_pools=path_pools + [edge.pool_address],
                path_dexes=path_dexes + [edge.dex_name],
                path_fees=path_fees + [edge.fee_bps],
                path_versions=path_versions + [edge.version],
                log_weight=log_weight + w,
                depth=depth + 1,
                max_hops=max_hops,
                min_profit_ratio=min_profit_ratio,
                results=results,
                seen=seen,
                must_include_pool=must_include_pool,
                with_refresh=with_refresh
            )

    # ═════════════════════════════════════════════════════════════════════════════
    # CONVENIENCE METHODS
    # ═════════════════════════════════════════════════════════════════════════════
    
    def best_rate(self, token_in: str, token_out: str) -> Optional[PoolEdge]:
        """Return the pool offering the best rate."""
        edges = self.get_edges_for_pair(token_in, token_out)
        if not edges:
            return None
        return max(edges, key=lambda e: e.exchange_rate)
    
    def _get_protocol_registry(self):
        """Lazily build, then reuse, one ProtocolRegistry for this graph's
        lifetime (see the comment on self._protocol_registry in __init__)."""
        if self._protocol_registry is None and self.w3 is not None:
            from protocols import ProtocolRegistry
            self._protocol_registry = ProtocolRegistry(self.w3)
        return self._protocol_registry

    def best_rate_for_amount(
        self, 
        token_in: str, 
        token_out: str, 
        amount_in: int
    ) -> tuple[Optional[PoolEdge], int]:
        """
        Find best pool considering amount (realized price including slippage).
        Returns (best_edge, amount_out).

        Every protocol is quoted the same way here now: via its
        ProtocolAdapter.quote_exact_in(). Previously v2 and v3 each had their
        own inline reimplementation of AMM math in this method — v2's
        duplicated protocols/uniswap_v2.py's constant-product formula
        (UniswapV2Adapter existed and was registered, but nothing ever
        called it), and v3's was a crude `amount_in * spot_rate` linear
        approximation that ignored tick boundaries entirely (no
        UniswapV3Adapter existed to call instead). Both adapters now exist
        and are used uniformly with every other protocol.
        """
        edges = self.get_edges_for_pair(token_in, token_out)
        if not edges:
            return None, 0
        
        best_edge = None
        best_output = 0
        registry = self._get_protocol_registry()

        for edge in edges:
            curr_output = 0
            if registry is not None and edge.protocol:
                try:
                    from config import get_chain
                    from protocols import PoolRef
                    dex_cfg = next((d for d in get_chain().dexes if d.name == edge.dex_name), None)
                    adapter = registry.get(dex_cfg) if dex_cfg is not None else None
                    if adapter is not None:
                        ref = PoolRef(edge.pool_address, edge.protocol, edge.dex_name, edge.token_in, edge.token_out, edge.fee_bps, edge.metadata)
                        q = adapter.quote_exact_in(ref, token_in, amount_in)
                        curr_output = q.amount_out if q else 0
                except Exception:
                    curr_output = 0
            elif edge.version == 2 and edge.reserve_in > 0:
                # No adapter available (e.g. graph constructed without a w3,
                # as some tests do) — fall back to the same V2 constant-
                # product formula UniswapV2Adapter.quote_exact_in() uses.
                fee_num = 10000 - edge.fee_bps
                numerator = amount_in * fee_num * edge.reserve_out
                denominator = (edge.reserve_in * 10000) + (amount_in * fee_num)
                curr_output = numerator // denominator
            elif edge.sqrt_price_x96 > 0:
                # No adapter available — fall back to the spot-rate
                # approximation (no tick-walk possible without a live w3).
                curr_output = int(amount_in * edge.exchange_rate)
            
            if curr_output > best_output:
                best_output = curr_output
                best_edge = edge
        
        return best_edge, best_output
    
    def all_edges_for_pair(self, token_in: str, token_out: str) -> list[PoolEdge]:
        """Get all edges for a pair across all DEXes."""
        return self.get_edges_for_pair(token_in, token_out)
    
    def get_top_pools_by_tvl(self, n: int = 10) -> list[PoolEdge]:
        """Get top N pools by TVL."""
        all_edges = []
        for edges in self._edges.values():
            all_edges.extend(edges)
        
        # Deduplicate by pool address
        seen = set()
        unique_edges = []
        for e in all_edges:
            if e.pool_address not in seen:
                seen.add(e.pool_address)
                unique_edges.append(e)
        
        return sorted(unique_edges, key=lambda e: e.tvl_usd, reverse=True)[:n]
    
    def summary(self) -> str:
        return (
            f"Graph: {self.token_count} tokens | "
            f"{self.pool_count} pools | "
            f"{self.edge_count} edges | "
            f"cycles_found={self.total_cycles_found}"
        )
    
    def detailed_summary(self) -> dict:
        """Get detailed graph statistics."""
        dex_counts = {}
        version_counts = {2: 0, 3: 0}
        protocol_counts = {}
        
        for edges in self._edges.values():
            for e in edges:
                dex_counts[e.dex_name] = dex_counts.get(e.dex_name, 0) + 1
                version_counts[e.version] += 1
                protocol_counts[e.protocol or ("uniswap_v3" if e.version == 3 else "uniswap_v2")] = protocol_counts.get(e.protocol or ("uniswap_v3" if e.version == 3 else "uniswap_v2"), 0) + 1
        
        return {
            "tokens": self.token_count,
            "pools": self.pool_count,
            "edges": self.edge_count,
            "dex_distribution": dex_counts,
            "version_distribution": version_counts,
            "protocol_distribution": protocol_counts,
            "cycles_found": self.total_cycles_found,
            "last_scan_time": self.last_scan_time,
        }
    
    def to_dict(self) -> dict:
        """Serialize graph for caching."""
        pools = {}
        for addr, edges in self._pool_index.items():
            if edges:
                e = edges[0]
                # PRODUCTION FILTER: Only cache pools with significant historical TVL
                # OR pools that are in the "Waiting Room" (is_pending=True).
                if not e.is_pending and e.tvl_usd < MIN_POOL_LIQUIDITY_USD * 0.1:
                    continue
                    
                pools[addr] = {
                    "token0": min(e.token_in, e.token_out),
                    "token1": max(e.token_in, e.token_out),
                    "dex": e.dex_name,
                    "version": e.version,
                    "fee_bps": e.fee_bps,
                    "r0": e.reserve_in,
                    "r1": e.reserve_out,
                    "sqrt_price_x96": e.sqrt_price_x96,
                    "liquidity": e.liquidity,
                    "tvl_usd": e.tvl_usd,
                    "is_pending": e.is_pending,
                    "last_updated": e.last_updated,
                }
        return {
            "tokens": list(self.tokens),
            "pools": pools,
            "stats": self.detailed_summary(),
        }

