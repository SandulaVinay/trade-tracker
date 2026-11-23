"""Call tracker: paper fills, stops, targets, trailing, time exits, cause analysis and averaging alerts.

Same rules as the backtest. Paper fills use the next session's NSE OHLC:
  - open within [buy-zone low, no-chase] -> fill at open (a lower open also fills at open)
  - open above no-chase but low <= buy-zone high -> limit fill at buy-zone high
  - otherwise not filled; the plan expires after 3 sessions.
"""
from __future__ import annotations

import bisect
import json
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import costs, news, store
from .config import TIME_EXIT_DAYS, TRAIL_ATR, T1_BOOK_FRACTION, MAX_POSITION_PCT
from .market import compute_indicators, load_panel, regime_series

log = logging.getLogger("tt.tracker")
PLAN_VALID_SESSIONS = 3


def create_call(plan: dict, signal_date: str, mode: str = "paper") -> int | None:
    if store.q1("SELECT id FROM calls WHERE symbol=? AND status IN ('planned','open')", (plan["symbol"],)):
        return None
    with store.tx() as c:
        cur = c.execute("""INSERT INTO calls (created_at, signal_date, symbol, name, strategy, status, mode, plan,
                           stop, last_date, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (store.now_ist().isoformat(), signal_date, plan["symbol"], plan["name"], plan["strategy"],
                         "planned", mode, json.dumps(plan, default=str), plan["stop"], signal_date, ""))
        cid = cur.lastrowid
    event(cid, signal_date, "plan", f"New plan: buy zone Rs {plan['buy_zone'][0]}-{plan['buy_zone'][1]}, "
                                    f"stop Rs {plan['stop']}, T1 Rs {plan['target1']}, T2 Rs {plan['target2']}.")
    return cid


def event(cid: int, d: str, kind: str, msg: str, price: float | None = None, qty: int | None = None):
    with store.tx() as c:
        c.execute("INSERT INTO call_events (call_id,date,kind,message,price,qty) VALUES (?,?,?,?,?,?)",
                  (cid, d, kind, msg, price, qty))


def _upd(cid: int, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    with store.tx() as c:
        c.execute(f"UPDATE calls SET {cols} WHERE id=?", (*kw.values(), cid))


def _sell(call: dict, d: str, px: float, qty: int, reason: str, kind: str = "exit"):
    val = px * qty
    ch = costs.sell_charges(val)
    realized = (call["realized"] or 0) + val
    charges = (call["charges"] or 0) + ch
    qty_open = call["qty_open"] - qty
    call.update(realized=realized, charges=charges, qty_open=qty_open)
    upd = dict(realized=realized, charges=charges, qty_open=qty_open)
    if qty_open <= 0:
        upd.update(status="closed", exit_date=d, exit_price=round(realized / call["qty"], 2), exit_reason=reason)
        call["status"] = "closed"
    _upd(call["id"], **upd)
    event(call["id"], d, kind, f"{reason}: sold {qty} @ Rs {px:.2f}", px, qty)


def _buy(call: dict, d: str, px: float, qty: int, kind: str = "fill", msg: str = ""):
    val = px * qty
    ch = costs.buy_charges(val)
    new_qty = (call["qty"] or 0) + qty
    avg = ((call["entry_price"] or 0) * (call["qty"] or 0) + val) / new_qty
    call.update(qty=new_qty, qty_open=(call["qty_open"] or 0) + qty, entry_price=avg,
                charges=(call["charges"] or 0) + ch)
    upd = dict(qty=new_qty, qty_open=call["qty_open"], entry_price=round(avg, 2), charges=call["charges"])
    if kind == "fill":
        upd.update(status="open", entry_date=d, highest=px)
        call.update(status="open", entry_date=d, highest=px)
    _upd(call["id"], **upd)
    event(call["id"], d, kind, msg or f"Bought {qty} @ Rs {px:.2f}", px, qty)


def _cause(p, reg, sym: str, i: int, entry_i: int) -> str:
    """Classify a fall since entry: market-wide / sector-wide / stock-specific."""
    s_ret = p.close[sym].iloc[i] / p.close[sym].iloc[entry_i] - 1
    m = reg["index"]
    m_ret = m.iloc[i] / m.iloc[entry_i] - 1
    peers = [x for x in p.close.columns if p.industry.get(x) == p.industry.get(sym) and x != sym]
    sec_ret = float((p.close[peers].iloc[i] / p.close[peers].iloc[entry_i] - 1).median()) if peers else np.nan
    if m_ret <= 0.6 * s_ret:
        return f"market-wide (stock {s_ret:.1%}, Nifty 500 {m_ret:.1%})"
    if np.isfinite(sec_ret) and sec_ret <= 0.6 * s_ret:
        return f"sector-wide (stock {s_ret:.1%}, industry median {sec_ret:.1%}, Nifty 500 {m_ret:.1%})"
    return f"stock-specific (stock {s_ret:.1%}, industry {sec_ret:.1%}, Nifty 500 {m_ret:.1%})"


def update_all(red_flag_check: bool = True) -> dict:
    calls = store.q("SELECT * FROM calls WHERE status IN ('planned','open')")
    if not calls:
        return {"updated": 0}
    start = (date.fromisoformat(min(c["signal_date"] for c in calls)) - timedelta(days=420)).isoformat()
    p = load_panel(start=start)
    compute_indicators(p)
    reg = regime_series(p)
    dates = [str(x.date()) for x in p.dates]
    n = 0
    for call in calls:
        sym = call["symbol"]
        if sym not in p.close.columns:
            continue
        plan = json.loads(call["plan"])
        mode = call["mode"]
        # First session strictly after the last processed date. A date that is not a session in the
        # panel (weekend / holiday manual entry) must not restart the walk from the first bar.
        start_i = bisect.bisect_right(dates, call["last_date"] or call["signal_date"])
        # Position of the signal session (last session on or before the signal date).
        signal_i = bisect.bisect_right(dates, call["signal_date"]) - 1
        for i in range(max(start_i, 1), len(dates)):
            d = dates[i]
            o, h, l, c = (float(x[sym].iloc[i]) for x in (p.open, p.high, p.low, p.close))
            if not np.isfinite(c):
                continue
            a = float(p.ind["atr"][sym].iloc[i])
            if call["status"] == "planned":
                if mode == "live":
                    break  # live calls are filled by the user, not simulated
                sessions = i - signal_i
                lo, hi = plan["buy_zone"]
                nc = plan["no_chase_above"]
                fill = None
                if o <= nc:
                    fill = o
                elif l <= hi:
                    fill = hi
                if fill is not None and plan["position"]["qty"] > 0 and fill > plan["stop"]:
                    call.update(qty=0, qty_open=0, entry_price=0)
                    _buy(call, d, fill, int(plan["position"]["qty"]), "fill",
                         f"Paper fill {plan['position']['qty']} @ Rs {fill:.2f} (open Rs {o:.2f})")
                    call["stop"] = plan["stop"]
                    call["highest"] = c
                    call["entry_i"] = i
                elif sessions >= PLAN_VALID_SESSIONS:
                    _upd(call["id"], status="expired", exit_reason="not filled within 3 sessions")
                    event(call["id"], d, "expired", "Plan expired: price never traded in the buy zone below the no-chase level.")
                    call["status"] = "expired"
                    break
                else:
                    continue
            # ---- open position management (same rules as backtest)
            stop = call["stop"]
            if mode == "paper":
                if o <= stop:
                    _sell(call, d, o, call["qty_open"], "Stop-loss (gap down)")
                    break
                if l <= stop:
                    _sell(call, d, stop, call["qty_open"], "Stop-loss" if not call["t1_done"] else "Trailing stop")
                    break
                if not call["t1_done"] and h >= plan["target1"]:
                    q = max(1, int(round(call["qty"] * T1_BOOK_FRACTION)))
                    _sell(call, d, max(o, plan["target1"]), min(q, call["qty_open"]), "Target 1 hit", "target")
                    call["t1_done"] = 1
                    stop = max(stop, call["entry_price"])
                    _upd(call["id"], t1_done=1)
                    if call["status"] == "closed":
                        break
                if call["t1_done"] and not plan.get("_t2_done") and h >= plan["target2"]:
                    q = max(1, int(round(call["qty"] * T1_BOOK_FRACTION)))
                    _sell(call, d, max(o, plan["target2"]), min(q, call["qty_open"]), "Target 2 hit", "target")
                    plan["_t2_done"] = True
                    _upd(call["id"], plan=json.dumps(plan, default=str))
                    if call["status"] == "closed":
                        break
            else:  # live: alerts only
                if l <= stop:
                    event(call["id"], d, "alert", f"Price touched stop Rs {stop:.2f} (low Rs {l:.2f}). Rule: exit.")
                if not call["t1_done"] and h >= plan["target1"]:
                    event(call["id"], d, "alert", f"Target 1 Rs {plan['target1']} reached. Rule: book {T1_BOOK_FRACTION:.0%}.")
            highest = max(call["highest"] or c, c)
            if np.isfinite(a) and call["t1_done"]:
                new = highest - TRAIL_ATR * a
                if new > stop:
                    stop = round(new, 2)
                    event(call["id"], d, "trail", f"Trailing stop raised to Rs {stop:.2f}")
            if reg["state"].iloc[i] == "OFF" and np.isfinite(a) and c - 2 * a > stop:
                stop = round(c - 2 * a, 2)
                event(call["id"], d, "regime", f"Market regime OFF: stop tightened to Rs {stop:.2f} (2x ATR)")
            call.update(stop=stop, highest=highest)
            _upd(call["id"], stop=stop, highest=highest, trail_stop=stop)
            # first session on or after the entry date (a weekend manual entry counts from the next session)
            entry_i = min(bisect.bisect_left(dates, call["entry_date"]), i) if call["entry_date"] else i
            held = i - entry_i
            if mode == "paper" and held >= TIME_EXIT_DAYS and not call["t1_done"]:
                _sell(call, d, c, call["qty_open"], f"Time exit (day {held}, Target 1 not reached)")
                break
            if mode == "paper" and c < float(p.ind["sma50"][sym].iloc[i]) and call["t1_done"]:
                pass  # trailing stop governs after T1; 50-DMA is shown as a warning only
            # cause analysis + averaging conditions when below entry
            if c < call["entry_price"] * 0.95 and held >= 1:
                cause = _cause(p, reg, sym, i, entry_i)
                lows = p.low[sym].iloc[max(0, i - 15):i + 1]
                higher_low = len(lows) > 10 and lows.iloc[-5:].min() > lows.iloc[:-5].min()
                above20 = c > float(p.ind["sma20"][sym].iloc[i])
                ok = cause.startswith("market") and higher_low and above20 and not call["averaged"]
                flag = {"date": d, "cause": cause, "higher_low": bool(higher_low), "above_20dma": bool(above20),
                        "average_ok": bool(ok)}
                prev = json.loads(call["notes"] or "{}") if (call["notes"] or "").startswith("{") else {}
                if prev.get("cause_kind") != cause.split(" ")[0] or prev.get("average_ok") != ok:
                    event(call["id"], d, "cause", f"Down {c / call['entry_price'] - 1:.1%} from entry. Cause: {cause}. "
                          + ("Averaging conditions MET (one tranche allowed)." if ok else
                             "Stock-specific: exit, do not average." if cause.startswith("stock") else
                             "Averaging conditions not met: wait."))
                flag["cause_kind"] = cause.split(" ")[0]
                _upd(call["id"], notes=json.dumps(flag))
                call["notes"] = json.dumps(flag)
        if call["status"] in ("open", "planned"):
            _upd(call["id"], last_date=dates[-1], last_price=float(p.raw_close[sym].iloc[-1]))
        n += 1
    # red-flag check for open positions (Tier-1 filings)
    if red_flag_check:
        today = date.fromisoformat(dates[-1])
        for call in store.q("SELECT * FROM calls WHERE status='open'"):
            for f in news.announcements(call["symbol"], today, days=3):
                if f.extra["red_flags"] and not store.q1(
                        "SELECT 1 FROM call_events WHERE call_id=? AND kind='redflag' AND message LIKE ?",
                        (call["id"], f"%{f.key}%")):
                    event(call["id"], f.as_of, "redflag",
                          f"RED FLAG ({', '.join(f.extra['red_flags'])}) in NSE filing {f.as_of} [{f.key}]: {f.url}. Rule: exit.")
    return {"updated": n, "data_date": dates[-1]}


# ---------------------------------------------------------------- manual actions
def manual(call_id: int, action: str, price: float, qty: int | None = None, d: str | None = None) -> dict:
    call = store.q1("SELECT * FROM calls WHERE id=?", (call_id,))
    if not call:
        raise ValueError("call not found")
    d = d or store.now_ist().date().isoformat()
    if action in ("buy", "average", "sell"):
        if price is None or not price > 0:
            raise ValueError("a positive price is required")
        if qty is not None and qty <= 0:
            raise ValueError("quantity must be positive")
    if action == "buy":
        if call["status"] != "planned":
            raise ValueError("call is not in planned state")
        qty = qty or json.loads(call["plan"])["position"]["qty"]
        call.update(qty=0, qty_open=0, entry_price=0)
        _buy(call, d, price, qty, "fill", f"Bought {qty} @ Rs {price:.2f} (manual)")
        _upd(call_id, last_date=d)
    elif action == "average":
        if call["status"] != "open" or call["averaged"]:
            raise ValueError("averaging not allowed (not open or already averaged once)")
        plan = json.loads(call["plan"])
        qty = min(qty or call["qty"], call["qty"])
        cap = plan["position"]["capital"]
        if (call["qty_open"] + qty) * price > MAX_POSITION_PCT * cap:
            qty = max(0, int(MAX_POSITION_PCT * cap / price) - call["qty_open"])
        if qty <= 0:
            raise ValueError("position already at the maximum size")
        _buy(call, d, price, qty, "average", f"Averaged {qty} @ Rs {price:.2f} (one tranche)")
        _upd(call_id, averaged=1)
    elif action == "sell":
        if call["status"] != "open":
            raise ValueError("call is not open")
        qty = min(qty or call["qty_open"], call["qty_open"])
        _sell(call, d, price, qty, "Manual exit")
    elif action == "cancel":
        if call["status"] != "planned":
            raise ValueError("only a planned (unfilled) call can be cancelled; sell an open position instead")
        _upd(call_id, status="cancelled", exit_reason="cancelled by user")
        event(call_id, d, "cancel", "Plan cancelled by user")
    elif action == "live":
        if call["status"] not in ("planned", "open"):
            raise ValueError("call is not active")
        _upd(call_id, mode="live")
        event(call_id, d, "mode", "Marked as a live trade (no simulated fills)")
    else:
        raise ValueError(f"unknown action '{action}'")
    return store.q1("SELECT * FROM calls WHERE id=?", (call_id,))


def summary() -> dict:
    closed = store.q("SELECT * FROM calls WHERE status='closed'")
    out = {"closed": len(closed)}
    if closed:
        pnl = [(c["realized"] - c["entry_price"] * c["qty"] - c["charges"]) for c in closed]
        rets = [x / (c["entry_price"] * c["qty"]) for x, c in zip(pnl, closed)]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        out.update(win_rate=len(wins) / len(closed), avg_win=float(np.mean(wins)) if wins else 0,
                   avg_loss=float(np.mean(losses)) if losses else 0, total_pnl=float(sum(pnl)),
                   expectancy=float(np.mean(rets)))
    out["open"] = store.q1("SELECT COUNT(*) n FROM calls WHERE status='open'")["n"]
    out["planned"] = store.q1("SELECT COUNT(*) n FROM calls WHERE status='planned'")["n"]
    return out


def to_average() -> list[dict]:
    rows = []
    for c in store.q("SELECT * FROM calls WHERE status='open'"):
        try:
            n = json.loads(c["notes"] or "{}")
        except json.JSONDecodeError:
            n = {}
        if n.get("cause"):
            rows.append({"id": c["id"], "symbol": c["symbol"], "name": c["name"], "entry": c["entry_price"],
                         "last": c["last_price"], "stop": c["stop"], "cause": n["cause"],
                         "average_ok": n.get("average_ok"), "higher_low": n.get("higher_low"),
                         "above_20dma": n.get("above_20dma"), "date": n.get("date"), "averaged": c["averaged"]})
    return rows
