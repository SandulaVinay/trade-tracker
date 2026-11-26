"""Rule-based trade-setup verdict from verified analysis data."""
from __future__ import annotations

from .strategies import BY_KEY

DISCLAIMER = "Analysis from published rules and NSE data, not investment advice. You make the final decision."


def _d(v):
    return v if isinstance(v, dict) else {}


def _l(v):
    return v if isinstance(v, list) else []


def _s(v):
    return "" if v is None else str(v)


def _f(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _money(v):
    x = _f(v)
    return "data missing" if x is None else f"Rs {x:,.2f}"


def _crore(v):
    """Large rupee amounts (results) in crore, the unit Indian filings use."""
    x = _f(v)
    if x is None:
        return "data missing"
    if abs(x) >= 1e7:
        return f"Rs {x / 1e7:,.2f} Cr"
    return f"Rs {x:,.2f}"


def _dated(text, when):
    return f"{text} (as of {when})" if when else text


def _fact_lines(facts, words):
    out = []
    for item in facts:
        f = _d(item)
        label = _s(f.get("label"))
        if words and not any(w in label.lower() for w in words):
            continue
        when = _s(f.get("as_of") or f.get("date"))
        out.append(_dated(f"{label}: {f.get('value', '')}".strip(), when))
    return out


def verdict(result: dict) -> dict:
    a = _d(result)
    plan = _d(a.get("plan"))
    company = _d(plan.get("company"))
    facts = _l(plan.get("company_facts")) or _l(a.get("company_facts"))
    as_of = _s(a.get("data_date") or plan.get("data_date"))
    fetched_at = _s(a.get("fetched_at") or as_of)
    name = _s(a.get("name") or plan.get("name")) or _s(a.get("symbol") or plan.get("symbol"))
    symbol = _s(a.get("symbol") or plan.get("symbol"))
    regime = _s(_d(a.get("regime")).get("state"))
    passes_base = bool(a.get("passes_base"))
    signals = [_s(x) for x in _l(a.get("signals_fired")) if _s(x)]

    blocked = [_s(b) for b in _l(plan.get("blocked")) + _l(company.get("block_reasons")) if _s(b)]
    warnings = [_s(w) for w in _l(company.get("warnings")) if _s(w)]
    positives = [_s(p) for p in _l(company.get("positives")) if _s(p)]

    call = _d(a.get("call") or a.get("open_call"))
    if not call:
        for c in _l(a.get("calls")):
            if _s(_d(c).get("status")).lower() == "open":
                call = _d(c)
                break
    if call and _s(call.get("status")).lower() not in ("", "open"):
        call = {}
    events = _l(a.get("call_events")) or _l(call.get("events"))

    ind = _d(plan.get("indicators"))
    close = _f(plan.get("signal_close")) or _f(plan.get("close"))
    sma20, sma50, sma200 = (_f(ind.get(k)) for k in ("sma20", "sma50", "sma200"))
    hi252 = _f(ind.get("hi252"))
    rsi2 = _f(a.get("rsi2") if a.get("rsi2") is not None else ind.get("rsi2"))
    zone = [x for x in (_f(v) for v in _l(plan.get("buy_zone"))) if x is not None]
    no_chase = _f(plan.get("no_chase_above"))
    stop = _f(plan.get("stop"))
    t1, t2 = _f(plan.get("target1")), _f(plan.get("target2"))
    t1d, t2d = _f(plan.get("t1_days_median")), _f(plan.get("t2_days_median"))

    if t1d and t2d:
        duration = f"about {int(round(t1d))} sessions to T1, {int(round(t2d))} to T2"
    elif t1d:
        duration = f"about {int(round(t1d))} sessions to T1"
    elif t2d:
        duration = f"about {int(round(t2d))} sessions to T2"
    else:
        duration = "no session medians available"

    exit_reason = ""
    for item in events:
        e = _d(item)
        kind = _s(e.get("kind")).lower()
        blob = f"{kind} {e.get('message', '')}".lower()
        if kind in ("exit", "redflag") or ("regime" in blob and "exit" in blob) or "stop-loss" in blob \
                or "trailing stop" in blob:
            exit_reason = _s(e.get("message")) or kind
            break
    call_stop, last = _f(call.get("stop")), _f(call.get("last_price"))
    stop_hit = bool(call) and call_stop is not None and last is not None and last <= call_stop

    if blocked:
        outcome = "AVOID"
        reasons = [_dated(b, as_of) for b in blocked]
    elif call and (exit_reason or stop_hit):
        outcome = "EXIT"
        reason = exit_reason or f"Stop {_money(call_stop)} hit at last price {_money(last)}"
        reasons = [_dated(reason, as_of)]
    elif call:
        outcome = "HOLD"
        reasons = [_dated(f"Open call above stop {_money(call.get('stop'))}, T1 {_money(t1)} not reached", as_of)]
    elif signals and passes_base and regime == "ON" and close is not None and no_chase is not None and close <= no_chase:
        outcome = "BUY"
        names = ", ".join(BY_KEY[s].name if s in BY_KEY else s for s in signals)
        reasons = [_dated(f"{names} fired; buy zone {_money(zone[0])}–{_money(zone[1])}" if len(zone) >= 2
                          else f"{names} fired; buy zone data missing", as_of)]
    elif passes_base:
        outcome = "WAIT"
        missing = []
        if not signals:
            missing.append("no strategy signal today")
        if regime != "ON":
            missing.append(f"regime {regime or 'unknown'}")
        if close is not None and no_chase is not None and close > no_chase:
            missing.append(f"close {_money(close)} above no-chase {_money(no_chase)}")
        zone_text = f"{_money(zone[0])}–{_money(zone[1])}" if len(zone) >= 2 else "data missing"
        reasons = [_dated(f"{', '.join(missing) or 'entry confirmation missing'}; watch buy zone {zone_text}", as_of)]
    else:
        outcome = "AVOID"
        detail = []
        if close is not None and sma50 is not None and close <= sma50:
            detail.append(f"close {_money(close)} not above 50-DMA {_money(sma50)}")
        if close is not None and sma200 is not None and close <= sma200:
            detail.append(f"close {_money(close)} not above 200-DMA {_money(sma200)}")
        if regime != "ON":
            detail.append(f"regime {regime or 'unknown'}")
        failed = detail or ["base trend/liquidity filter did not pass"]
        reasons = [_dated("; ".join(failed), as_of)]

    horizon = "short term" if outcome in ("BUY", "WAIT", "HOLD") else "none"
    if horizon == "none":
        short_line = "short term: no new trade (no setup to time)"
    else:
        short_line = ("short term: open call, " if outcome == "HOLD" else "short term: ") + duration
    long_ok = passes_base and sma200 is not None and close is not None and close > sma200 and not warnings
    long_line = "long term: trend intact, review quarterly" if long_ok else "long term: not a candidate"

    high_distance = None if close is None or hi252 is None or not hi252 else round(100 * (hi252 / close - 1), 2)
    trend = {"close": close, "sma20": sma20, "sma50": sma50, "sma200": sma200, "hi252": hi252, "rsi2": rsi2,
             "above_20dma": None if close is None or sma20 is None else close > sma20,
             "above_50dma": None if close is None or sma50 is None else close > sma50,
             "above_200dma": None if close is None or sma200 is None else close > sma200,
             "sma50_above_sma200": None if sma50 is None or sma200 is None else sma50 > sma200,
             "high_52w_distance_pct": high_distance,
             "text": (f"close {_money(close)} vs 50-DMA {_money(sma50)} / 200-DMA {_money(sma200)} "
                      f"(50-DMA {'above' if sma50 is not None and sma200 is not None and sma50 > sma200 else 'below' if sma50 is not None and sma200 is not None else 'unknown'} 200-DMA); "
                      f"{'above' if trend_above(sma20, close) else 'below' if sma20 is not None and close is not None else 'unknown'} 20-DMA {_money(sma20)}; "
                      f"{'-' if high_distance is None else f'{high_distance:.2f}%'} from 52-week high {_money(hi252)}; "
                      f"RSI(2) {'-' if rsi2 is None else f'{rsi2:.1f}'} as of {as_of}")}

    results = _d(company.get("results"))
    shareholding = _d(company.get("shareholding"))
    current = _d(results.get("current"))
    res_facts = _fact_lines(facts, ("result", "revenue", "pat", "quarter"))
    sh_facts = _fact_lines(facts, ("shareholding", "promoter", "pledge"))
    result_text = (f"Q ended {_s(results.get('quarter_end') or 'date missing')}: revenue {_crore(current.get('revenue'))}, "
                   f"PAT {_crore(current.get('pat'))}, filed {_s(results.get('filed') or 'date missing')}"
                   if current or results else "latest results data missing")
    pledge = shareholding.get("pledge_pct") if shareholding.get("pledge_pct") is not None else None
    share_text = (f"promoter {_s(shareholding.get('promoter_pct', '?'))}% as of {_s(shareholding.get('as_of') or 'date missing')}, "
                  f"pledge {_s(pledge if pledge is not None else shareholding.get('pledge_pct', '?'))}%"
                  if shareholding else "shareholding data missing")
    fundamentals = {"results": results, "shareholding": shareholding, "pledge_pct": pledge,
                    "text": f"Latest results: {result_text}; shareholding: {share_text}"
                            + ("; " + "; ".join(res_facts + sh_facts) if res_facts or sh_facts else "")}

    warn_lines = [_dated(w, as_of) for w in warnings] + _fact_lines(facts, ("warning", "pledge", "surveillance"))
    pos_lines = [_dated(p, as_of) for p in positives] + _fact_lines(facts, ("positive", "order", "contract"))
    news = {"positives": pos_lines, "warnings": warn_lines,
            "text": f"positives {'none' if not pos_lines else ' / '.join(pos_lines)} / "
                    f"warnings {'none' if not warn_lines else ' / '.join(warn_lines)}"}

    data_missing = []
    if not results and not res_facts:
        data_missing.append("latest quarterly results")
    if not shareholding and not sh_facts:
        data_missing.append("shareholding and pledge")
    if not facts:
        data_missing.append("company facts")
    if t1d is None and t2d is None:
        data_missing.append("T1/T2 session medians")
    if not regime:
        data_missing.append("market regime")
    if not ind:
        data_missing.append("price indicators")

    zone_text = f"{_money(zone[0])}–{_money(zone[1])}" if len(zone) >= 2 else "data missing"
    lines = [
        f"{name} ({symbol}) - {outcome}" + (f" · {horizon} ({duration})" if horizon != "none" else " · no trade"),
        f"Buy zone {zone_text} · don't chase above {_money(no_chase)} · stop {_money(stop)} · "
        f"T1 {_money(t1)} · T2 {_money(t2)}",
        "Why: " + " ".join(reasons),
        f"Trend: {trend['text']}",
        f"Fundamentals: {fundamentals['text']}",
        f"News: {news['text']}",
        short_line,
        long_line,
        f"Missing data: {', '.join(data_missing)}" if data_missing else "",
        f"Data: NSE close {as_of or 'unknown'}, fetched {fetched_at} (IST)",
        DISCLAIMER,
    ]
    return {"verdict": outcome, "horizon": horizon, "expected_duration": duration, "reasons": reasons,
            "entry_zone": zone, "stop": stop, "targets": {"t1": t1, "t2": t2},
            "trend": trend, "fundamentals": fundamentals, "news": news, "data_missing": data_missing,
            "as_of": as_of, "fetched_at": fetched_at, "short_term": short_line, "long_term": long_line,
            "text": "\n".join(line for line in lines if line)}


def trend_above(moving_average, close):
    return moving_average is not None and close is not None and close > moving_average
