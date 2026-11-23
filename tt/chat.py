"""Chat: rule-based commands (always available) + optional free LLM for free-form questions."""
from __future__ import annotations

import json
import re

from . import analysis, llm, pdf, signals, store, tracker

HELP = """Things you can ask:
- "today" or "pick" - today's pick (or why there is none)
- "analyse TCS" / "check Tata Motors" - full analysis of any NSE stock
- "pdf TCS" or "pdf today" - generate a PDF report with sources
- "holdings" / "open calls" - open and planned calls
- "average" - positions down from entry and whether averaging is allowed
- "upcoming" - stocks within 3% of a 52-week-high breakout
- "regime" - market health
- "watch TCS" / "unwatch TCS" - manage your watchlist
- "bought 12 at 1450" / "sold 12 at 1500" / "average 12 at 1400" - log actions on call #12
- "strategy" - backtest results and the active strategy"""


def _fmt_plan(plan: dict) -> str:
    p = plan
    pos = p["position"]
    lines = [f"{p['name']} ({p['symbol']}) - {p['strategy_name']} | NSE close {p.get('data_date')}",
             f"Buy zone Rs {p['buy_zone'][0]}-{p['buy_zone'][1]} | don't chase above Rs {p['no_chase_above']}",
             f"Stop Rs {p['stop']} ({p['stop_reason']})",
             f"T1 Rs {p['target1']} (~{p.get('t1_days_median') or '-'} sessions median) | T2 Rs {p['target2']} (~{p.get('t2_days_median') or '-'} sessions)",
             f"Size {pos['qty']} shares (Rs {pos['value']:,}, {pos['pct_capital']}% of capital), max loss Rs {pos['max_loss']:,}"]
    lines += ["Why: " + " ".join(p.get("why", []))]
    comp = p.get("company") or {}
    if comp.get("warnings"):
        lines.append("Warnings: " + "; ".join(comp["warnings"]))
    if p.get("blocked"):
        lines.append("BLOCKED: " + "; ".join(p["blocked"]))
    return "\n".join(lines)


def _log(role, text, attachment=None):
    with store.tx() as c:
        c.execute("INSERT INTO chat (at, role, text, attachment) VALUES (?,?,?,?)",
                  (store.now_ist().isoformat(), role, text, attachment))


def handle(msg: str) -> dict:
    _log("user", msg)
    out = _route(msg.strip())
    _log("assistant", out["text"], out.get("pdf"))
    return out


def _route(m: str) -> dict:
    low = m.lower()
    if low in ("help", "?", "hi", "hello"):
        return {"text": HELP}
    mm = re.match(r"^(bought|sold|average|avg)\s+#?(\d+)\s+(?:at|@)\s*([\d.]+)(?:\s+(?:qty\s*)?(\d+))?", low)
    if mm:
        act = {"bought": "buy", "sold": "sell", "avg": "average"}.get(mm.group(1), mm.group(1))
        try:
            c = tracker.manual(int(mm.group(2)), act, float(mm.group(3)), int(mm.group(4)) if mm.group(4) else None)
            return {"text": f"Logged {act} on call #{c['id']} {c['symbol']}: status {c['status']}, qty open {c['qty_open']}, "
                            f"avg price Rs {c['entry_price']}, stop Rs {c['stop']}."}
        except ValueError as e:
            return {"text": f"Not done: {e}"}
    if low.startswith("pdf"):
        rest = m[3:].strip()
        if rest.lower() in ("", "today", "pick"):
            sc = signals.latest_scan()
            if not sc or sc.get("status") != "pick":
                return {"text": "No pick in the latest scan, so there is no pick PDF. Try 'pdf TCS' for any stock."}
            f = pdf.plan_pdf(sc["pick"], sc)
            return {"text": f"PDF ready for {sc['pick']['symbol']}.", "pdf": f}
        sym = analysis.resolve_symbol(rest)
        if not sym:
            return {"text": f"Could not find '{rest}' in NSE data."}
        a = analysis.analyse(sym)
        if a.get("error"):
            return {"text": a["error"]}
        f = pdf.plan_pdf(a["plan"], {"regime": a["regime"], "strategy": _strategy_block()},
                         title=f"{a['plan']['name']} ({sym}) - analysis")
        return {"text": f"PDF ready for {sym}.", "pdf": f}
    mm = re.match(r"^(analy[sz]e|check|plan for|plan)\s+(.+)$", low)
    if mm:
        sym = analysis.resolve_symbol(m[len(mm.group(1)):].strip())
        if not sym:
            return {"text": "Stock not found in NSE data."}
        a = analysis.analyse(sym)
        if a.get("error"):
            return {"text": a["error"]}
        txt = _fmt_plan(a["plan"])
        txt += f"\nSignals today: {', '.join(a['signals_fired']) or 'none'} | Regime {a['regime']['state']}"
        if llm.enabled():
            r = llm.ask("Summarise this analysis for the user in plain language.", a)
            txt += "\n\nAI summary: " + (r["text"] if r["ok"] else f"(not shown: {r['reason']})")
        return {"text": txt, "analysis": a}
    if low in ("today", "pick", "today's pick", "todays pick"):
        sc = signals.latest_scan()
        if not sc:
            return {"text": "No scan yet. Use 'Run scan' on the Today tab."}
        if sc.get("status") == "pick":
            return {"text": _fmt_plan(sc["pick"])}
        return {"text": sc.get("message", "No pick.")}
    if low in ("holdings", "open calls", "calls", "positions"):
        rows = store.q("SELECT id,symbol,status,qty_open,entry_price,stop,last_price FROM calls WHERE status IN ('planned','open') ORDER BY id")
        if not rows:
            return {"text": "No open or planned calls."}
        return {"text": "\n".join(f"#{r['id']} {r['symbol']} {r['status']} qty {r['qty_open'] or 0} entry {r['entry_price'] or '-'} "
                                  f"stop {r['stop']} last {r['last_price'] or '-'}" for r in rows)}
    if low in ("average", "to average", "averaging"):
        rows = tracker.to_average()
        if not rows:
            return {"text": "No open position is more than 5% below entry."}
        return {"text": "\n".join(f"#{r['id']} {r['symbol']}: {r['cause']} -> "
                                  f"{'one tranche allowed' if r['average_ok'] else 'do not average'}" for r in rows)}
    if low in ("upcoming",):
        sc = signals.latest_scan() or {}
        up = sc.get("upcoming", [])
        return {"text": "\n".join(f"{u['symbol']}: Rs {u['close']} vs 52w high Rs {u['hi252']} ({u['gap_pct']}% away)" for u in up) or "None."}
    if low in ("regime", "market"):
        sc = signals.latest_scan() or {}
        r = sc.get("regime")
        if not r:
            return {"text": sc.get("message", "No scan yet.")}
        return {"text": f"Regime {r['state']} on {r['date']}: Nifty 500 {r['index']:.2f} vs 200-DMA {r['index_200dma']:.2f}, "
                        f"breadth {r['breadth']:.0%}. Size multiplier {r['size_mult']}."}
    mm = re.match(r"^(watch|unwatch)\s+(.+)$", low)
    if mm:
        sym = analysis.resolve_symbol(m[len(mm.group(1)):].strip())
        if not sym:
            return {"text": "Stock not found."}
        with store.tx() as c:
            if mm.group(1) == "watch":
                c.execute("INSERT OR REPLACE INTO watchlist VALUES (?,?,?,?)", (sym, store.now_ist().isoformat(), "manual", ""))
            else:
                c.execute("DELETE FROM watchlist WHERE symbol=?", (sym,))
        return {"text": f"{'Added' if mm.group(1) == 'watch' else 'Removed'} {sym}."}
    if low in ("strategy", "backtest", "strategies"):
        b = _strategy_block()
        s = b.get("stats", {})
        return {"text": f"Active: {b['name']} ({b['key']}). Win rate {s.get('win_rate', 0):.0%}, expectancy {s.get('expectancy', 0):.2%}/trade, "
                        f"max DD {s.get('max_drawdown', 0):.0%}, CAGR after cost+tax {s.get('cagr_after_cost_tax') or 0:.1%} "
                        f"vs Nifty 500 {s.get('bench_cagr') or 0:.1%}. See the Strategy Lab tab." if s else "No backtest yet."}
    if llm.enabled():
        sc = signals.latest_scan() or {}
        ctx = {"latest_scan": {k: sc.get(k) for k in ("status", "message", "regime", "pick", "strategy", "upcoming")},
               "open_calls": store.q("SELECT id,symbol,status,entry_price,stop,last_price,qty_open FROM calls WHERE status IN ('open','planned')")}
        r = llm.ask(m, ctx)
        if r["ok"]:
            return {"text": r["text"]}
        return {"text": f"I could not give a verified answer ({r['reason']}).\n\n{HELP}"}
    return {"text": "I didn't recognise that. " + HELP}


def _strategy_block() -> dict:
    from .strategies import BY_KEY
    key, m = signals.active_strategy()
    return {"key": key, "name": BY_KEY[key].name, "stats": (m or {}).get("full", {}),
            "recent": (m or {}).get("recent", {}), "early": (m or {}).get("early", {})}
