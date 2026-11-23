"""Company checks: Tier-1 NSE filings (announcements, results XBRL, shareholding, pledge, ASM/GSM,
F&O ban, results calendar) and Tier-2 press headlines (context only).

Every item becomes a confidence-scored Fact with its source link and both dates.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

from . import store
from .confidence import Fact, tier_of, two_date_check
from .config import NEWS_MAX_AGE_DAYS, RESULTS_BLACKOUT_DAYS
from .nse import UA, client

log = logging.getLogger("tt.news")

RED_FLAG_PATTERNS = [
    (r"downgrad", "Credit rating downgrade"),
    (r"\bdefault|delay in (payment|servicing)", "Debt default / payment delay"),
    (r"insolvency|\bnclt\b|\bibc\b|corporate insolvency", "Insolvency proceedings"),
    (r"resignation of (the )?(statutory )?auditor|auditor.{0,40}resign", "Auditor resignation"),
    (r"fraud|forensic audit|siphon|divers(ion|ted) of funds", "Fraud / forensic audit"),
    (r"search and seizure|income tax (search|survey)|\braid\b|enforcement directorate", "Search / raid"),
    (r"show cause|sebi (interim )?order|adjudication order", "Regulatory action"),
    (r"qualified opinion|adverse opinion|disclaimer of opinion", "Audit qualification"),
    (r"resignation of (the )?(chief financial officer|cfo|managing director|ceo)", "Key management exit"),
]
POSITIVE_PATTERNS = [
    (r"award of order|receipt of order|bagging|bags? order|order win|letter of award|receives? order|work order", "Order win"),
    (r"buy ?back", "Buyback"),
    (r"acquisition", "Acquisition"),
]


def _parse_nse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%d-%B-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def classify(text: str) -> tuple[list[str], list[str]]:
    t = text.lower()
    red = [label for pat, label in RED_FLAG_PATTERNS if re.search(pat, t)]
    pos = [label for pat, label in POSITIVE_PATTERNS if re.search(pat, t)]
    return red, pos


# ---------------------------------------------------------------- Tier-1 checks
def announcements(symbol: str, today: date, days: int = 90) -> list[Fact]:
    try:
        url, js = client.announcements(symbol)
    except Exception as e:
        log.warning("announcements %s: %s", symbol, e)
        return []
    out = []
    page = f"https://www.nseindia.com/get-quotes/equity?symbol={quote_plus(symbol)}"
    for a in js if isinstance(js, list) else []:
        d1 = _parse_nse_dt(a.get("an_dt"))
        d2 = _parse_nse_dt(a.get("sort_date")) or _parse_nse_dt(a.get("exchdisstime"))
        if not d1 or (today - d1.date()).days > days or d1.date() > today:
            continue
        text = f"{a.get('desc') or ''}. {a.get('attchmntText') or ''}"
        red, pos = classify(text)
        f = Fact(key=f"ann:{a.get('seq_id')}", label=a.get("desc") or "Announcement",
                 value=(a.get("attchmntText") or "")[:400], url=a.get("attchmntFile") or page,
                 as_of=d1.date().isoformat(), label_date=d2.date().isoformat() if d2 else "",
                 fetched_at=store.now_ist().isoformat(), tier=1,
                 date_check=two_date_check(d1.date(), d2.date() if d2 else None),
                 stale=(today - d1.date()).days > NEWS_MAX_AGE_DAYS,
                 extra={"red_flags": red, "positives": pos, "api": url, "time": a.get("an_dt")})
        out.append(f.finalize())
    out.sort(key=lambda f: f.as_of, reverse=True)
    return out


def _xbrl_val(x: str, tag: str, ctx: str = "OneD") -> str | None:
    m = re.search(r"<in-[\w-]+:" + tag + r"\b[^>]*contextRef=\"" + ctx + r"\"[^>]*>([^<]+)<", x)
    return m.group(1).strip() if m else None


def latest_results(symbol: str) -> dict:
    """Latest quarterly consolidated (fallback standalone) results from NSE integrated filings + YoY."""
    try:
        url, js = client.api(f"integrated-filing-results?index=equities&symbol={quote_plus(symbol)}")
    except Exception as e:
        return {"error": f"results API failed: {e}"}
    fins = [r for r in js.get("data", []) if "Financial" in (r.get("type") or "") and r.get("xbrl")]
    if not fins:
        return {"error": "no integrated financial filings found"}

    def qe(r):
        return datetime.strptime(r["qe_Date"].title(), "%d-%b-%Y").date()

    def pick(q):
        rows = [r for r in fins if qe(r) == q]
        rows.sort(key=lambda r: 0 if r.get("consolidated") == "Consolidated" else 1)
        return rows[0] if rows else None

    latest_q = max(qe(r) for r in fins)
    cur = pick(latest_q)
    prev = pick(latest_q.replace(year=latest_q.year - 1))
    out = {"quarter_end": latest_q.isoformat(), "api": url}

    def parse(r):
        x = client.xbrl(r["xbrl"]).decode("utf8", "ignore")
        rev = _xbrl_val(x, "RevenueFromOperations") or _xbrl_val(x, "InterestEarned") or _xbrl_val(x, "Income")
        pat = _xbrl_val(x, "ProfitLossForPeriod")
        end = _xbrl_val(x, "DateOfEndOfReportingPeriod")
        return {"revenue": float(rev) if rev else None, "pat": float(pat) if pat else None,
                "period_end": end, "filed": r.get("broadcast_Date"), "basis": r.get("consolidated"),
                "url": r["xbrl"]}
    try:
        out["current"] = parse(cur)
        out["date_check"] = two_date_check(latest_q, out["current"]["period_end"])
        if prev:
            out["previous_year"] = parse(prev)
            c, p = out["current"], out["previous_year"]
            if c["revenue"] and p["revenue"]:
                out["revenue_yoy"] = c["revenue"] / p["revenue"] - 1
            if c["pat"] is not None and p["pat"] and p["pat"] > 0:
                out["pat_yoy"] = c["pat"] / p["pat"] - 1
    except Exception as e:
        out["error"] = f"XBRL parse failed: {e}"
    return out


def shareholding(symbol: str) -> dict:
    out = {}
    try:
        url, js = client.api(f"corporate-share-holdings-master?index=equities&symbol={quote_plus(symbol)}")
        rows = sorted(js, key=lambda r: _parse_nse_dt(r.get("date")) or datetime(1900, 1, 1), reverse=True)
        if rows:
            out["promoter_pct"] = float(rows[0]["pr_and_prgrp"]) if rows[0].get("pr_and_prgrp") not in (None, "-", "") else None
            out["as_of"] = rows[0].get("date")
            out["filed"] = rows[0].get("broadcastDate")
            out["url"] = url
            if len(rows) > 1 and rows[1].get("pr_and_prgrp") not in (None, "-", ""):
                out["promoter_prev"] = float(rows[1]["pr_and_prgrp"])
    except Exception as e:
        out["error"] = str(e)
    try:
        url, js = client.pledge(symbol)
        rows = js.get("data", [])
        if rows:
            r = rows[0]
            v = (r.get("percSharesPledged") or r.get("percPromoterShares") or "").strip()
            out["pledge_pct"] = float(v) if v not in ("", "-") else None
            out["pledge_as_of"] = r.get("shp") or r.get("broadcastDt")
            out["pledge_url"] = url
    except Exception as e:
        out["pledge_error"] = str(e)
    return out


def surveillance_lists() -> dict:
    """ASM/GSM/F&O-ban sets for today (cached per day)."""
    today = store.now_ist().date().isoformat()
    cached = store.kv_get("surveillance")
    if cached and cached.get("day") == today:
        return cached
    out = {"day": today, "asm": [], "gsm": [], "ban": [], "urls": {}}
    try:
        url, js = client.asm()
        for part in ("longterm", "shortterm"):
            out["asm"] += [r["symbol"] for r in (js.get(part) or {}).get("data", [])]
        out["urls"]["asm"] = url
    except Exception as e:
        out["asm_error"] = str(e)
    try:
        url, js = client.gsm()
        out["gsm"] = [r["symbol"] for r in js]
        out["urls"]["gsm"] = url
    except Exception as e:
        out["gsm_error"] = str(e)
    d = store.now_ist().date()
    for back in range(0, 5):
        dd = d - timedelta(days=back)
        try:
            f = client.fo_ban(dd)
            out["ban"], out["urls"]["ban"], out["ban_date"] = f.data, f.url, dd.isoformat()
            break
        except Exception:
            continue
    store.kv_set("surveillance", out)
    return out


def results_calendar() -> dict[str, dict]:
    today = store.now_ist().date().isoformat()
    cached = store.kv_get("event_calendar")
    if cached and cached.get("day") == today:
        return cached["events"]
    events: dict[str, dict] = {}
    try:
        url, js = client.event_calendar()
        for e in js:
            d = _parse_nse_dt(e.get("date"))
            if d and "result" in (e.get("purpose") or "").lower():
                events.setdefault(e["symbol"], {"date": d.date().isoformat(), "purpose": e.get("purpose"), "url": url})
    except Exception as e:
        log.warning("event calendar: %s", e)
    store.kv_set("event_calendar", {"day": today, "events": events})
    return events


def results_blackout(symbol: str, today: date) -> dict | None:
    ev = results_calendar().get(symbol)
    if not ev:
        return None
    d = date.fromisoformat(ev["date"])
    if today <= d <= today + timedelta(days=RESULTS_BLACKOUT_DAYS):
        return ev
    return None


# ---------------------------------------------------------------- Tier-2 headlines
def press_headlines(company: str, symbol: str, today: date) -> list[Fact]:
    q = quote_plus(f'"{company}" when:{NEWS_MAX_AGE_DAYS}d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning("rss %s: %s", symbol, e)
        return []
    facts = []
    for it in root.iter("item"):
        src = it.find("source")
        src_url = src.get("url") if src is not None else ""
        tier = tier_of(src_url)
        if tier != 2:  # only reputable press; Tier 3/4 dropped entirely
            continue
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            continue
        title = it.findtext("title") or ""
        red, pos = classify(title)
        f = Fact(key=f"news:{hash(title) & 0xffffffff}", label=src.text if src is not None else "Press",
                 value=title, url=it.findtext("link") or src_url, as_of=pub.date().isoformat(),
                 label_date=pub.date().isoformat(), fetched_at=store.now_ist().isoformat(), tier=2,
                 stale=(today - pub.date()).days > NEWS_MAX_AGE_DAYS, date_check="n/a",
                 extra={"red_flags": red, "positives": pos, "source_site": src_url})
        facts.append(f)
    # confirmation: same red-flag/positive label reported by >1 outlet
    for f in facts:
        labels = set(f.extra["red_flags"] + f.extra["positives"])
        if labels:
            f.confirmations = sum(1 for g in facts if g is not f and labels & set(g.extra["red_flags"] + g.extra["positives"]))
        f.finalize()
    facts.sort(key=lambda f: f.as_of, reverse=True)
    return facts[:15]


# ---------------------------------------------------------------- combined check
def company_check(symbol: str, company: str, today: date, deep: bool = True) -> dict:
    """Run every check for one stock. Returns verdict with facts and reasons."""
    surv = surveillance_lists()
    reasons_block, warnings, positives = [], [], []
    facts: list[dict] = []

    if symbol in surv.get("asm", []):
        reasons_block.append("Under NSE ASM surveillance")
    if symbol in surv.get("gsm", []):
        reasons_block.append("Under NSE GSM surveillance")
    if symbol in surv.get("ban", []):
        reasons_block.append("In F&O ban period")
    bo = results_blackout(symbol, today)
    if bo:
        reasons_block.append(f"Results board meeting on {bo['date']} (within {RESULTS_BLACKOUT_DAYS}-day blackout)")

    anns = announcements(symbol, today)
    for f in anns:
        if f.extra["red_flags"]:
            # a Tier-1 filing in the last 90 days stays valid even if older than the 7-day news window
            if f.tier == 1 and f.date_check != "fail":
                reasons_block.append(f"NSE filing {f.as_of}: {', '.join(f.extra['red_flags'])}")
            else:
                warnings.append(f"Possible {', '.join(f.extra['red_flags'])} ({f.as_of}), date check {f.date_check}")
        if f.extra["positives"] and not f.stale:
            positives.append(f"{', '.join(f.extra['positives'])} (NSE filing {f.as_of})")
    facts += [f.to_dict() for f in anns[:12]]

    res, sh, press = {}, {}, []
    if deep:
        res = latest_results(symbol)
        sh = shareholding(symbol)
        press = press_headlines(company, symbol, today)
        for f in press:
            if f.extra["red_flags"] and f.status in ("fact", "unverified") and not f.stale:
                # asymmetric rule: unverified bad news is enough to skip a buy
                warnings.append(f"Press ({f.label}, {f.as_of}): {f.value[:90]}")
                reasons_block.append(f"Unconfirmed negative press: {', '.join(f.extra['red_flags'])}")
        facts += [f.to_dict() for f in press]
        if sh.get("pledge_pct") and sh["pledge_pct"] > 20:
            reasons_block.append(f"Promoter pledge {sh['pledge_pct']:.1f}% > 20%")
        elif sh.get("pledge_pct") and sh["pledge_pct"] > 5:
            warnings.append(f"Promoter pledge {sh['pledge_pct']:.1f}%")
        if sh.get("promoter_pct") is not None and sh.get("promoter_prev") is not None \
                and sh["promoter_pct"] < sh["promoter_prev"] - 2:
            warnings.append(f"Promoter holding fell {sh['promoter_prev']:.2f}% -> {sh['promoter_pct']:.2f}%")
        if res.get("pat_yoy") is not None and res["pat_yoy"] < -0.25:
            warnings.append(f"Latest quarter PAT down {res['pat_yoy']:.0%} YoY")
        if res.get("current", {}).get("pat") is not None and res["current"]["pat"] < 0:
            warnings.append("Latest quarter is loss-making")

    return {"symbol": symbol, "blocked": bool(reasons_block), "block_reasons": reasons_block,
            "warnings": warnings, "positives": positives, "results": res, "shareholding": sh,
            "facts": facts, "surveillance_urls": surv.get("urls", {}), "results_event": bo}
