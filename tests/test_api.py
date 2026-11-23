"""End-to-end API + tracker tests on a synthetic, offline database (run: python -m pytest -q tests).

Nothing here touches the network or the real data/ folder:
  - TT_DATA_DIR points at a fresh temp dir before anything from tt is imported;
  - every NSE client method, news fetcher, holiday/F&O/market-fact lookup and the LLM are stubbed;
  - a socket guard refuses any non-loopback connection as a last line of defence.
"""
import math
import os
import socket
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

_REPO_DATA = (Path(__file__).resolve().parent.parent / "data").resolve()
if "tt.config" not in sys.modules:
    os.environ["TT_DATA_DIR"] = tempfile.mkdtemp(prefix="tt_api_test_")
os.environ["TT_NO_SCHEDULER"] = "1"
os.environ["TT_LLM_PROVIDER"] = ""

from tt import config  # noqa: E402

_DATA = Path(config.DATA_DIR).resolve()
if _DATA == _REPO_DATA or not str(_DATA).lower().startswith(str(Path(tempfile.gettempdir()).resolve()).lower()):
    pytest.skip(f"tt.config.DATA_DIR={_DATA} is not a temp dir; refusing to run API tests", allow_module_level=True)

import pandas as pd  # noqa: E402
import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tt import costs, llm, news, nse, signals, store, tracker, tradeplan  # noqa: E402
from tt import pdf as pdfmod  # noqa: E402
from tt.app import app  # noqa: E402
from tt.confidence import Fact  # noqa: E402

# ---------------------------------------------------------------- synthetic market
HOLIDAYS = {date(2026, 10, 2), date(2026, 11, 24)}
END = date(2026, 10, 30)                    # last seeded session (a Friday, also a month end)
FROZEN_NOW = datetime(2026, 10, 30, 19, 0, tzinfo=config.IST)   # after the 18:00 bhavcopy cut-off
N_SESSIONS = 300
SYMS = {  # symbol: (name, industry, close on END)
    "AAA": ("Alpha Tech Ltd", "Information Technology", 100.0),
    "BBB": ("Beta Software Ltd", "Information Technology", 200.0),
    "CCC": ("Gamma Bank Ltd", "Financial Services", 300.0),
    "DDD": ("Delta Finance Ltd", "Financial Services", 150.0),
    "EEE": ("Epsilon Pharma Ltd", "Healthcare", 80.0),   # breaks out on END -> today's S2 pick
}
BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{:%d%m%Y}.csv"
INDEX_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{:%d%m%Y}.csv"


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*a, **k):
    raise NetworkBlocked("network access is disabled in tests")


def _is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def _sessions_ending(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if _is_session(d):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _price_row(sym, d, o, h, l, c, prev, vol):
    return (sym, d.isoformat(), "EQ", o, h, l, c, prev, vol, round(c * vol / 1e5, 2), vol / 500, vol * 0.5, 50.0)


def _log_rows(d: date, n_rows: int):
    ts = FROZEN_NOW.isoformat()
    return [("bhav", d.isoformat(), BHAV_URL.format(d), d.isoformat(), d.isoformat(), ts, "ok", n_rows, ""),
            ("index", d.isoformat(), INDEX_URL.format(d), d.isoformat(), d.isoformat(), ts, "ok", 1, "")]


def _seed():
    days = _sessions_ending(END, N_SESSIONS)
    prices, idx, logs = [], [], []
    for sym, (_name, _ind, last) in SYMS.items():
        prev = None
        for k, d in enumerate(days):
            t = k - (N_SESSIONS - 1)
            c = last * math.exp(0.0015 * t) * (1 + 0.005 * math.sin(t / 3))
            vol = 2e6
            if sym == "EEE" and t == 0:  # 52-week-high breakout on 3x volume, green candle
                o, c, vol = prev * 1.002, prev * 1.05, 6e6
                h, l = c * 1.005, o * 0.995
            else:
                o = prev if prev is not None else c
                h, l = max(o, c) * 1.01, min(o, c) * 0.99
            o, h, l, c = (round(x, 2) for x in (o, h, l, c))
            prices.append(_price_row(sym, d, o, h, l, c, prev if prev is not None else c, vol))
            prev = c
    for k, d in enumerate(days):
        ix = round(20000 * math.exp(0.0008 * (k - N_SESSIONS + 1)), 2)
        idx.append(("Nifty 500", d.isoformat(), ix, ix, ix, ix, None, None, None))
        logs += _log_rows(d, len(SYMS))
    # a rejected file (two-date check failed) to show up in /api/sources
    logs.append(("bhav", "2026-10-02", BHAV_URL.format(date(2026, 10, 2)), "2026-10-02", "2026-10-01",
                 FROZEN_NOW.isoformat(), "date_mismatch", 0, "content dates [2026-10-01]"))
    with store.tx() as c:
        c.executemany("INSERT INTO universe VALUES (?,?,?,?,?,?)",
                      [(s, n, i, f"INE{s}01010", "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
                        FROZEN_NOW.isoformat()) for s, (n, i, _) in SYMS.items()])
        c.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", prices)
        c.executemany("INSERT INTO index_prices VALUES (?,?,?,?,?,?,?,?,?)", idx)
        c.executemany("INSERT OR REPLACE INTO sync_log VALUES (?,?,?,?,?,?,?,?,?)", logs)
    store.kv_set("universe_meta", {"url": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
                                   "fetched_at": FROZEN_NOW.isoformat(), "count": len(SYMS)})


def _last_session() -> str:
    return store.q1("SELECT MAX(date) d FROM prices")["d"]


def _last_close(sym: str) -> float:
    return store.q1("SELECT close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,))["close"]


def add_session(bars: dict | None = None) -> str:
    """Append the next trading session for every symbol (default: +0.1% drift) plus index and sync_log rows."""
    bars = bars or {}
    d = date.fromisoformat(_last_session()) + timedelta(days=1)
    while not _is_session(d):
        d += timedelta(days=1)
    rows = []
    for sym in SYMS:
        prev = _last_close(sym)
        if sym in bars:
            o, h, l, c = bars[sym]
        else:
            c = prev * 1.001
            o, h, l = prev, c * 1.01, prev * 0.99
        o, h, l, c = (round(x, 2) for x in (o, h, l, c))
        assert l <= min(o, c) and h >= max(o, c), (sym, o, h, l, c)
        rows.append(_price_row(sym, d, o, h, l, c, prev, 2e6))
    ix = round(store.q1("SELECT close FROM index_prices WHERE name='Nifty 500' ORDER BY date DESC LIMIT 1")["close"] * 1.001, 2)
    with store.tx() as cx:
        cx.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        cx.execute("INSERT INTO index_prices VALUES (?,?,?,?,?,?,?,?,?)", ("Nifty 500", d.isoformat(), ix, ix, ix, ix, None, None, None))
        cx.executemany("INSERT OR REPLACE INTO sync_log VALUES (?,?,?,?,?,?,?,?,?)", _log_rows(d, len(rows)))
    return d.isoformat()


def make_plan(sym: str, atr: float, capital: float = 500_000) -> dict:
    close = _last_close(sym)
    row = {"close": close, "atr": atr, "sma20": close * 0.97, "sma50": close * 0.93, "sma200": close * 0.85,
           "lo20": close * 0.95}
    plan = tradeplan.build(sym, SYMS[sym][0], row, {}, capital, 1.0, "S2", "52-week-high breakout")
    plan["data_date"] = _last_session()
    plan["industry"] = SYMS[sym][1]
    return plan


def _call(cid: int) -> dict:
    return store.q1("SELECT * FROM calls WHERE id=?", (cid,))


def _event_kinds(cid: int) -> list[str]:
    return [e["kind"] for e in store.q("SELECT kind FROM call_events WHERE call_id=? ORDER BY id", (cid,))]


# ---------------------------------------------------------------- fixtures
def _fake_company_check(symbol, company, today, deep=True):
    return {"symbol": symbol, "blocked": False, "block_reasons": [], "warnings": [], "positives": [],
            "results": {}, "shareholding": {}, "facts": [], "surveillance_urls": {}, "results_event": None}


def _fake_cross_check_source(d: date):
    """Second-source (UDiFF) price file for d, built from the seeded bhavcopy closes (agrees exactly)."""
    rows = store.q("SELECT symbol, series, close FROM prices WHERE date=?", (d.isoformat(),))
    df = pd.DataFrame({"TckrSymb": [r["symbol"] for r in rows], "SctySrs": [r["series"] for r in rows],
                       "ClsPric": [r["close"] for r in rows]})
    url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    return nse.Fetched(url, FROZEN_NOW.replace(tzinfo=None), d, d, df)


@pytest.fixture(scope="module", autouse=True)
def offline():
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _local_only(fn):
        def guard(self, address, *a, **k):
            host = address[0] if isinstance(address, tuple) else address
            if host not in ("127.0.0.1", "::1", "localhost"):
                raise NetworkBlocked(f"connect to {address!r} blocked in tests")
            return fn(self, address, *a, **k)
        return guard

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", _local_only(real_connect))
        mp.setattr(socket.socket, "connect_ex", _local_only(real_connect_ex))
        mp.setattr(requests.sessions.Session, "request", _blocked)
        # every NSEClient method (present and future) raises instead of fetching
        for name in dir(nse.NSEClient):
            if not name.startswith("__") and callable(getattr(nse.NSEClient, name)):
                mp.setattr(nse.client, name, _blocked)
        if hasattr(signals, "cross_check_source"):
            mp.setattr(signals, "cross_check_source", _fake_cross_check_source)
        mp.setattr(news, "announcements", lambda symbol, today, days=90: [])
        mp.setattr(news, "company_check", _fake_company_check)
        mp.setattr(news, "surveillance_lists", lambda: {"day": FROZEN_NOW.date().isoformat(), "asm": [], "gsm": [],
                                                        "ban": [], "urls": {}})
        mp.setattr(news, "results_calendar", lambda: {})
        mp.setattr(news, "press_headlines", lambda company, symbol, today: [])
        mp.setattr(news, "latest_results", lambda symbol: {})
        mp.setattr(news, "shareholding", lambda symbol: {})
        mp.setattr(signals, "holidays", lambda: set(HOLIDAYS))
        mp.setattr(signals, "fo_context", lambda symbols, d: {})
        mp.setattr(signals, "market_facts", lambda fr, reg_row, d: [])
        mp.setattr(llm, "enabled", lambda: False)
        mp.setattr(store, "now_ist", lambda: FROZEN_NOW)
        _seed()
        scan = signals.run_scan(create_call=False)
        yield {"scan": scan}


@pytest.fixture(scope="module")
def api():
    return TestClient(app)


# ---------------------------------------------------------------- offline guard + scan
def test_network_is_blocked():
    with pytest.raises(NetworkBlocked):
        nse.client.bhavcopy(END)
    with pytest.raises(NetworkBlocked):
        requests.get("https://www.nseindia.com")
    assert Path(config.DB_PATH).resolve().parent == _DATA


def test_daily_scan_on_synthetic_data(offline, api):
    sc = offline["scan"]
    assert sc["freshness"]["fresh"] is True and sc["freshness"]["expected"] == END.isoformat()
    assert sc["regime"]["state"] == "ON" and sc["regime"]["date"] == END.isoformat()
    assert sc["status"] == "pick", (sc.get("message"), sc.get("checked"), sc.get("signals_by_strategy"))
    pick = sc["pick"]
    assert pick["symbol"] == "EEE" and pick["data_date"] == END.isoformat()
    assert pick["stop"] < pick["buy_zone"][0] < pick["buy_zone"][1] < pick["no_chase_above"] < pick["target1"] < pick["target2"]
    assert pick["expiry"]["next_monthly"] == "2026-11-23"   # last Tuesday 24 Nov is a holiday
    assert sc["checked"] == [{"symbol": "EEE", "name": "Epsilon Pharma Ltd", "blocked": False, "block_reasons": [],
                              "warnings": []}]
    r = api.get("/api/today").json()
    assert r["status"] == "pick" and r["pick"]["symbol"] == "EEE"
    scans = api.get("/api/scans").json()
    assert scans[0]["data_date"] == END.isoformat() and scans[0]["status"] == "pick"


# ---------------------------------------------------------------- read-only endpoints
def test_status(api):
    r = api.get("/api/status")
    assert r.status_code == 200
    s = r.json()
    latest = store.q1("SELECT MAX(date) d FROM sync_log WHERE kind='bhav' AND status='ok'")["d"]
    assert s["latest_data"] == latest
    assert s["expected_data"] == END.isoformat()
    assert s["fresh"] == (latest == END.isoformat())
    assert s["capital"] == config.DEFAULT_CAPITAL
    assert s["active_strategy"] == "S2"
    assert s["llm"] is False
    assert s["universe"]["count"] == len(SYMS)
    assert s["now_ist"].startswith("2026-10-30T19:00")


def test_sources(api):
    s = api.get("/api/sources").json()
    summ = {(r["kind"], r["status"]): r for r in s["summary"]}
    assert summ[("bhav", "ok")]["n"] >= N_SESSIONS and summ[("index", "ok")]["n"] >= N_SESSIONS
    assert summ[("bhav", "ok")]["first"] == _sessions_ending(END, N_SESSIONS)[0].isoformat()
    assert len(s["recent"]) == 30
    assert [r["date"] for r in s["rejected"]] == ["2026-10-02"]
    assert s["universe"]["count"] == len(SYMS)


def test_search(api):
    assert api.get("/api/search", params={"q": "alpha"}).json() == [{"symbol": "AAA", "name": "Alpha Tech Ltd"}]
    assert [r["symbol"] for r in api.get("/api/search", params={"q": "ccc"}).json()] == ["CCC"]
    found = [r["symbol"] for r in api.get("/api/search", params={"q": "ltd"}).json()]
    assert [s for s in found if s in SYMS] == sorted(SYMS) and found == sorted(found)   # (other test modules may add rows)
    assert api.get("/api/search", params={"q": "zzzz"}).json() == []


def test_watchlist_add_list_delete(api):
    assert api.post("/api/watchlist", json={"symbol": "aaa", "note": "breakout soon"}).json() == {"symbol": "AAA"}
    assert api.post("/api/watchlist", json={"symbol": "Gamma"}).json() == {"symbol": "CCC"}   # resolved by name
    assert api.post("/api/watchlist", json={"symbol": "NOSUCHSTOCK"}).status_code == 404
    w = api.get("/api/watchlist").json()
    rows = {r["symbol"]: r for r in w["manual"]}
    assert set(rows) >= {"AAA", "CCC"}
    a = rows["AAA"]
    assert a["note"] == "breakout soon" and a["name"] == "Alpha Tech Ltd" and a["industry"] == "Information Technology"
    assert a["close"] == _last_close("AAA") and a["date"] == _last_session()
    prev2 = store.q("SELECT close FROM prices WHERE symbol='AAA' ORDER BY date DESC LIMIT 2")
    assert a["chg"] == pytest.approx(prev2[0]["close"] / prev2[1]["close"] - 1)
    assert isinstance(w["upcoming"], list) and isinstance(w["to_average"], list)
    assert "EEE" in w["signals"]["S2"] and w["data_date"] == END.isoformat()
    assert api.delete("/api/watchlist/aaa").json() == {"ok": True}
    assert api.delete("/api/watchlist/CCC").json() == {"ok": True}
    assert {r["symbol"] for r in api.get("/api/watchlist").json()["manual"]}.isdisjoint({"AAA", "CCC"})


def test_summary_shape(api):
    s = api.get("/api/summary").json()
    assert {"closed", "open", "planned"} <= set(s)
    assert all(isinstance(s[k], int) for k in ("closed", "open", "planned"))


# ---------------------------------------------------------------- chat
def _chat(api, msg: str) -> dict:
    r = api.post("/api/chat", json={"message": msg})
    assert r.status_code == 200
    out = r.json()
    assert not out["text"].startswith("Error:"), out["text"]
    return out


def test_chat_commands(api):
    assert "Things you can ask" in _chat(api, "help")["text"]
    h = _chat(api, "holdings")["text"]
    assert h == "No open or planned calls." or h.startswith("#")
    reg = _chat(api, "regime")["text"]
    assert reg.startswith(f"Regime ON on {END.isoformat()}") and "breadth 100%" in reg
    assert _chat(api, "watch BBB")["text"] == "Added BBB."
    assert "BBB" in {r["symbol"] for r in api.get("/api/watchlist").json()["manual"]}
    assert _chat(api, "unwatch beta software")["text"] == "Removed BBB."
    assert _chat(api, "watch NOSUCHSTOCK")["text"] == "Stock not found."
    avg = _chat(api, "average")["text"]
    assert avg == "No open position is more than 5% below entry." or "->" in avg
    assert "Epsilon Pharma Ltd (EEE)" in _chat(api, "today")["text"]
    assert _chat(api, "strategy")["text"] == "No backtest yet."
    assert _chat(api, "upcoming")["text"]
    assert "didn't recognise" in _chat(api, "what is the meaning of life")["text"]
    assert _chat(api, "bought 99999 at 100")["text"].startswith("Not done: call not found")
    hist = api.get("/api/chat").json()
    assert hist[-1]["role"] == "assistant" and hist[-2]["role"] == "user"
    assert hist[-2]["text"] == "bought 99999 at 100"


def test_chat_analyse_and_pdf(api):
    a = _chat(api, "analyse alpha")
    assert a["analysis"]["symbol"] == "AAA" and a["text"].startswith("Alpha Tech Ltd (AAA) - ")
    p = _chat(api, "pdf AAA")
    assert p["pdf"] and (config.REPORTS_DIR / p["pdf"]).exists()
    t = _chat(api, "pdf today")
    assert t["text"] == "PDF ready for EEE." and (config.REPORTS_DIR / t["pdf"]).read_bytes()[:4] == b"%PDF"


# ---------------------------------------------------------------- tracker
def test_tracker_fill_target1_breakeven_then_stop(api):
    sig = _last_session()
    plan = make_plan("AAA", atr=2.0)
    qty = plan["position"]["qty"]
    stop0, t1 = plan["stop"], plan["target1"]
    entry = plan["signal_close"]
    assert qty > 0 and plan["buy_zone"][0] <= entry <= plan["buy_zone"][1] < plan["no_chase_above"]
    cid = tracker.create_call(plan, sig)
    assert cid and tracker.create_call(plan, sig) is None   # only one active call per symbol
    assert _call(cid)["status"] == "planned"

    # (a) next session opens inside the buy zone -> paper fill at the open
    d1 = add_session({"AAA": (entry, entry + 1, entry - 1, entry + 0.5)})
    assert tracker.update_all(red_flag_check=False)["data_date"] == d1
    c = _call(cid)
    assert (c["status"], c["qty"], c["qty_open"], c["entry_price"], c["entry_date"]) == ("open", qty, qty, entry, d1)
    assert c["stop"] == stop0 and c["t1_done"] == 0 and c["last_date"] == d1 and c["last_price"] == entry + 0.5
    assert c["charges"] == pytest.approx(costs.buy_charges(entry * qty))

    # (b) high reaches Target 1 -> book 1/3 at T1, stop to breakeven
    c1 = entry + 0.5
    d2 = add_session({"AAA": (c1 + 0.5, t1 + 0.5, c1, t1 - 1.5)})
    tracker.update_all(red_flag_check=False)
    booked = max(1, int(round(qty * config.T1_BOOK_FRACTION)))
    c = _call(cid)
    assert c["status"] == "open" and c["t1_done"] == 1
    assert c["qty_open"] == qty - booked
    assert c["stop"] == entry                      # breakeven, not lowered, trail still below entry
    assert c["realized"] == pytest.approx(booked * t1)
    assert _event_kinds(cid) == ["plan", "fill", "target"]
    ev = store.q1("SELECT * FROM call_events WHERE call_id=? AND kind='target'", (cid,))
    assert ev["date"] == d2 and ev["qty"] == booked and ev["price"] == t1

    # (c) later bar trades through the breakeven stop -> rest sold at the stop, call closed
    d3 = add_session({"AAA": (t1 - 2.5, t1 - 2, entry - 0.5, entry + 0.2)})
    tracker.update_all(red_flag_check=False)
    c = _call(cid)
    rest = qty - booked
    realized = booked * t1 + rest * entry
    charges = costs.buy_charges(entry * qty) + costs.sell_charges(booked * t1) + costs.sell_charges(rest * entry)
    assert (c["status"], c["qty_open"], c["exit_date"], c["exit_reason"]) == ("closed", 0, d3, "Trailing stop")
    assert c["exit_price"] == round(realized / qty, 2)
    assert c["realized"] == pytest.approx(realized) and c["charges"] == pytest.approx(charges)
    assert _event_kinds(cid) == ["plan", "fill", "target", "exit"]
    assert store.q1("SELECT qty, price FROM call_events WHERE call_id=? AND kind='exit'", (cid,)) == {"qty": rest, "price": entry}

    # a closed call is not touched again
    before = _call(cid)
    add_session()
    tracker.update_all(red_flag_check=False)
    assert _call(cid) == before and _event_kinds(cid)[-1] == "exit"

    # API views agree with the database
    pnl = round(realized - entry * qty - charges, 2)
    one = api.get(f"/api/calls/{cid}").json()
    assert one["status"] == "closed" and one["qty"] == qty and one["qty_open"] == 0
    assert one["plan"]["stop"] == stop0 and one["plan"]["target1"] == t1
    assert [e["kind"] for e in one["events"]] == ["plan", "fill", "target", "exit"]
    assert [e["date"] for e in one["events"]] == [sig, d1, d2, d3]
    listed = {r["id"]: r for r in api.get("/api/calls", params={"status": "closed"}).json()}
    r = listed[cid]
    assert "plan" not in r and r["plan_summary"]["target1"] == t1 and r["plan_summary"]["buy_zone"] == plan["buy_zone"]
    assert r["pnl"] == pnl and r["ret"] == pytest.approx(pnl / (entry * qty))
    assert r["realized"] == pytest.approx(one["realized"]) and r["charges"] == pytest.approx(one["charges"])
    assert cid in {x["id"] for x in api.get("/api/calls").json()}
    assert cid not in {x["id"] for x in api.get("/api/calls", params={"status": "open,planned"}).json()}
    s = api.get("/api/summary").json()
    closed = store.q("SELECT * FROM calls WHERE status='closed'")
    assert s["closed"] == len(closed)
    assert s["total_pnl"] == pytest.approx(sum(x["realized"] - x["entry_price"] * x["qty"] - x["charges"] for x in closed))
    assert pnl > 0


def test_plan_expires_after_three_sessions_and_limit_fill():
    sig = _last_session()
    b = make_plan("BBB", atr=4.0)
    e = make_plan("EEE", atr=3.0)
    bid, eid = tracker.create_call(b, sig), tracker.create_call(e, sig)
    bc, nc, hi = b["signal_close"], b["no_chase_above"], b["buy_zone"][1]
    ec = e["signal_close"]
    kinds = []
    for k in range(1, 4):
        # BBB gaps above no-chase and never trades back into the buy zone
        bars = {"BBB": (nc + 2 * k, nc + 2 * k + 1, hi + 1 + k, nc + 2 * k + 0.5)}
        if k == 1:  # EEE opens above no-chase but dips into the zone -> limit fill at buy-zone high
            bars["EEE"] = (e["no_chase_above"] + 1, e["no_chase_above"] + 2, e["buy_zone"][1] - 0.2, ec + 1)
        d = add_session(bars)
        tracker.update_all(red_flag_check=False)
        c = _call(bid)
        kinds.append(c["status"])
        if k < 3:
            assert c["status"] == "planned" and c["last_date"] == d and c["qty"] is None
    assert kinds == ["planned", "planned", "expired"]
    c = _call(bid)
    assert c["exit_reason"] == "not filled within 3 sessions" and c["qty"] is None
    assert _event_kinds(bid) == ["plan", "expired"]
    assert store.q1("SELECT date FROM call_events WHERE call_id=? AND kind='expired'", (bid,))["date"] == d
    assert bc < nc
    # expired call stays expired
    add_session()
    tracker.update_all(red_flag_check=False)
    assert _call(bid)["status"] == "expired" and _event_kinds(bid) == ["plan", "expired"]

    f = _call(eid)
    assert f["status"] == "open" and f["entry_price"] == e["buy_zone"][1] and f["qty"] == e["position"]["qty"]
    tracker.manual(eid, "sell", _last_close("EEE"), d=_last_session())
    assert _call(eid)["status"] == "closed"


def test_manual_actions(api):
    while date.fromisoformat(_last_session()).weekday() != 4:   # signal on a Friday so the next session is after Saturday
        add_session()
    plan = make_plan("CCC", atr=10.0)
    sig = _last_session()
    cid = tracker.create_call(plan, sig)
    qty0, px = plan["position"]["qty"], plan["signal_close"]
    act = lambda **kw: api.post(f"/api/calls/{cid}/action", json=kw)  # noqa: E731

    assert act(action="sell", price=px).status_code == 400            # not open yet
    r = act(action="buy")                                              # no price -> must not fill at Rs 0
    assert r.status_code == 400 and "price" in r.json()["detail"]
    assert act(action="buy", price=px, qty=-5).status_code == 400
    assert _call(cid)["status"] == "planned"

    # bought on a Saturday (no session that day)
    sat = date.fromisoformat(sig) + timedelta(days=1)
    while sat.weekday() != 5:
        sat += timedelta(days=1)
    r = act(action="buy", price=px, date=sat.isoformat())
    assert r.status_code == 200
    c = r.json()
    assert (c["status"], c["qty"], c["qty_open"], c["entry_price"], c["entry_date"]) == ("open", qty0, qty0, px, sat.isoformat())
    assert act(action="buy", price=px).status_code == 400              # already bought
    assert act(action="cancel").status_code == 400                      # cannot cancel an open position

    # average once (capped at 10% of capital), never twice
    p2 = round(px * 0.97, 2)
    want = min(100, qty0)
    cap = config.MAX_POSITION_PCT * plan["position"]["capital"]
    if (qty0 + want) * p2 > cap:
        want = int(cap / p2) - qty0
    assert want > 0
    r = act(action="average", price=p2, qty=100, date=sat.isoformat())
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["averaged"] == 1 and c["qty"] == qty0 + want and c["qty_open"] == qty0 + want
    assert c["entry_price"] == round((qty0 * px + want * p2) / (qty0 + want), 2)
    assert c["qty_open"] * p2 <= cap
    r = act(action="average", price=p2, qty=1)
    assert r.status_code == 400 and "already averaged" in r.json()["detail"]

    # the next session must only process bars after the (non-session) manual entry date
    d = add_session()
    tracker.update_all(red_flag_check=False)
    c = _call(cid)
    assert c["status"] == "open" and c["last_date"] == d and c["qty_open"] == qty0 + want
    assert _event_kinds(cid) == ["plan", "fill", "average"]

    # partial then full manual exit
    total = c["qty"]
    r = act(action="sell", price=px + 5, qty=50, date=d)
    assert r.status_code == 200 and r.json()["qty_open"] == total - 50 and r.json()["status"] == "open"
    row = {x["id"]: x for x in api.get("/api/calls", params={"status": "open"}).json()}[cid]
    entry = row["entry_price"]
    assert row["unrealized"] == round((row["last_price"] - entry) * row["qty_open"] + row["realized"]
                                      - entry * (row["qty"] - row["qty_open"]) - row["charges"], 2)
    r = act(action="sell", price=px + 6, date=d)
    c = r.json()
    assert (c["status"], c["qty_open"], c["exit_reason"], c["exit_date"]) == ("closed", 0, "Manual exit", d)
    assert c["realized"] == pytest.approx(50 * (px + 5) + (total - 50) * (px + 6))
    assert act(action="sell", price=px).status_code == 400              # already closed
    assert act(action="bogus", price=1).status_code == 400
    assert api.post("/api/calls/999999/action", json={"action": "sell", "price": 1}).status_code == 400
    assert api.get("/api/calls/999999").status_code == 404
    assert _event_kinds(cid) == ["plan", "fill", "average", "exit", "exit"]

    # a planned call can be cancelled, and then nothing else
    pid = tracker.create_call(make_plan("CCC", atr=10.0), _last_session())
    assert api.post(f"/api/calls/{pid}/action", json={"action": "cancel"}).json()["status"] == "cancelled"
    assert api.post(f"/api/calls/{pid}/action", json={"action": "buy", "price": px}).status_code == 400


def test_cause_analysis_red_flag_and_average_chat(api, monkeypatch):
    plan = make_plan("DDD", atr=5.0)
    cid = tracker.create_call(plan, _last_session())
    cl, stop = plan["signal_close"], plan["stop"]
    add_session({"DDD": (cl, cl + 1, cl - 1, cl)})
    tracker.update_all(red_flag_check=False)
    assert _call(cid)["status"] == "open"
    down = round(cl * 0.94, 2)                       # -6%, still above the stop
    assert down - 0.5 > stop
    d = add_session({"DDD": (cl - 2, cl - 1.5, down - 0.5, down)})
    tracker.update_all(red_flag_check=False)
    c = _call(cid)
    assert c["status"] == "open" and _event_kinds(cid) == ["plan", "fill", "cause"]
    msg = store.q1("SELECT message FROM call_events WHERE call_id=? AND kind='cause'", (cid,))["message"]
    assert "stock-specific" in msg and "do not average" in msg
    rows = {r["id"]: r for r in tracker.to_average()}
    assert rows[cid]["average_ok"] is False and rows[cid]["date"] == d
    assert cid in {r["id"] for r in api.get("/api/watchlist").json()["to_average"]}
    assert f"#{cid} DDD: stock-specific" in _chat(api, "average")["text"]
    assert f"#{cid} DDD open" in _chat(api, "holdings")["text"]

    # red-flag NSE filing on an open position -> one 'redflag' event, not duplicated on re-run
    flag = Fact(key="ann:42", label="Resignation of statutory auditor", value="auditor resigned",
                url="https://nsearchives.nseindia.com/corporate/x.pdf", as_of=d, fetched_at=FROZEN_NOW.isoformat(),
                tier=1, extra={"red_flags": ["Auditor resignation"], "positives": []}).finalize()
    monkeypatch.setattr(news, "announcements", lambda symbol, today, days=90: [flag] if symbol == "DDD" else [])
    tracker.update_all(red_flag_check=True)
    tracker.update_all(red_flag_check=True)
    assert _event_kinds(cid).count("redflag") == 1

    # chat logs manual actions on the call
    out = _chat(api, f"sold {cid} at {down}")["text"]
    assert out.startswith(f"Logged sell on call #{cid} DDD: status closed, qty open 0")


def test_pdf_generation_and_download(offline, api):
    plan = make_plan("AAA", atr=2.0)
    fname = pdfmod.plan_pdf(plan, offline["scan"])
    path = config.REPORTS_DIR / fname
    assert path.exists() and path.resolve().parent == Path(config.REPORTS_DIR).resolve()
    assert str(path.resolve()).startswith(str(_DATA))
    assert path.read_bytes()[:4] == b"%PDF"
    r = api.get(f"/api/pdf/{fname}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content == path.read_bytes()
    assert api.get("/api/pdf/does_not_exist.pdf").status_code == 404
    assert api.get("/api/pdf/..%5Ctracker.db").status_code == 404        # no escaping the reports dir
    cid = store.q1("SELECT id FROM calls WHERE symbol='AAA' ORDER BY id LIMIT 1")["id"]
    r = api.post(f"/api/pdf/call/{cid}")
    assert r.status_code == 200 and (config.REPORTS_DIR / r.json()["pdf"]).exists()
    assert api.post("/api/pdf/call/999999").status_code == 404
