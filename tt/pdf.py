"""PDF report for a pick / call / stock analysis, with a full source table."""
from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import store
from .config import REPORTS_DIR

ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=16, spaceAfter=4)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, spaceBefore=8, spaceAfter=3)
P = ParagraphStyle("p", parent=ss["BodyText"], fontSize=8.8, leading=11.5)
S = ParagraphStyle("s", parent=P, fontSize=7.2, leading=9, textColor=colors.HexColor("#555555"))


def _p(t, st=P):
    return Paragraph(escape(str(t)).replace("\n", "<br/>"), st)


def _link(url, text=None):
    if not url:
        return _p("-", S)
    return Paragraph(f'<link href="{escape(url)}" color="blue">{escape(text or url[:70])}</link>', S)


def _table(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
          ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 8)]
    if header:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")))
    t.setStyle(TableStyle(st))
    return t


def _pct(x):
    return "-" if x is None else f"{x * 100:.1f}%"


def plan_pdf(plan: dict, scan: dict | None = None, title: str | None = None) -> str:
    sym = plan["symbol"]
    fname = f"{sym}_{plan.get('data_date', 'na')}_{store.now_ist():%Y%m%d%H%M%S}.pdf"
    path = REPORTS_DIR / fname
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=title or f"{sym} trade plan")
    el = [_p(title or f"{plan['name']} ({sym}) - trade plan", H1),
          _p(f"Data date (NSE close): {plan.get('data_date')} | Generated: {store.now_ist():%d-%b-%Y %H:%M} IST | "
             f"Strategy: {plan['strategy_name']} ({plan['strategy']})", S),
          _p("Analysis from published rules and NSE data, not investment advice. Paper-trade first. You decide.", S)]

    if scan and scan.get("regime"):
        r = scan["regime"]
        el += [_p("1. Market regime", H2),
               _p(f"{r['state']} on {r['date']}: Nifty 500 {r['index']:.2f} vs 200-DMA {r['index_200dma']:.2f}; "
                  f"breadth {r['breadth']:.0%} of Nifty 500 stocks above their own 200-DMA. Position size x{r['size_mult']}.")]

    el += [_p("2. Why this stock", H2)] + [_p("- " + w) for w in plan.get("why", [])]
    pos = plan["position"]
    rows = [["Item", "Value"],
            ["Buy zone", f"Rs {plan['buy_zone'][0]} - {plan['buy_zone'][1]}"],
            ["Do not chase above", f"Rs {plan['no_chase_above']}"],
            ["Stop-loss", f"Rs {plan['stop']}"],
            ["Stop reason", _p(plan["stop_reason"], S)],
            ["Target 1", f"Rs {plan['target1']} (R:R 1:{plan['rr_t1']}); backtest median {plan.get('t1_days_median') or '-'} "
                         f"sessions (75th pct {plan.get('t1_days_p75') or '-'}), hit in {_pct(plan.get('t1_hit_rate'))} of trades"],
            ["Target 2", f"Rs {plan['target2']} (R:R 1:{plan['rr_t2']}); backtest median {plan.get('t2_days_median') or '-'} "
                         f"sessions, hit in {_pct(plan.get('t2_hit_rate'))} of trades"],
            ["Expected holding", f"~{plan.get('expected_hold_days') or '-'} sessions (backtest average); time exit day {plan['time_exit_day']}"],
            ["Position", f"{pos['qty']} shares = Rs {pos['value']:,} ({pos['pct_capital']}% of Rs {pos['capital']:,.0f}); "
                         f"max loss Rs {pos['max_loss']:,} ({pos['max_loss_pct']}%); est. charges Rs {pos['round_trip_charges_est']}"]]
    el += [_p("3. Trade plan", H2), _table(rows, [40 * mm, 140 * mm])]
    el += [_p("4. If it goes up", H2)] + [_p("- " + x) for x in plan["if_up"]]
    el += [_p("5. If it goes down", H2)] + [_p("- " + x) for x in plan["if_down"]]

    comp = plan.get("company", {})
    res = comp.get("results", {}) or {}
    sh = comp.get("shareholding", {}) or {}
    crow = [["Check", "Value", "Source / date"]]
    cur = res.get("current") or {}
    if cur:
        crow.append(["Latest quarter", f"Q ended {res.get('quarter_end')}: revenue Rs {cur['revenue'] / 1e7:,.0f} cr, "
                     f"PAT Rs {cur['pat'] / 1e7:,.0f} cr ({cur.get('basis')})" if cur.get("revenue") and cur.get("pat") is not None else "parse incomplete",
                     _link(cur.get("url"), f"NSE XBRL filed {cur.get('filed')}")])
        crow.append(["YoY growth", f"Revenue {_pct(res.get('revenue_yoy'))}, PAT {_pct(res.get('pat_yoy'))}",
                     _p(f"Two-date check: {res.get('date_check')}", S)])
    elif res.get("error"):
        crow.append(["Latest quarter", "DATA MISSING: " + res["error"], "-"])
    if sh:
        crow.append(["Promoter holding", f"{sh.get('promoter_pct')}% (prev {sh.get('promoter_prev')}%) as of {sh.get('as_of')}",
                     _link(sh.get("url"), "NSE shareholding")])
        pledge = (f"{sh['pledge_pct']}% as of {sh.get('pledge_as_of')}" if sh.get("pledge_pct") is not None
                  else "DATA MISSING (no pledge disclosure returned by NSE)")
        crow.append(["Promoter pledge", pledge, _link(sh.get("pledge_url"), "NSE pledge data")])
    fo = plan.get("fo") or {}
    if fo.get("in_fo"):
        crow.append(["F&O context (untested)", f"{fo.get('buildup', '-')}; max call OI strike {fo.get('max_call_oi_strike')}, "
                     f"max put OI strike {fo.get('max_put_oi_strike')} (expiry {fo.get('expiry')})", _p("NSE F&O bhavcopy", S)])
    ex = plan.get("expiry") or {}
    crow.append(["Next monthly expiry", f"{ex.get('next_monthly')} ({ex.get('trading_days_to')} sessions)", _p("Rule: last Tuesday, holiday-adjusted", S)])
    for w in comp.get("warnings", []):
        crow.append(["Warning", _p(w, S), "-"])
    for w in comp.get("positives", []):
        crow.append(["Positive (info only)", _p(w, S), "-"])
    el += [_p("6. Company checks", H2), _table(crow, [35 * mm, 95 * mm, 50 * mm])]

    st = (scan or {}).get("strategy", {})
    if st.get("stats"):
        s_ = st["stats"]
        rc = st.get("recent", {})
        er = st.get("early", {})
        brow = [["Metric", "Full", "2021-2023", "2024-now"]]
        for k, lab, f in [("trades", "Trades", str), ("win_rate", "Win rate", _pct), ("avg_win", "Avg win", _pct),
                          ("avg_loss", "Avg loss", _pct), ("expectancy", "Expectancy / trade", _pct),
                          ("max_drawdown", "Max drawdown", _pct), ("cagr_after_cost_tax", "CAGR after cost+tax", _pct),
                          ("bench_cagr", "Nifty 500 CAGR (price)", _pct), ("avg_hold_days", "Avg hold (sessions)", lambda x: f"{x:.0f}")]:
            def g(m):
                v = m.get(k)
                return f(v) if v is not None else "-"
            brow.append([lab, g(s_), g(er), g(rc)])
        el += [_p("7. Backtest of this strategy (NSE data from 2020, costs + 20% STCG)", H2), _table(brow, [55 * mm, 40 * mm, 40 * mm, 40 * mm]),
               _p("Limits: universe is today's Nifty 500 (survivorship bias); no 2008-type crash in the sample; "
                  "benchmark is the price index (no dividends); averaging rule not tested.", S)]

    srow = [["Fact", "Value", "As of", "Fetched", "Conf.", "Status", "Source"]]
    for f in (scan or {}).get("market_facts", []) + plan.get("company_facts", []):
        srow.append([_p(f["label"][:40], S), _p(str(f["value"])[:120], S), _p(f["as_of"], S), _p(f["fetched_at"][:16], S),
                     _p(f"{f['score']:.2f}", S), _p(f"{f['status']} / dates {f['date_check']}", S), _link(f["url"], "link")])
    for k, u in (plan.get("surveillance_urls") or {}).items():
        srow.append([_p(f"{k.upper()} list", S), _p("checked: not listed", S), _p(store.now_ist().date().isoformat(), S), "-", "1.00", "fact", _link(u, "link")])
    el += [_p("8. Sources (Tier 1 = official, Tier 2 = press; facts below 0.85 are not used for decisions)", H2),
           _table(srow, [30 * mm, 55 * mm, 17 * mm, 22 * mm, 10 * mm, 25 * mm, 18 * mm])]
    doc.build(el)
    return fname
