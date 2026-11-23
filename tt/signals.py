"""Daily scan: freshness check -> regime -> gates -> ranking -> company checks -> one pick (or none)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from . import data_sync, news, store, tradeplan
from .confidence import Fact, two_date_check
from .config import DEFAULT_CAPITAL, REGIME_PARTIAL_SIZE
from .market import compute_indicators, load_panel, regime_series
from .nse import client
from .strategies import BY_KEY, base_filter, signals

log = logging.getLogger("tt.signals")
DEFAULT_STRATEGY = "S2"
MAX_DEEP_CHECKS = 6


# ---------------------------------------------------------------- calendar
def holidays() -> set[date]:
    cached = store.kv_get("holidays")
    today = store.now_ist().date().isoformat()
    if cached and cached.get("day") == today:
        return {date.fromisoformat(d) for d in cached["dates"]}
    try:
        url, hs = client.holidays()
        store.kv_set("holidays", {"day": today, "dates": sorted(d.isoformat() for d in hs), "url": url})
        return hs
    except Exception as e:
        log.warning("holiday list: %s", e)
        return {date.fromisoformat(d) for d in (cached or {}).get("dates", [])}


def is_trading_day(d: date, hs: set[date]) -> bool:
    return d.weekday() < 5 and d not in hs


def expected_data_date(now: datetime | None = None) -> date:
    """Latest trading day whose bhavcopy should already be published (NSE publishes ~18:00 IST)."""
    now = now or store.now_ist()
    hs = holidays()
    d = now.date() if now.time() >= time(18, 0) else now.date() - timedelta(days=1)
    while not is_trading_day(d, hs):
        d -= timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    hs = holidays()
    d -= timedelta(days=1)
    while not is_trading_day(d, hs):
        d -= timedelta(days=1)
    return d


def ensure_fresh() -> dict:
    """Make sure the DB holds the expected latest trading day; two-date verified."""
    exp = expected_data_date()
    latest = data_sync.latest_data_date()
    if latest is None or latest < exp.isoformat():
        data_sync.sync_range(exp - timedelta(days=10), exp)
        latest = data_sync.latest_data_date()
    row = store.q1("SELECT * FROM sync_log WHERE kind='bhav' AND date=?", (exp.isoformat(),))
    idx = store.q1("SELECT * FROM sync_log WHERE kind='index' AND date=?", (exp.isoformat(),))
    fresh = bool(row and row["status"] == "ok" and idx and idx["status"] == "ok")
    return {"expected": exp.isoformat(), "latest": latest, "fresh": fresh,
            "bhav": row, "index": idx}


# ---------------------------------------------------------------- strategy stats
def active_strategy() -> tuple[str, dict]:
    key = store.kv_get("active_strategy") or DEFAULT_STRATEGY
    r = store.q1("SELECT metrics FROM backtests WHERE strategy=? ORDER BY id DESC LIMIT 1", (key,))
    return key, (store.loads(r["metrics"]) if r else {})


# ---------------------------------------------------------------- F&O context (untested, info only)
def fo_context(symbols: list[str], d: date) -> dict:
    try:
        f = client.fo_bhavcopy(d)
    except Exception as e:
        return {"error": str(e)}
    df = f.data
    out = {"_url": f.url, "_date": d.isoformat()}
    for s in symbols:
        x = df[df["TckrSymb"] == s]
        if x.empty:
            continue
        fut = x[x["FinInstrmTp"] == "STF"]
        opt = x[x["FinInstrmTp"] == "STO"].copy()
        info = {"in_fo": True}
        if not fut.empty:
            oi_chg = float(fut["ChngInOpnIntrst"].sum())
            px_chg = float(fut.sort_values("XpryDt").iloc[0]["ClsPric"] - fut.sort_values("XpryDt").iloc[0]["PrvsClsgPric"])
            info["fut_oi_change"] = oi_chg
            info["buildup"] = ("Long build-up" if px_chg > 0 and oi_chg > 0 else "Short covering" if px_chg > 0
                               else "Short build-up" if oi_chg > 0 else "Long unwinding")
        if not opt.empty:
            near = sorted(opt["XpryDt"].unique())[0]
            o = opt[opt["XpryDt"] == near]
            und = float(o["UndrlygPric"].iloc[0])
            ce = o[(o["OptnTp"] == "CE") & (o["StrkPric"] >= und)]
            pe = o[(o["OptnTp"] == "PE") & (o["StrkPric"] <= und)]
            if not ce.empty:
                r = ce.loc[ce["OpnIntrst"].idxmax()]
                info["max_call_oi_strike"] = float(r["StrkPric"])
            if not pe.empty:
                r = pe.loc[pe["OpnIntrst"].idxmax()]
                info["max_put_oi_strike"] = float(r["StrkPric"])
            info["expiry"] = str(near)
        out[s] = info
    return out


# ---------------------------------------------------------------- second-source price check
PRICE_XCHECK_TOL = 0.005


def cross_check_source(d: date):
    try:
        return client.cm_udiff(d)
    except Exception as e:
        log.warning("cross-check file %s: %s", d, e)
        return None


def cross_check_price(xc, sym: str, close: float, d: str) -> tuple[dict | None, str | None]:
    """Compare the bhavcopy close with NSE's separate UDiFF cash-market file. >0.5% apart => block."""
    if xc is None:
        return None, "Second-source price check unavailable (NSE UDiFF file missing or date mismatch)"
    df = xc.data
    row = df[(df["TckrSymb"] == sym) & (df["SctySrs"].isin(["EQ", "BE", "BZ"]))]
    if row.empty:
        return None, f"{sym} not found in the second NSE price file"
    other = float(row.iloc[0]["ClsPric"])
    diff = abs(other / close - 1) if close else 1.0
    f = Fact(f"xcheck:{sym}", "Price cross-check (NSE UDiFF file)",
             f"bhavcopy close Rs {close:.2f} vs UDiFF close Rs {other:.2f} (diff {diff:.2%})",
             xc.url, str(xc.content_date), store.now_ist().isoformat(), str(xc.label_date), tier=1,
             confirmations=1 if diff <= PRICE_XCHECK_TOL else 0, conflict=diff > PRICE_XCHECK_TOL,
             date_check=two_date_check(xc.label_date, d)).finalize().to_dict()
    if diff > PRICE_XCHECK_TOL:
        return f, f"Price sources disagree by {diff:.2%} (> {PRICE_XCHECK_TOL:.1%})"
    return f, None


# ---------------------------------------------------------------- scan
def _row(p, sym: str, i: int = -1) -> dict:
    out = {"close": float(p.close[sym].iloc[i]), "raw_close": float(p.raw_close[sym].iloc[i])}
    for k in ("atr", "sma20", "sma50", "sma200", "hi252", "hi252_prev", "lo20", "rsi14", "vol_ratio",
              "deliv_ratio", "mom_score", "ret63", "ret21", "sector_rank", "med_turnover"):
        v = p.ind[k][sym].iloc[i]
        out[k] = float(v) if pd.notna(v) else None
    v = p.deliv_per[sym].iloc[i]
    out["deliv_per"] = float(v) if pd.notna(v) else None
    # convert adjusted levels to today's raw price scale (identical unless a corporate action is pending)
    return out


def market_facts(fr: dict, reg_row: pd.Series, d: str) -> list[dict]:
    facts = []
    b, ix = fr.get("bhav") or {}, fr.get("index") or {}
    if b:
        facts.append(Fact("bhavcopy", "NSE bhavcopy (prices, volume, delivery %)", f"{b['rows']} rows", b["url"],
                          b["content_date"], b["fetched_at"], b["label_date"], tier=1,
                          date_check=two_date_check(b["label_date"], b["content_date"])).finalize().to_dict())
    if ix:
        facts.append(Fact("indices", "NSE index closes", f"Nifty 500 {reg_row['index']:.2f}, 200-DMA {reg_row['index_200dma']:.2f}",
                          ix["url"], ix["content_date"], ix["fetched_at"], ix["label_date"], tier=1,
                          date_check=two_date_check(ix["label_date"], ix["content_date"])).finalize().to_dict())
    try:
        url, js = client.fii_dii()
        for r in js:
            dd = datetime.strptime(r["date"], "%d-%b-%Y").date()
            facts.append(Fact(f"flows:{r['category']}", f"{r['category']} net (cash, Rs cr)", r["netValue"], url,
                              dd.isoformat(), store.now_ist().isoformat(), d, tier=1,
                              date_check=two_date_check(dd, d)).finalize().to_dict())
    except Exception as e:
        log.warning("fii/dii: %s", e)
    return facts


def run_scan(capital: float | None = None, deep_checks: int = MAX_DEEP_CHECKS, create_call: bool = True) -> dict:
    run_at = store.now_ist()
    capital = capital or store.kv_get("capital") or DEFAULT_CAPITAL
    fr = ensure_fresh()
    result: dict = {"run_at": run_at.isoformat(), "freshness": fr, "capital": capital}
    if not fr["fresh"]:
        result.update(status="stale", message=f"No pick today: NSE data for {fr['expected']} is not available or "
                                              f"failed the two-date check (latest verified: {fr['latest']}).")
        _save(fr["expected"], result)
        return result
    d = fr["expected"]
    start = (date.fromisoformat(d) - timedelta(days=560)).isoformat()
    p = load_panel(start=start)
    if str(p.dates[-1].date()) != d:
        result.update(status="stale", message=f"Panel last date {p.dates[-1].date()} != expected {d}.")
        _save(d, result)
        return result
    compute_indicators(p)
    reg = regime_series(p)
    rr = reg.iloc[-1]
    state = rr["state"]
    size_mult = {"ON": 1.0, "PARTIAL": REGIME_PARTIAL_SIZE}.get(state, 0.0)
    result["regime"] = {"state": state, "index": float(rr["index"]), "index_200dma": float(rr["index_200dma"]),
                        "breadth": float(rr["breadth"]), "size_mult": size_mult, "date": d,
                        "history": [{"date": str(k.date()), "breadth": round(float(v["breadth"]), 4), "state": v["state"],
                                     "index": float(v["index"])} for k, v in reg.iloc[-120:].iterrows()]}
    result["market_facts"] = market_facts(fr, rr, d)

    key, m = active_strategy()
    strat = BY_KEY[key]
    stats = (m or {}).get("full", {})
    result["strategy"] = {"key": key, "name": strat.name, "rules": strat.rules, "stats": stats,
                          "recent": (m or {}).get("recent", {}), "early": (m or {}).get("early", {})}

    # candidates from every strategy (active one decides the pick)
    all_sigs = {}
    for s in BY_KEY.values():
        if s.key.endswith("N"):
            continue
        sig, score = signals(s, p)
        last = sig.iloc[-1]
        all_sigs[s.key] = sorted(((float(score[x].iloc[-1]), x) for x in last[last].index), reverse=True)
    result["signals_by_strategy"] = {k: [x for _, x in v][:15] for k, v in all_sigs.items()}

    # already held or planned on an earlier day => skip; a plan created by an earlier run of today's scan stays eligible
    held = {r["symbol"] for r in store.q("SELECT symbol FROM calls WHERE status='open' OR (status='planned' AND signal_date < ?)", (d,))}
    cands = [x for _, x in all_sigs.get(key, []) if x not in held]
    today = date.fromisoformat(d)
    checked, pick = [], None
    if size_mult > 0 and cands:
        xc = cross_check_source(today)
        for sym in cands[:deep_checks]:
            xfact, xerr = cross_check_price(xc, sym, float(p.raw_close[sym].iloc[-1]), d)
            chk = news.company_check(sym, p.names[sym], today, deep=True)
            if xerr:
                chk["block_reasons"].append(xerr)
                chk["blocked"] = True
            if xfact:
                chk["facts"].insert(0, xfact)
            checked.append({"symbol": sym, "name": p.names[sym], "blocked": chk["blocked"],
                            "block_reasons": chk["block_reasons"], "warnings": chk["warnings"]})
            if not chk["blocked"]:
                pick = (sym, chk)
                break
    result["checked"] = checked

    # upcoming: within 3% of 52-week high, not yet triggered, passing base filter
    bf = base_filter(p).iloc[-1]
    near = (p.close.iloc[-1] >= 0.97 * p.ind["hi252_prev"].iloc[-1]) & (p.close.iloc[-1] < p.ind["hi252_prev"].iloc[-1])
    up = [s for s in p.close.columns if bf.get(s, False) and near.get(s, False)]
    up.sort(key=lambda s: -(p.ind["mom_score"][s].iloc[-1] if pd.notna(p.ind["mom_score"][s].iloc[-1]) else -9))
    result["upcoming"] = [{"symbol": s, "name": p.names[s], "close": round(float(p.raw_close[s].iloc[-1]), 2),
                           "hi252": round(float(p.ind["hi252_prev"][s].iloc[-1]), 2),
                           "gap_pct": round(100 * (p.ind["hi252_prev"][s].iloc[-1] / p.close[s].iloc[-1] - 1), 2),
                           "vol_ratio": round(float(p.ind["vol_ratio"][s].iloc[-1]), 2) if pd.notna(p.ind["vol_ratio"][s].iloc[-1]) else None,
                           "industry": p.industry[s]} for s in up[:15]]

    if size_mult == 0:
        result.update(status="no_trade", message=f"No trade today: market regime is {state} "
                                                 f"(Nifty 500 vs 200-DMA and breadth {rr['breadth']:.0%}).")
    elif not pick:
        result.update(status="no_trade", message="No trade today: no stock passed every gate and company check."
                      if cands else f"No trade today: no {strat.name} signal on {d}.")
    else:
        sym, chk = pick
        row = _row(p, sym)
        plan = tradeplan.build(sym, p.names[sym], row, stats, capital, size_mult, key, strat.name)
        plan["industry"] = p.industry[sym]
        plan["data_date"] = d
        hs = holidays()
        # the trade starts next session, so the relevant expiry is the first one after the data date
        exp = tradeplan.next_monthly_expiry(today + timedelta(days=1), hs)
        plan["expiry"] = {"next_monthly": exp.isoformat(), "trading_days_to": int(np.busday_count(today, exp, holidays=list(hs)))}
        plan["fo"] = fo_context([sym], today).get(sym, {"in_fo": False})
        plan["company"] = {k: chk[k] for k in ("results", "shareholding", "warnings", "positives", "results_event")}
        plan["company_facts"] = chk["facts"]
        plan["surveillance_urls"] = chk["surveillance_urls"]
        plan["chart"] = [{"date": str(x.date()), "close": round(float(v), 2)} for x, v in p.raw_close[sym].iloc[-180:].dropna().items()]
        plan["why"] = _why(sym, row, strat, rr)
        result.update(status="pick", pick=plan,
                      message=f"Pick for next session: {p.names[sym]} ({sym}) via {strat.name}.")
        if create_call:
            from . import tracker
            tracker.create_call(plan, d)
    _save(d, result)
    return result


def _why(sym, row, strat, rr) -> list[str]:
    out = [f"Signal: {strat.name} ({strat.key}) fired on the {rr.name.date()} close."]
    if row.get("hi252_prev"):
        out.append(f"Close Rs {row['close']:.2f} vs prior 52-week high Rs {row['hi252_prev']:.2f}.")
    if row.get("vol_ratio"):
        out.append(f"Volume {row['vol_ratio']:.1f}x its 20-day average.")
    if row.get("deliv_per") is not None and row.get("deliv_ratio"):
        out.append(f"Delivery {row['deliv_per']:.1f}% ({row['deliv_ratio']:.2f}x its 20-day average).")
    out.append(f"Above 50-DMA Rs {row['sma50']:.2f} and 200-DMA Rs {row['sma200']:.2f}.")
    if row.get("sector_rank") is not None:
        out.append(f"Industry strength percentile {row['sector_rank']:.0%} (3-month return vs other industries).")
    return out


def _save(d: str, result: dict) -> None:
    with store.tx() as c:
        c.execute("INSERT INTO scans (data_date, run_at, status, result) VALUES (?,?,?,?)",
                  (d, result["run_at"], result.get("status"), store.dumps(result)))


def latest_scan() -> dict | None:
    """Latest scan. If the newest run was stale (data not fresh), return the last good scan's data,
    clearly labelled, with the stale notice on top, so the page never silently shows old data as new."""
    r = store.q1("SELECT result, status FROM scans ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    res = store.loads(r["result"])
    if r["status"] == "stale":
        good = store.q1("SELECT result FROM scans WHERE status IN ('pick','no_trade') ORDER BY id DESC LIMIT 1")
        if good:
            g = store.loads(good["result"])
            g["stale_notice"] = res.get("message")
            g["freshness"] = res.get("freshness")
            g["message"] = (f"{res.get('message')} Showing the last verified scan "
                            f"(NSE close {(g.get('regime') or {}).get('date', '?')}) for reference only.")
            g["status"] = "stale"
            g.pop("pick", None)  # never present an old pick as today's pick
            return g
    return res
