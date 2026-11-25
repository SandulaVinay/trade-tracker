"""On-demand analysis of any NSE stock (used by chat, watchlist and PDF)."""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

import pandas as pd

from . import news, nse, signals, store, tradeplan
from .config import DEFAULT_CAPITAL, DEEP_CHECK_BUDGET_S, DEEP_CHECK_TIMEOUT_REASON, REGIME_PARTIAL_SIZE
from .market import compute_indicators, load_panel, regime_series
from .strategies import BY_KEY, base_filter
from .strategies import signals as strat_signals


def resolve_symbol(text: str) -> str | None:
    t = text.strip().upper()
    if store.q1("SELECT 1 FROM prices WHERE symbol=? LIMIT 1", (t,)):
        return t
    r = store.q1("SELECT symbol FROM universe WHERE UPPER(name) LIKE ? ORDER BY LENGTH(name) LIMIT 1", (f"%{t}%",))
    return r["symbol"] if r else None


def _manual_surveillance_urls(day: date) -> dict:
    """Official NSE pages to check manually when an automated check did not finish."""
    return {"asm": f"{nse.SITE}/api/reportASM", "gsm": f"{nse.SITE}/api/reportGSM",
            "ban": f"{nse.ARCHIVE}/archives/fo/sec_ban/fo_secban_{day:%d%m%Y}.csv"}


def _bounded_company_check(symbol: str, company: str, day: date) -> dict:
    """Run deep checks under one deadline; an unfinished check always blocks the stock."""
    out: dict = {}
    check = news.company_check

    def work():
        try:
            out["chk"] = check(symbol, company, day)
        except Exception as e:
            logging.getLogger("tt.analysis").warning("company check %s: %s", symbol, e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(DEEP_CHECK_BUDGET_S)
    if "chk" in out:
        return out["chk"]
    return {"symbol": symbol, "blocked": True, "block_reasons": [DEEP_CHECK_TIMEOUT_REASON],
            "warnings": [], "positives": [], "results": {}, "shareholding": {},
            "facts": [], "surveillance_urls": _manual_surveillance_urls(day), "results_event": None}


def analyse(symbol: str, deep: bool = True) -> dict:
    fr = signals.ensure_fresh()
    d = fr["latest"]
    if not d:
        return {"error": "No verified NSE data yet. Run 'Sync + scan now' first."}
    syms = [r["symbol"] for r in store.q("SELECT symbol FROM universe")]
    if symbol not in syms:
        syms.append(symbol)
    p = load_panel(symbols=syms, start=(date.fromisoformat(d) - timedelta(days=560)).isoformat())
    if symbol not in p.close.columns or pd.isna(p.close[symbol].iloc[-1]):
        return {"error": f"No NSE price for {symbol} on {d}."}
    compute_indicators(p)
    reg = regime_series(p).iloc[-1]
    size_mult = {"ON": 1.0, "PARTIAL": REGIME_PARTIAL_SIZE}.get(reg["state"], 0.0)
    key, m = signals.active_strategy()
    fired = []
    for s in BY_KEY.values():
        if s.key.endswith("N"):
            continue
        sig, _ = strat_signals(s, p)
        if bool(sig[symbol].iloc[-1]):
            fired.append(s.key)
    passes_base = bool(base_filter(p)[symbol].iloc[-1])
    row = signals._row(p, symbol)
    capital = store.kv_get("capital") or DEFAULT_CAPITAL
    if row["atr"] is None:
        return {"error": f"Not enough history for {symbol} to compute ATR."}
    plan = tradeplan.build(symbol, p.names.get(symbol, symbol), row, (m or {}).get("full", {}), capital,
                           size_mult if fired else 0.0, key, BY_KEY[key].name)
    plan["data_date"] = d
    plan["industry"] = p.industry.get(symbol, "Unknown")
    plan["why"] = (signals._why(symbol, row, BY_KEY[fired[0]], reg) if fired else
                   [f"No strategy signal on {d}: this is NOT a buy setup. Levels are shown for reference only.",
                    f"Passes base trend/liquidity filters: {'yes' if passes_base else 'no'}."])
    if deep:
        chk = _bounded_company_check(symbol, p.names.get(symbol, symbol), date.fromisoformat(d))
        plan["company"] = {k: chk[k] for k in ("results", "shareholding", "warnings", "positives", "results_event")}
        plan["company_facts"] = chk["facts"]
        plan["surveillance_urls"] = chk["surveillance_urls"]
        plan["blocked"] = chk["block_reasons"]
    plan["chart"] = [{"date": str(x.date()), "close": round(float(v), 2)} for x, v in p.raw_close[symbol].iloc[-180:].dropna().items()]
    return {"symbol": symbol, "data_date": d, "fresh": fr["fresh"], "signals_fired": fired, "passes_base": passes_base,
            "regime": {"state": reg["state"], "breadth": float(reg["breadth"]), "index": float(reg["index"]),
                       "index_200dma": float(reg["index_200dma"]), "size_mult": size_mult, "date": d},
            "plan": plan}
