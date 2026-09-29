"""Deterministic opportunity scoring and de-duplication.

Ranking is deliberately independent of execution. It helps choose what to
validate first; it never turns an unsupported candidate into an executable one.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from typing import Iterable

from logger import Opportunity

@dataclass(frozen=True)
class RankConfig:
    unsupported_penalty: float = 0.25
    approximate_penalty: float = 0.65
    high_competition_penalty: float = 0.70
    medium_competition_penalty: float = 0.88
    stale_window_seconds: float = 30.0


def score(opp: Opportunity, now: float | None = None, cfg: RankConfig | None = None) -> float:
    cfg = cfg or RankConfig()
    base = max(0.0, float(opp.net_profit_usd or opp.estimated_profit_usd))
    confidence = max(0.0, min(1.0, float(opp.confidence)))
    if not bool(opp.metadata.get("execution_supported", True)):
        base *= cfg.unsupported_penalty
    if bool(opp.metadata.get("approximate")) or bool(opp.metadata.get("requires_exact_validation")):
        base *= cfg.approximate_penalty
    comp = str(opp.competition_level).lower()
    if comp == "high":
        base *= cfg.high_competition_penalty
    elif comp == "medium":
        base *= cfg.medium_competition_penalty
    if now is not None and opp.timestamp:
        age = max(0.0, now - opp.timestamp)
        if age > cfg.stale_window_seconds:
            base *= max(0.05, cfg.stale_window_seconds / age)
    return base * confidence


def rank(opps: Iterable[Opportunity], now: float | None = None, cfg: RankConfig | None = None) -> list[Opportunity]:
    cfg = cfg or RankConfig()
    dedup = {}
    for opp in opps:
        key = (
            opp.chain_id,
            opp.arb_kind.value,
            opp.opp_type.value,
            tuple(x.lower() for x in opp.tokens[:2]),
            tuple(sorted(x.lower() for x in opp.pools)),
        )
        candidate = (score(opp, now, cfg), opp)
        current = dedup.get(key)
        if current is None or candidate[0] > current[0]:
            dedup[key] = candidate
    return [x[1] for x in sorted(dedup.values(), key=lambda item: item[0], reverse=True)]
