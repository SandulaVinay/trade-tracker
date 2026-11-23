"""Turn a signal into a complete, rule-based trade plan."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

from . import costs
from .config import (AVERAGE_MAX_TRANCHES, BUY_ZONE_ATR, CHASE_ATR, MAX_POSITION_PCT, RISK_PER_TRADE,
                     REVIEW_DAYS, STOP_ATR, T1_BOOK_FRACTION, TARGET1_R, TARGET2_R, TIME_EXIT_DAYS,
                     TRAIL_ATR)


def r2(x):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else round(float(x), 2)


def tick(x: float) -> float:
    """Round to NSE tick size (0.05 for most stocks; 0.01 below Rs 250 since 2025 - use 0.05 conservatively)."""
    return round(round(x / 0.05) * 0.05, 2)


def last_tuesday(y: int, m: int) -> date:
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def next_monthly_expiry(today: date, holidays: set[date]) -> date:
    y, m = today.year, today.month
    for _ in range(3):
        d = last_tuesday(y, m)
        while d in holidays or d.weekday() >= 5:
            d -= timedelta(days=1)
        if d >= today:
            return d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return d


def build(symbol: str, name: str, row: dict, stats: dict | None, capital: float, size_mult: float,
          strategy_key: str, strategy_name: str) -> dict:
    """row: close, atr, sma20, sma50, sma200, lo20, hi252, rsi14, vol_ratio, deliv_per, deliv_ratio..."""
    c, a = row["close"], row["atr"]
    stop_atr = tick(c - STOP_ATR * a)
    swing = row.get("lo20")
    # Same stop rule as the backtest (so the tested statistics apply to this plan).
    stop = stop_atr
    stop_reason = f"Technical: {STOP_ATR}x ATR(14) below the signal close (ATR = Rs {a:.2f}); same rule as the backtest"
    if swing and np.isfinite(swing):
        stop_reason += f". For context, the 20-day low is Rs {swing:.2f}"
        stop_reason += " (inside the stop)." if swing > stop else " (below the stop)."
    risk = c - stop
    t1, t2 = tick(c + TARGET1_R * risk), tick(c + TARGET2_R * risk)
    zone_lo, zone_hi = tick(c - BUY_ZONE_ATR / 2 * a), tick(c + BUY_ZONE_ATR / 2 * a)
    no_chase = tick(c + CHASE_ATR * a)

    risk_rs = capital * RISK_PER_TRADE * size_mult
    qty = math.floor(risk_rs / (zone_hi - stop)) if zone_hi > stop else 0
    qty = min(qty, math.floor(capital * MAX_POSITION_PCT * size_mult / zone_hi))
    value = qty * zone_hi
    rt = costs.round_trip(value, qty * t1) if qty else 0

    st = stats or {}
    def days(k):
        v = st.get(k)
        return int(round(v)) if v else None
    plan = {
        "symbol": symbol, "name": name, "strategy": strategy_key, "strategy_name": strategy_name,
        "signal_close": r2(c), "atr": r2(a),
        "buy_zone": [zone_lo, zone_hi], "no_chase_above": no_chase,
        "stop": stop, "stop_reason": stop_reason,
        "target1": t1, "target2": t2,
        "t1_days_median": days("t1_days_median"), "t1_days_p75": days("t1_days_p75"),
        "t2_days_median": days("t2_days_median"), "t2_days_p75": days("t2_days_p75"),
        "t1_hit_rate": st.get("t1_hit_rate"), "t2_hit_rate": st.get("t2_hit_rate"),
        "risk_per_share": r2(risk), "rr_t1": round(TARGET1_R, 2), "rr_t2": round(TARGET2_R, 2),
        "expected_hold_days": round(st["avg_hold_days"]) if st.get("avg_hold_days") else None,
        "position": {"capital": capital, "size_mult": size_mult, "qty": qty, "value": r2(value),
                     "pct_capital": r2(100 * value / capital) if capital else None,
                     "max_loss": r2(qty * (zone_hi - stop)), "max_loss_pct": r2(100 * qty * (zone_hi - stop) / capital) if capital else None,
                     "round_trip_charges_est": r2(rt)},
        "if_up": [
            f"Sell {T1_BOOK_FRACTION:.0%} at Target 1 Rs {t1} and move the stop to your entry price (breakeven).",
            f"Sell another {T1_BOOK_FRACTION:.0%} at Target 2 Rs {t2}.",
            f"Trail the rest: exit on a close below (highest close since entry - {TRAIL_ATR}x ATR).",
            "Exit fully on: trailing-stop close, a verified red-flag NSE filing, or close below the 50-DMA.",
        ],
        "if_down": [
            "First classify the fall (the tracker does this daily): market-wide, sector-wide or stock-specific.",
            f"Stock-specific (results miss, governance, rating, pledge): EXIT. Never average.",
            f"Market-wide with thesis intact: at most {AVERAGE_MAX_TRANCHES} add, only after a higher low AND a close back above the 20-DMA (now Rs {r2(row.get('sma20'))}), "
            f"add size <= original qty, total position <= {MAX_POSITION_PCT:.0%} of capital, and the stop stays at Rs {stop}.",
            f"Stop-loss Rs {stop} is never moved down. A close at or below it means exit.",
            f"Time exit: review on day {REVIEW_DAYS}; exit on day {TIME_EXIT_DAYS} if Target 1 has not been reached.",
        ],
        "averaging": {"allowed_only_if": "market-wide fall, thesis intact, higher low + close above 20-DMA",
                      "reference_20dma": r2(row.get("sma20")), "max_tranches": AVERAGE_MAX_TRANCHES,
                      "max_add_qty": qty, "tested": False,
                      "note": "Averaging rule comes from the research plan; it is NOT part of the backtest."},
        "indicators": {k: r2(row.get(k)) for k in ("sma20", "sma50", "sma200", "hi252", "lo20", "rsi14",
                                                    "vol_ratio", "deliv_per", "deliv_ratio", "mom_score",
                                                    "ret63", "sector_rank", "med_turnover")},
        "time_exit_day": TIME_EXIT_DAYS, "review_day": REVIEW_DAYS,
    }
    return plan
