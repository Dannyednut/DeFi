"""Protocol-specific opportunity detectors.

Each detector owns exactly one arbitrage thesis.  Shared opportunity shaping,
validation, economics and ranking live outside the detectors.
"""
from __future__ import annotations
import time
from typing import Iterable

from detectors.base import BaseDetector
from graph import TokenGraph
from logger import Opportunity, OppType
from config import get_chain, NATIVE_PRICE_USD, MIN_LIQUIDITY_USD
from routing import ArbKind, execution_capability
from log import get_logger

log = get_logger("protocol_opportunities")


def _is_stable(addr: str) -> bool:
    """True if `addr` is a genuine stablecoin on the active chain.

    NOTE: this intentionally checks the chain's real `stablecoins` list
    (config.py), not tvl.STABLE_TOKEN_SETS — that set also contains
    WETH/WBTC/wrapped-native "anchor asset" addresses for TVL scoring
    purposes and is not a stablecoin set, despite the name.
    """
    from config import get_chain
    a = (addr or "").lower()
    return a in {s.lower() for s in get_chain().stablecoins}


class CrossProtocolDetector(BaseDetector):
    """Detect executable spot-price fragmentation across distinct pools."""
    name = "cross_proto"
    requires_touched_pools = True
    arb_kind = ArbKind.CROSS_DEX_SPREAD
    MIN_SPREAD_PCT = 0.15

    def on_block(self, block_number: int) -> list[Opportunity]:
        out: list[Opportunity] = []
        chain = get_chain(self.chain_id)
        anchors = [chain.wrapped_native] + list(chain.stablecoins)
        seen: set[tuple[str, str]] = set()
        for i, a in enumerate(anchors):
            for b in anchors[i + 1:]:
                token_in, token_out = a.lower(), b.lower()
                key = tuple(sorted((token_in, token_out)))
                if key in seen:
                    continue
                seen.add(key)
                edges = [e for e in self.graph.all_edges_for_pair(token_in, token_out)
                         if e.chain_id == self.chain_id and e.tvl_usd >= MIN_LIQUIDITY_USD
                         and e.token_in.lower() == token_in]
                # all_edges_for_pair() returns edges for the PAIR regardless
                # of direction -- a token_in->token_out rate and its
                # token_out->token_in reciprocal are NOT comparable
                # quantities (e.g. ~2000 USDC-per-WETH vs ~0.0005 WETH-per-
                # USDC), so without the token_in filter above, sorting by
                # raw exchange_rate below could pick one edge's forward rate
                # as "buy" and a DIFFERENT edge's reverse rate as "sell",
                # producing a nonsensical spread (confirmed: a constructed
                # test case produced a "420249900%" spread this way). This
                # was previously masked by a separate TokenGraph bug that
                # silently dropped one direction per pool from the pair
                # index entirely, so all_edges_for_pair() never actually
                # returned mixed directions in practice -- fixing that
                # storage bug exposed this one.
                if len(edges) < 2:
                    continue
                edges.sort(key=lambda e: e.exchange_rate)
                buy, sell = edges[0], edges[-1]
                if buy.exchange_rate <= 0 or sell.exchange_rate <= 0:
                    continue
                spread = (sell.exchange_rate / buy.exchange_rate - 1.0) * 100.0
                if spread < self.MIN_SPREAD_PCT:
                    continue
                protocols = [buy.protocol, sell.protocol]
                supported, reason = execution_capability(protocols)
                out.append(self._opportunity(
                    opp_type=OppType.CROSS_PROTOCOL,
                    block_number=block_number,
                    tokens=[token_in, token_out, token_in],
                    pools=[buy.pool_address, sell.pool_address],
                    dexes=[buy.dex_name, sell.dex_name],
                    estimated_profit_ratio=spread / 100.0,
                    estimated_profit_usd=0.0,
                    confidence=0.65,
                    metadata={
                        "arb_kind": self.arb_kind.value,
                        "buy_protocol": buy.protocol,
                        "sell_protocol": sell.protocol,
                        "buy_rate": buy.exchange_rate,
                        "sell_rate": sell.exchange_rate,
                        "spread_pct": spread,
                        "execution_supported": supported,
                        "execution_reason": reason,
                        "requires_exact_validation": supported,
                    },
                ))
        return out


class StableImbalanceDetector(BaseDetector):
    """Detect Curve-vs-AMM stablecoin imbalance; no generic spread logic."""
    name = "stable_imbalance"
    requires_touched_pools = True
    arb_kind = ArbKind.STABLE_IMBALANCE
    MIN_SPREAD_PCT = 0.10

    def on_block(self, block_number: int) -> list[Opportunity]:
        out: list[Opportunity] = []
        chain = get_chain(self.chain_id)
        stables = {x.lower() for x in chain.stablecoins}
        anchors = [chain.wrapped_native] + list(chain.stablecoins)
        seen: set[tuple[str, str]] = set()
        for i, a in enumerate(anchors):
            for b in anchors[i + 1:]:
                token_in, token_out = a.lower(), b.lower()
                if token_in not in stables and token_out not in stables:
                    continue
                key = tuple(sorted((token_in, token_out)))
                if key in seen:
                    continue
                seen.add(key)
                edges = [e for e in self.graph.all_edges_for_pair(token_in, token_out)
                         if e.chain_id == self.chain_id and e.token_in.lower() == token_in]
                # See the matching comment in CrossProtocolDetector above --
                # must restrict to one direction before comparing rates.
                curves = [e for e in edges if e.protocol == "curve"]
                amms = [e for e in edges if e.protocol != "curve"]
                if not curves or not amms:
                    continue
                curve = max(curves, key=lambda e: e.exchange_rate)
                amm = max(amms, key=lambda e: e.exchange_rate)
                if not curve.exchange_rate or not amm.exchange_rate:
                    continue
                spread = abs(curve.exchange_rate - amm.exchange_rate) / min(curve.exchange_rate, amm.exchange_rate) * 100.0
                if spread < self.MIN_SPREAD_PCT:
                    continue
                # Stablecoin imbalance has its own detector. Keep this detector
                # focused on generic cross-protocol price fragmentation.
                if _is_stable(token_in) and _is_stable(token_out):
                    continue
                supported, reason = execution_capability([curve.protocol, amm.protocol])
                out.append(self._opportunity(
                    opp_type=OppType.CROSS_PROTOCOL,
                    block_number=block_number,
                    tokens=[token_in, token_out, token_in],
                    pools=[curve.pool_address, amm.pool_address],
                    dexes=[curve.dex_name, amm.dex_name],
                    estimated_profit_ratio=spread / 100.0,
                    estimated_profit_usd=0.0,
                    confidence=0.70,
                    metadata={
                        "arb_kind": self.arb_kind.value,
                        "curve_pool": curve.pool_address,
                        "comparison_pool": amm.pool_address,
                        "spread_pct": spread,
                        "execution_supported": supported,
                        "execution_reason": reason,
                        "requires_exact_validation": supported,
                    },
                ))
        return out


class ProtocolOpportunityDetector:
    """Compatibility facade; runtime orchestration should use distinct detectors."""
    name = "protocol_opportunities"

    def __init__(self, w3, graph: TokenGraph, chain_id: int):
        self.cross = CrossProtocolDetector(w3, graph, None, None, chain_id)
        self.stable = StableImbalanceDetector(w3, graph, None, None, chain_id)

    def on_block(self, block_number: int) -> list[Opportunity]:
        return self.cross.on_block(block_number) + self.stable.on_block(block_number)
