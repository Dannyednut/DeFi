"""Attach conservative execution economics to Opportunity objects."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from config import NATIVE_PRICE_USD
from tvl import token_price, token_decimals
from logger import Opportunity
from optimizer.execution_economics import ExecutionEconomics

@dataclass(frozen=True)
class EconomicsConfig:
    default_gas_units: int = 300_000
    default_gas_price_gwei: float = 0.0
    execution_buffer_bps: int = 300
    min_net_profit_usd: float = 0.0


def enrich(opp: Opportunity, *, cfg: EconomicsConfig | None = None,
           native_price_usd: float = NATIVE_PRICE_USD) -> Opportunity:
    cfg = cfg or EconomicsConfig()
    meta = dict(opp.metadata or {})
    gas_units = int(meta.get("validated_gas_units", meta.get("gas_units", cfg.default_gas_units)))
    gas_price_wei = int(meta.get("gas_price_wei", max(0, int(cfg.default_gas_price_gwei * 1e9))))
    gross_token = int(meta.get("validated_profit_floor_wei", meta.get("quote_profit_wei", opp.simulation_profit_wei or 0)))
    if gross_token <= 0:
        return opp

    profit_token = meta.get("root_token") or (opp.tokens[0] if opp.tokens else "")
    p_decimals = int(token_decimals(profit_token)) if profit_token else 18
    p_price = float(token_price(profit_token) or native_price_usd) if profit_token else native_price_usd

    gas_native_wei = max(0, gas_units) * max(0, gas_price_wei)
    # Convert native gas cost into the profit token's own base units so it
    # can be combined with the other cost fields below, which are already
    # denominated in the profit token (not the chain's native currency).
    gas_token = int((gas_native_wei / 1e18) * native_price_usd / max(p_price, 1e-18) * (10 ** p_decimals))
    loan = int(meta.get("flashloan_fee_token", meta.get("flashloan_fee_wei", 0)))
    protocol = int(meta.get("protocol_cost_token", meta.get("protocol_cost_wei", 0)))
    tip = int(meta.get("builder_tip_token", meta.get("builder_tip_wei", 0)))
    buffer = int(meta.get("execution_buffer_token", gross_token * cfg.execution_buffer_bps // 10_000))

    # ExecutionEconomics is the single canonical "net profit" calculation
    # used everywhere else in the codebase that makes this decision
    # (execution_router.py's economics gate, the batch executor). This used
    # to be hand-rolled subtraction here instead — this module already
    # imported EconomicsInput/ExecutionEconomics but never actually used
    # either, leaving a dangling import and a second, independent copy of
    # the same "gross - gas - loan - protocol - tip - buffer" arithmetic.
    #
    # ExecutionEconomics' fields are generically "native token units" per its
    # own docstring; here that unit is the profit token's base units (gas_token
    # above is gas cost already converted from native wei), not the chain's
    # native currency — constructed directly rather than via
    # EconomicsInput.from_input(), since that helper's gas_cost_wei property
    # assumes gas_units*gas_price_wei are in the SAME unit as everything else,
    # which isn't true once gas has been converted like this.
    economics = ExecutionEconomics(
        gross_profit_wei=max(0, gross_token),
        gas_cost_wei=max(0, gas_token),
        flashloan_fee_wei=max(0, loan),
        protocol_cost_wei=max(0, protocol),
        builder_tip_wei=max(0, tip),
        execution_buffer_wei=max(0, buffer),
    )
    net_token = economics.deterministic_net_wei
    token_to_usd = p_price / (10 ** p_decimals)
    opp.gas_cost_usd = gas_token * token_to_usd
    opp.net_profit_usd = net_token * token_to_usd
    opp.estimated_profit_usd = max(float(opp.estimated_profit_usd), gross_token * token_to_usd)

    meta["execution_economics"] = {
        "gross_profit_token": gross_token,
        "gas_cost_token": gas_token,
        "flashloan_fee_token": loan,
        "protocol_cost_token": protocol,
        "builder_tip_token": tip,
        "execution_buffer_token": buffer,
        "deterministic_net_token": net_token,
        "gas_units": gas_units,
        "gas_price_wei": gas_price_wei,
        "gas_cost_native_wei": gas_native_wei,
        "profit_token_decimals": p_decimals,
        "profit_token_price_usd": p_price,
        "net_profit_usd": opp.net_profit_usd,
    }
    meta["economics_profitable"] = opp.net_profit_usd >= float(cfg.min_net_profit_usd)
    opp.metadata = meta
    return opp


def enrich_many(opps: Iterable[Opportunity], *, cfg: EconomicsConfig | None = None,
                native_price_usd: float = NATIVE_PRICE_USD) -> list[Opportunity]:
    return [enrich(o, cfg=cfg, native_price_usd=native_price_usd) for o in opps]
