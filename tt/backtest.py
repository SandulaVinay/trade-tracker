"""Portfolio backtest using the exact same trade-plan rules the daily signal publishes.

Signal at close of day t -> buy at open of day t+1 (skipped if it opens above the no-chase price).
Costs: statutory charges + slippage per side. Tax: 20% STCG on each financial year's net gain,
with losses carried forward; deducted at FY end.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import costs, store
from .config import (CHASE_ATR, MAX_OPEN_POSITIONS, MAX_POSITION_PCT, MAX_SECTOR_POSITIONS,
                     REGIME_PARTIAL_SIZE, RISK_PER_TRADE, SLIPPAGE, STCG_RATE, STOP_ATR, TARGET1_R,
                     TARGET2_R, TIME_EXIT_DAYS, TRAIL_ATR, T1_BOOK_FRACTION)
from .market import Panel, regime_series
from .strategies import Strategy, rank_frame, signals

MAX_HOLD_DAYS = 120
START_CAPITAL = 1_000_000.0


@dataclass
class Pos:
    sym: str
    entry_i: int
    entry: float
    qty0: int
    qty: int
    stop: float
    t1: float
    t2: float
    risk: float
    cost: float
    realized: float = 0.0
    charges: float = 0.0
    t1_i: int = -1
    t2_i: int = -1
    highest: float = 0.0


def run(s: Strategy, p: Panel, start: str | None = None, end: str | None = None,
        stop_atr: float = STOP_ATR, trail_atr: float = TRAIL_ATR, cash_yield: float = 0.0) -> dict:
    sig, score = signals(s, p)
    reg = regime_series(p)
    ranks = rank_frame(p) if s.rank_exit_n else None
    O, H, L, C = (x.values for x in (p.open, p.high, p.low, p.close))
    ATR = p.ind["atr"].values
    syms = list(p.close.columns)
    col = {x: k for k, x in enumerate(syms)}
    dates = p.dates
    sig_v, score_v = sig.values, score.values
    is_me = (dates.to_series().dt.to_period("M") != dates.to_series().shift(-1).dt.to_period("M")).values
    i0 = 0 if start is None else int(dates.searchsorted(pd.Timestamp(start)))
    i1 = len(dates) if end is None else int(dates.searchsorted(pd.Timestamp(end), side="right"))

    cash = START_CAPITAL
    pos: dict[str, Pos] = {}
    trades, equity = [], []
    fy_gain: dict[int, float] = {}
    carry_loss = 0.0
    tax_paid = 0.0
    pending: list[tuple[str, float, float]] = []  # (sym, signal close, atr) from yesterday

    def fy(i):
        d = dates[i]
        return d.year if d.month >= 4 else d.year - 1

    def sell(ps: Pos, i: int, px: float, qty: int, reason: str | None):
        nonlocal cash
        fill = px * (1 - SLIPPAGE)
        val = fill * qty
        ch = costs.sell_charges(val)
        cash += val - ch
        ps.realized += val
        ps.charges += ch
        ps.qty -= qty
        if ps.qty == 0:
            pnl = ps.realized - ps.cost - ps.charges
            fy_gain[fy(i)] = fy_gain.get(fy(i), 0.0) + pnl
            trades.append({
                "symbol": ps.sym, "entry_date": str(dates[ps.entry_i].date()), "exit_date": str(dates[i].date()),
                "entry": round(ps.entry, 2), "avg_exit": round(ps.realized / ps.qty0, 2),
                "pnl": round(pnl, 2), "ret": pnl / ps.cost, "r_mult": pnl / (ps.risk * ps.qty0) if ps.risk else 0,
                "hold_days": i - ps.entry_i, "t1_days": (ps.t1_i - ps.entry_i) if ps.t1_i >= 0 else None,
                "t2_days": (ps.t2_i - ps.entry_i) if ps.t2_i >= 0 else None, "reason": reason,
            })
            del pos[ps.sym]

    for i in range(max(i0, 1), i1):
        # ---------------- entries from yesterday's signals at today's open
        state_y = reg["state"].iat[i - 1]
        size_mult = 1.0
        if s.use_regime:
            size_mult = {"ON": 1.0, "PARTIAL": REGIME_PARTIAL_SIZE}.get(state_y, 0.0)
        eq_y = equity[-1][1] if equity else cash
        if size_mult > 0:
            sector_count: dict[str, int] = {}
            for ps in pos.values():
                sec = p.industry[ps.sym]
                sector_count[sec] = sector_count.get(sec, 0) + 1
            for sym, ref, a in pending:
                if len(pos) >= MAX_OPEN_POSITIONS or sym in pos:
                    continue
                sec = p.industry[sym]
                if sector_count.get(sec, 0) >= MAX_SECTOR_POSITIONS:
                    continue
                j = col[sym]
                op = O[i, j]
                if not np.isfinite(op) or op > ref + CHASE_ATR * a:
                    continue
                stop = ref - stop_atr * a
                if op <= stop:
                    continue
                fill = op * (1 + SLIPPAGE)
                risk = fill - stop
                qty = math.floor(eq_y * RISK_PER_TRADE * size_mult / risk)
                qty = min(qty, math.floor(eq_y * MAX_POSITION_PCT * size_mult / fill))
                if qty <= 0:
                    continue
                val = fill * qty
                ch = costs.buy_charges(val)
                if val + ch > cash:
                    qty = math.floor((cash * 0.98) / fill)
                    if qty <= 0:
                        continue
                    val = fill * qty
                    ch = costs.buy_charges(val)
                cash -= val + ch
                r_ref = ref - stop
                pos[sym] = Pos(sym, i, fill, qty, qty, stop, ref + TARGET1_R * r_ref, ref + TARGET2_R * r_ref,
                               risk, val, charges=ch, highest=C[i, j] if np.isfinite(C[i, j]) else fill)
                sector_count[sec] = sector_count.get(sec, 0) + 1
        pending = []

        # ---------------- manage open positions with today's bar
        state_t = reg["state"].iat[i]
        for ps in list(pos.values()):
            j = col[ps.sym]
            o, h, l, c, a = O[i, j], H[i, j], L[i, j], C[i, j], ATR[i, j]
            if not np.isfinite(c):
                continue
            if o <= ps.stop:
                sell(ps, i, o, ps.qty, "stop (gap)")
                continue
            if l <= ps.stop:
                sell(ps, i, ps.stop, ps.qty, "stop" if ps.t1_i < 0 else "trailing stop")
                continue
            if s.plan_mode == "plan":
                if ps.t1_i < 0 and h >= ps.t1:
                    q = max(1, int(round(ps.qty0 * T1_BOOK_FRACTION)))
                    ps.t1_i = i
                    sell(ps, i, max(o, ps.t1), min(q, ps.qty), "target 1")
                    if ps.sym not in pos:
                        continue
                    ps.stop = max(ps.stop, ps.entry)
                if ps.t1_i >= 0 and ps.t2_i < 0 and h >= ps.t2:
                    q = max(1, int(round(ps.qty0 * T1_BOOK_FRACTION)))
                    ps.t2_i = i
                    sell(ps, i, max(o, ps.t2), min(q, ps.qty), "target 2")
                    if ps.sym not in pos:
                        continue
            else:
                if ps.t1_i < 0 and h >= ps.t1:
                    ps.t1_i = i
                if ps.t2_i < 0 and h >= ps.t2:
                    ps.t2_i = i
            ps.highest = max(ps.highest, c)
            if np.isfinite(a) and (s.plan_mode == "trail" or ps.t1_i >= 0):
                ps.stop = max(ps.stop, ps.highest - trail_atr * a)
            if s.use_regime and state_t == "OFF" and np.isfinite(a):
                ps.stop = max(ps.stop, c - 2.0 * a)
            held = i - ps.entry_i
            if s.plan_mode == "plan" and held >= TIME_EXIT_DAYS and ps.t1_i < 0:
                sell(ps, i, c, ps.qty, "time exit")
            elif held >= MAX_HOLD_DAYS and not s.monthly:
                sell(ps, i, c, ps.qty, "max hold")
            elif ranks is not None and is_me[i]:
                rk = ranks.iat[i, j]
                if not np.isfinite(rk) or rk > s.rank_exit_n:
                    sell(ps, i, c, ps.qty, "rank exit")

        # ---------------- new signals at today's close
        row = sig_v[i]
        if row.any():
            cand = [(score_v[i, j] if np.isfinite(score_v[i, j]) else -1e9, syms[j], C[i, j], ATR[i, j])
                    for j in np.flatnonzero(row) if np.isfinite(ATR[i, j])]
            cand.sort(reverse=True)
            pending = [(sym, ref, a) for _, sym, ref, a in cand]

        # ---------------- FY-end tax
        if i + 1 < len(dates) and fy(i + 1) != fy(i):
            g = fy_gain.get(fy(i), 0.0) - carry_loss
            if g > 0:
                t = g * STCG_RATE
                cash -= t
                tax_paid += t
                carry_loss = 0.0
            else:
                carry_loss = -g

        mv = sum(ps.qty * (C[i, col[ps.sym]] if np.isfinite(C[i, col[ps.sym]]) else ps.entry) for ps in pos.values())
        equity.append((dates[i], cash + mv, len(pos), cash))
        if cash_yield and cash > 0:
            cash *= (1 + cash_yield) ** (1 / 252)

    eq = pd.DataFrame(equity, columns=["date", "equity", "positions", "cash"]).set_index("date")
    return {"strategy": s.key, "trades": trades, "equity": eq, "tax_paid": tax_paid,
            "regime": reg}


# ---------------------------------------------------------------- metrics
def _cagr(series: pd.Series) -> float | None:
    if len(series) < 2 or series.iloc[0] <= 0:
        return None
    yrs = (series.index[-1] - series.index[0]).days / 365.25
    return (series.iloc[-1] / series.iloc[0]) ** (1 / yrs) - 1 if yrs > 0.25 else None


def metrics(res: dict, p: Panel, start: str | None = None, end: str | None = None) -> dict:
    eq = res["equity"]["equity"]
    tr = pd.DataFrame(res["trades"])
    if start:
        eq = eq[eq.index >= pd.Timestamp(start)]
        tr = tr[tr["entry_date"] >= start] if not tr.empty else tr
    if end:
        eq = eq[eq.index <= pd.Timestamp(end)]
        tr = tr[tr["entry_date"] <= end] if not tr.empty else tr
    out: dict = {"period": f"{eq.index[0].date() if len(eq) else '-'} to {eq.index[-1].date() if len(eq) else '-'}"}
    if tr.empty or len(eq) < 2:
        out.update({"trades": 0})
        return out
    wins, losses = tr[tr["pnl"] > 0], tr[tr["pnl"] <= 0]
    streak = best = 0
    for x in tr.sort_values("exit_date")["pnl"]:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    dd = (eq / eq.cummax() - 1).min()
    bench = p.index.get("Nifty 500")
    bcagr = None
    if bench is not None:
        b = bench.reindex(eq.index).ffill().dropna()
        bcagr = _cagr(b)
        bdd = (b / b.cummax() - 1).min()
    else:
        bdd = None
    t1 = tr["t1_days"].dropna()
    t2 = tr["t2_days"].dropna()
    out.update({
        "trades": int(len(tr)),
        "win_rate": float(len(wins) / len(tr)),
        "avg_win": float(wins["ret"].mean()) if len(wins) else 0.0,
        "avg_loss": float(losses["ret"].mean()) if len(losses) else 0.0,
        "payoff": float(wins["ret"].mean() / -losses["ret"].mean()) if len(wins) and len(losses) and losses["ret"].mean() < 0 else None,
        "expectancy": float(tr["ret"].mean()),
        "avg_r": float(tr["r_mult"].mean()),
        "profit_factor": float(wins["pnl"].sum() / -losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() < 0 else None,
        "avg_hold_days": float(tr["hold_days"].mean()),
        "max_losing_streak": int(best),
        "max_drawdown": float(dd),
        "cagr_after_cost_tax": _cagr(eq),
        "bench_cagr": bcagr,
        "bench_max_drawdown": float(bdd) if bdd is not None else None,
        "t1_hit_rate": float(len(t1) / len(tr)),
        "t1_days_median": float(t1.median()) if len(t1) else None,
        "t1_days_p75": float(t1.quantile(0.75)) if len(t1) else None,
        "t2_hit_rate": float(len(t2) / len(tr)),
        "t2_days_median": float(t2.median()) if len(t2) else None,
        "t2_days_p75": float(t2.quantile(0.75)) if len(t2) else None,
        "exit_reasons": tr["reason"].value_counts().to_dict(),
        "avg_exposure_positions": float(res["equity"]["positions"].mean()),
        "avg_capital_deployed": float(((res["equity"]["equity"] - res["equity"]["cash"]) / res["equity"]["equity"]).loc[eq.index].mean()),
    })
    yearly = eq.resample("YE").last()
    first = eq.iloc[0]
    yr = yearly.pct_change()
    yr.iloc[0] = yearly.iloc[0] / first - 1
    out["yearly"] = {str(k.year): float(v) for k, v in yr.items()}
    return out


# Signals need ~252 sessions of history, so trading can only start in Jan 2021: measure from there.
FIRST_TRADABLE = "2021-01-01"
PERIODS = {"full": (FIRST_TRADABLE, None), "early": (FIRST_TRADABLE, "2023-12-31"), "recent": ("2024-01-01", None)}
CASH_YIELD_NET = 0.05  # idle cash in an arbitrage/liquid fund, ~6.5% gross less tax


def run_catalog(p: Panel, keys: list[str] | None = None, save: bool = True) -> list[dict]:
    from .strategies import CATALOG
    out = []
    for s in CATALOG:
        if keys and s.key not in keys:
            continue
        res = run(s, p)
        m = {k: metrics(res, p, *v) for k, v in PERIODS.items()}
        # parameter sensitivity: stop multiple +-20%
        sens = {}
        for f in (0.8, 1.2):
            r2 = run(s, p, stop_atr=STOP_ATR * f, trail_atr=TRAIL_ATR * f)
            m2 = metrics(r2, p, FIRST_TRADABLE)
            sens[f"atr_x{f}"] = {k: m2.get(k) for k in ("cagr_after_cost_tax", "win_rate", "max_drawdown", "expectancy")}
        m["sensitivity"] = sens
        m["tax_paid"] = res["tax_paid"]
        rc = run(s, p, cash_yield=CASH_YIELD_NET)
        m["with_cash_yield"] = {k: v for k, v in metrics(rc, p, FIRST_TRADABLE).items() if k in ("cagr_after_cost_tax", "max_drawdown", "bench_cagr")}
        row = {"strategy": s.key, "name": s.name, "metrics": m, "trades": res["trades"],
               "equity": [(str(d.date()), round(v, 2)) for d, v in res["equity"]["equity"].items()]}
        out.append(row)
        if save:
            with store.tx() as c:
                c.execute("INSERT INTO backtests (run_at,strategy,params,metrics,trades) VALUES (?,?,?,?,?)",
                          (store.now_ist().isoformat(), s.key,
                           store.dumps({"stop_atr": STOP_ATR, "trail_atr": TRAIL_ATR, "plan_mode": s.plan_mode,
                                       "filters": s.filters, "use_regime": s.use_regime,
                                       "equity": row["equity"]}),
                           store.dumps(m), store.dumps(res["trades"])))
    return out


def choose_winner(results: list[dict]) -> dict | None:
    """Robust choice. Must have >=30 trades, positive expectancy in BOTH 2021-23 and 2024-now, and positive
    CAGR with stops/trail changed by +-20%. Among those, pick the best WORST-period CAGR after cost and tax
    (tie-break: full-period CAGR). Returns None if nothing qualifies."""
    best, best_key = None, None
    for r in results:
        m = r["metrics"]
        e, rc, full = m.get("early", {}), m.get("recent", {}), m.get("full", {})
        if full.get("trades", 0) < 30:
            continue
        if (e.get("expectancy") or -1) <= 0 or (rc.get("expectancy") or -1) <= 0:
            continue
        if any((v.get("cagr_after_cost_tax") or -1) <= 0 for v in m.get("sensitivity", {}).values()):
            continue
        worst = min(e.get("cagr_after_cost_tax") or -1, rc.get("cagr_after_cost_tax") or -1)
        key = (worst, full.get("cagr_after_cost_tax") or 0)
        if best_key is None or key > best_key:
            best, best_key = r, key
    return best
