"""Delivery trade charges and tax (FY 2026-27 rates in config)."""
from __future__ import annotations

from .config import (BROKERAGE_PER_ORDER, COST_DP_PER_SELL, COST_EXCH, COST_GST, COST_SEBI,
                     COST_STAMP_BUY, COST_STT, LTCG_RATE, STCG_RATE)


def buy_charges(value: float) -> float:
    exch, sebi = value * COST_EXCH, value * COST_SEBI
    return value * COST_STT + value * COST_STAMP_BUY + exch + sebi + (BROKERAGE_PER_ORDER + exch + sebi) * COST_GST + BROKERAGE_PER_ORDER


def sell_charges(value: float) -> float:
    exch, sebi = value * COST_EXCH, value * COST_SEBI
    return value * COST_STT + exch + sebi + (BROKERAGE_PER_ORDER + exch + sebi) * COST_GST + BROKERAGE_PER_ORDER + COST_DP_PER_SELL


def round_trip(buy_value: float, sell_value: float) -> float:
    return buy_charges(buy_value) + sell_charges(sell_value)


def tax_on_gain(gain: float, holding_days: int, ltcg_exempt_left: float = 0.0) -> float:
    if gain <= 0:
        return 0.0
    if holding_days > 365:
        return max(0.0, gain - ltcg_exempt_left) * LTCG_RATE
    return gain * STCG_RATE
