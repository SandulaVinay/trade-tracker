"""Candidate rule sets. Each returns (entry_signal, score) DataFrames aligned to the panel.

Signals are evaluated on the close of day t; the backtest fills at the open of day t+1.
Thresholds are conventional starting points; the backtest decides which survive.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .config import MAX_ABOVE_50DMA, MIN_MEDIAN_TURNOVER_CR, MIN_PRICE, NEAR_52W_HIGH
from .market import Panel


@dataclass
class Strategy:
    key: str
    name: str
    description: str
    rules: list[str]
    plan_mode: str = "plan"          # "plan" = T1/T2 partial booking + trail; "trail" = trailing stop only
    monthly: bool = False            # entries only on the last trading day of month
    rank_exit_n: int = 0             # monthly rank exit: sell if rank > rank_exit_n
    top_n: int = 0                   # monthly: buy only the top N
    use_regime: bool = True
    filters: list[str] = field(default_factory=list)  # "deliv", "sector"


def base_filter(p: Panel) -> pd.DataFrame:
    i, c = p.ind, p.close
    return ((i["med_turnover"] >= MIN_MEDIAN_TURNOVER_CR) & (p.raw_close >= MIN_PRICE)
            & (c > i["sma200"]) & (c > i["sma50"]) & (c >= NEAR_52W_HIGH * i["hi252"])
            & (c <= (1 + MAX_ABOVE_50DMA) * i["sma50"]) & i["atr"].notna() & i["mom_score"].notna())


def month_end_mask(p: Panel) -> pd.Series:
    d = p.dates.to_series()
    return d.dt.to_period("M") != d.shift(-1).dt.to_period("M")


def apply_filters(sig: pd.DataFrame, p: Panel, filters: list[str]) -> pd.DataFrame:
    i = p.ind
    if "deliv" in filters:
        sig = sig & (i["deliv_ratio"] >= 1.2)
    if "sector" in filters:
        sig = sig & (i["sector_rank"] >= 0.66)
    return sig


def signals(s: Strategy, p: Panel) -> tuple[pd.DataFrame, pd.DataFrame]:
    i, c = p.ind, p.close
    base = base_filter(p)
    score = i["mom_score"]
    if s.key.startswith("S1"):
        ranks = score.where(base).rank(axis=1, ascending=False)
        sig = (ranks <= s.top_n) & base
        sig = sig & month_end_mask(p).values[:, None]
    elif s.key.startswith("S2"):
        sig = base & (c >= i["hi252_prev"]) & (i["vol_ratio"] >= 1.5) & (c > p.open)
    elif s.key.startswith("S3"):
        sig = (base & (i["sma50"] > i["sma200"]) & (i["rsi2"] < 10)
               & ((c - i["sma20"]).abs() <= 1.0 * i["atr"]) & (i["ret63"] > 0))
    else:
        raise ValueError(s.key)
    sig = apply_filters(sig.fillna(False), p, s.filters)
    return sig, score


def rank_frame(p: Panel) -> pd.DataFrame:
    return p.ind["mom_score"].where(base_filter(p)).rank(axis=1, ascending=False)


CATALOG: list[Strategy] = [
    Strategy("S1", "Monthly quality-momentum", "Top-10 by volatility-adjusted 6/12-month momentum, rebalanced monthly.",
             ["Liquidity, trend and 52-week-high filters pass", "Rank in top 10 by momentum score on the last trading day of the month",
              "Sell when rank drops below 20 at month end, or on trailing stop"],
             plan_mode="trail", monthly=True, rank_exit_n=20, top_n=10),
    Strategy("S2", "52-week-high breakout", "Close at a new 52-week high on 1.5x volume.",
             ["Liquidity and trend filters pass", "Close >= prior 52-week high", "Volume >= 1.5x 20-day average", "Green candle"]),
    Strategy("S2D", "Breakout + rising delivery", "S2 plus delivery % at least 1.2x its 20-day average.",
             ["All S2 rules", "Delivery % >= 1.2x 20-day average"], filters=["deliv"]),
    Strategy("S2S", "Breakout + strong sector", "S2 plus stock's industry in the top third by 3-month return.",
             ["All S2 rules", "Industry in top third by 3-month return"], filters=["sector"]),
    Strategy("S2DS", "Breakout + delivery + sector", "S2 with both delivery and sector filters.",
             ["All S2 rules", "Delivery % >= 1.2x 20-day average", "Industry in top third"], filters=["deliv", "sector"]),
    Strategy("S2N", "Breakout, no regime gate", "S2 ignoring the market regime (to measure the gate's value).",
             ["All S2 rules", "Trades even when the market regime is OFF"], use_regime=False),
    Strategy("S3", "Pullback in uptrend", "Short-term oversold (RSI2 < 10) near the 20-DMA in a strong uptrend.",
             ["Liquidity and trend filters pass", "50-DMA > 200-DMA", "RSI(2) < 10", "Close within 1 ATR of 20-DMA"]),
    Strategy("S3S", "Pullback + strong sector", "S3 plus industry in the top third.",
             ["All S3 rules", "Industry in top third by 3-month return"], filters=["sector"]),
]

BY_KEY = {s.key: s for s in CATALOG}
