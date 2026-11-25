"""Core calculation tests (run: python -m pytest -q)."""
import os
import tempfile
import time
from datetime import date

os.environ.setdefault("TT_DATA_DIR", tempfile.mkdtemp())

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from tt import analysis, costs, news, tradeplan  # noqa: E402
from tt.confidence import Fact, tier_of, two_date_check  # noqa: E402
from tt.llm import verify_numbers  # noqa: E402
from tt.market import Panel, compute_indicators  # noqa: E402


def test_worked_charge_example():
    # Research note: Rs 1 lakh buy + Rs 1.05 lakh sell costs about Rs 243 at a zero-brokerage broker.
    total = costs.round_trip(100_000, 105_000)
    assert 240 <= total <= 246, total


def test_tax():
    assert costs.tax_on_gain(10_000, 100) == 2_000
    assert costs.tax_on_gain(-5_000, 100) == 0
    assert costs.tax_on_gain(200_000, 400, ltcg_exempt_left=125_000) == 75_000 * 0.125


def test_tiers_and_confidence():
    assert tier_of("https://nsearchives.nseindia.com/x.csv") == 1
    assert tier_of("https://www.business-standard.com/a") == 2
    assert tier_of("https://www.screener.in/company/TCS") == 3
    assert tier_of("https://t.me/tips") == 4
    f = Fact("k", "l", "v", "https://www.nseindia.com/api/x", "2026-11-20", "now", tier=1, date_check="pass").finalize()
    assert f.status == "fact" and f.score == 1.0
    g = Fact("k", "l", "v", "https://www.livemint.com/a", "2026-11-20", "now", tier=2).finalize()
    assert g.status == "unverified"
    g2 = Fact("k", "l", "v", "https://www.livemint.com/a", "2026-11-20", "now", tier=2, confirmations=1).finalize()
    assert g2.status == "fact"
    h = Fact("k", "l", "v", "https://www.nseindia.com/a", "2026-11-20", "now", tier=1, date_check="fail").finalize()
    assert h.status != "fact"
    z = Fact("k", "l", "v", "https://randomblog.example/a", "2026-11-20", "now").finalize()
    assert z.status == "discarded"


def test_two_date_check():
    assert two_date_check(date(2026, 11, 20), "2026-11-20") == "pass"
    assert two_date_check("2026-11-20", "2026-11-19") == "fail"
    assert two_date_check(None, "2026-11-19") == "n/a"


def test_expiry_last_tuesday_holiday_shift():
    assert tradeplan.last_tuesday(2026, 11) == date(2026, 11, 24)
    # 24 Nov 2026 is a holiday (Guru Nanak Jayanti) -> expiry moves to Monday 23 Nov
    assert tradeplan.next_monthly_expiry(date(2026, 11, 20), {date(2026, 11, 24)}) == date(2026, 11, 23)
    assert tradeplan.next_monthly_expiry(date(2026, 11, 25), set()) == date(2026, 12, 29)


def test_plan_levels_and_sizing():
    row = {"close": 1000.0, "atr": 20.0, "sma20": 980, "sma50": 950, "sma200": 900, "lo20": 960}
    p = tradeplan.build("X", "X Ltd", row, {"t1_days_median": 9.0}, 500_000, 1.0, "S2", "Breakout")
    assert p["stop"] == 950.0                      # 1000 - 2.5*20
    assert p["target1"] == 1075.0 and p["target2"] == 1150.0
    assert p["buy_zone"] == [995.0, 1005.0] and p["no_chase_above"] == 1020.0
    pos = p["position"]
    assert pos["max_loss"] <= 500_000 * 0.0075 + 1
    assert pos["value"] <= 500_000 * 0.10 + 1
    half = tradeplan.build("X", "X Ltd", row, {}, 500_000, 0.5, "S2", "Breakout")["position"]
    assert half["qty"] <= pos["qty"] // 2 + 1


def test_llm_number_guard():
    ctx = '{"close": 1234.5, "stop": 1180.25, "win_rate": 0.48}'
    assert verify_numbers("Stop is Rs 1,180.25 and close 1234.50", ctx) == []
    assert verify_numbers("Target is Rs 1999", ctx) == ["1999"]


def test_corporate_action_adjustment():
    """A 1:1 bonus (from NSE's corporate-actions feed) must not look like a 50% crash."""
    from tt import market, store
    from tt.corp_actions import parse_factor
    assert parse_factor("Bonus 1:1") == 0.5
    assert parse_factor("Bonus 4:1") == 0.2
    assert parse_factor("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share") == 0.2
    assert abs(parse_factor("Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share") - 0.1) < 1e-9
    assert parse_factor("Dividend - Rs 13 Per Share") is None
    dates = pd.bdate_range("2024-01-01", periods=6)
    close = [100, 102, 104, 52, 53, 54]
    with store.tx() as c:
        c.execute("DELETE FROM prices WHERE symbol='ZZA'")
        c.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      [("ZZA", d.strftime("%Y-%m-%d"), "EQ", x, x, x, x, x, 1000, 10, 10, 500, 50) for d, x in zip(dates, close)])
        c.execute("INSERT OR REPLACE INTO universe VALUES ('ZZA','ZZA Ltd','X','INE0','u','t')")
        c.execute("INSERT OR REPLACE INTO corp_actions VALUES ('ZZA', ?, 'Bonus 1:1', 0.5, 'u', 't')",
                  (dates[3].strftime("%Y-%m-%d"),))
    p = market.load_panel(symbols=["ZZA"])
    adj = p.close["ZZA"].tolist()
    assert abs(adj[2] - 52) < 1e-6 and abs(adj[3] - 52) < 1e-6
    assert p.raw_close["ZZA"].iloc[0] == 100


def _synthetic_panel(n_days=700, n_sym=30, seed=1):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-01", periods=n_days)
    syms = [f"S{i}" for i in range(n_sym)]
    drift = rng.normal(0.0006, 0.0004, n_sym)
    rets = rng.normal(drift, 0.018, (n_days, n_sym))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=syms)
    high, low = close * 1.01, close * 0.99
    open_ = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, (n_days, n_sym)))
    vol = pd.DataFrame(rng.lognormal(12, 0.5, (n_days, n_sym)), index=dates, columns=syms)
    to = vol * close / 1e7
    dp = pd.DataFrame(rng.uniform(20, 70, (n_days, n_sym)), index=dates, columns=syms)
    ind = pd.Series({s: f"I{i % 5}" for i, s in enumerate(syms)})
    idx = pd.DataFrame({"Nifty 500": close.mean(axis=1) * 100}, index=dates)
    return Panel(open_, high, low, close, vol, to * 100, dp, close, ind, pd.Series({s: s for s in syms}), idx)


def test_backtest_runs_and_accounts():
    from tt import backtest
    from tt.strategies import BY_KEY
    p = _synthetic_panel()
    compute_indicators(p)
    for key in ("S1", "S2", "S3"):
        res = backtest.run(BY_KEY[key], p)
        m = backtest.metrics(res, p)
        eq = res["equity"]["equity"]
        assert (eq > 0).all()
        for t in res["trades"]:
            assert t["hold_days"] >= 0
            assert t["exit_date"] >= t["entry_date"]
        if m["trades"]:
            assert 0 <= m["win_rate"] <= 1
            assert m["max_drawdown"] <= 0


def test_stale_official_filing_is_background_not_decision():
    f = Fact("k", "l", "v", "https://www.nseindia.com/a", "2026-10-01", "now", tier=1, stale=True, date_check="pass").finalize()
    assert f.status == "background"


def test_price_cross_check():
    from types import SimpleNamespace
    from tt.signals import cross_check_price
    df = pd.DataFrame({"TckrSymb": ["AAA", "BBB"], "SctySrs": ["EQ", "EQ"], "ClsPric": [100.0, 200.0]})
    xc = SimpleNamespace(data=df, url="https://nsearchives.nseindia.com/content/cm/x.zip",
                         content_date=date(2026, 11, 23), label_date=date(2026, 11, 23))
    fact, err = cross_check_price(xc, "AAA", 100.2, "2026-11-23")
    assert err is None and fact["status"] == "fact"
    fact, err = cross_check_price(xc, "BBB", 190.0, "2026-11-23")
    assert err and fact["status"] != "fact"
    _, err = cross_check_price(xc, "ZZZ", 10.0, "2026-11-23")
    assert err
    _, err = cross_check_price(None, "AAA", 100.0, "2026-11-23")
    assert err


def test_password_gate(monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    from tt import app as appmod, config
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret-test")
    c = TestClient(appmod.app)
    assert c.get("/api/summary").status_code == 401
    bad = base64.b64encode(b"x:wrong").decode()
    assert c.get("/api/summary", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"anyone:s3cret-test").decode()
    assert c.get("/api/summary", headers={"Authorization": f"Basic {good}"}).status_code == 200
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    assert c.get("/api/summary").status_code == 200


def _stub_analyse_market(monkeypatch):
    p = _synthetic_panel(n_days=700, n_sym=5, seed=4)
    compute_indicators(p)
    day = p.close.index[-1].date().isoformat()
    monkeypatch.setattr(analysis.signals, "ensure_fresh", lambda: {"latest": day, "fresh": True})
    monkeypatch.setattr(analysis, "load_panel", lambda **kwargs: p)
    monkeypatch.setattr(analysis.signals, "active_strategy", lambda: ("S2", None))
    return p.close.columns[0]


def test_slow_deep_check_blocks_within_budget(monkeypatch):
    sym = _stub_analyse_market(monkeypatch)
    budget = 1.0
    monkeypatch.setattr(analysis, "DEEP_CHECK_BUDGET_S", budget)

    def slow(symbol, company, today):
        time.sleep(budget + 2)
        return {"block_reasons": [], "warnings": [], "positives": [], "results": {},
                "shareholding": {}, "facts": [], "surveillance_urls": {}, "results_event": None}

    monkeypatch.setattr(news, "company_check", slow)
    started = time.perf_counter()
    a = analysis.analyse(sym, deep=True)
    assert time.perf_counter() - started < budget + 5
    assert any("not verified" in r for r in a["plan"]["blocked"])
    assert a["plan"]["surveillance_urls"]


def test_fast_deep_check_keeps_verdict(monkeypatch):
    sym = _stub_analyse_market(monkeypatch)

    def fast(symbol, company, today):
        return {"block_reasons": [], "warnings": ["Promoter pledge 6.0%"], "positives": [],
                "results": {}, "shareholding": {}, "facts": [], "surveillance_urls": {"asm": "test-url"},
                "results_event": None}

    monkeypatch.setattr(news, "company_check", fast)
    a = analysis.analyse(sym, deep=True)
    assert a["plan"]["blocked"] == []
    assert a["plan"]["company"]["warnings"] == ["Promoter pledge 6.0%"]
    assert not any("not verified" in r for r in a["plan"]["blocked"])


def test_unhandled_error_is_friendly():
    from fastapi.testclient import TestClient
    from tt import app as appmod

    def boom():
        raise RuntimeError("secret-internal-detail")

    appmod.app.add_api_route("/_boom", boom)
    route = appmod.app.routes.pop()
    mount_at = next(i for i, r in enumerate(appmod.app.routes) if getattr(r, "name", None) == "web")
    appmod.app.routes.insert(mount_at, route)
    try:
        c = TestClient(appmod.app, raise_server_exceptions=False)
        resp = c.get("/_boom")
        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert "ref " in detail and "secret-internal-detail" not in detail
    finally:
        appmod.app.routes.remove(route)


def test_chat_memory_error_is_friendly(monkeypatch):
    from fastapi.testclient import TestClient
    from tt import app as appmod

    def boom(*args, **kwargs):
        raise MemoryError("secret-internal-detail")

    monkeypatch.setattr(appmod.chat, "handle", boom)
    c = TestClient(appmod.app)
    text = c.post("/api/chat", json={"message": "hello"}).json()["text"]
    assert "ran out of memory" in text and "Traceback" not in text


def test_chat_hides_internal_exception_text(monkeypatch):
    from fastapi.testclient import TestClient
    from tt import app as appmod

    def boom(*args, **kwargs):
        raise ValueError("secret-internal-detail")

    monkeypatch.setattr(appmod.chat, "handle", boom)
    c = TestClient(appmod.app)
    text = c.post("/api/chat", json={"message": "hello"}).json()["text"]
    assert "secret-internal-detail" not in text and "ref " in text


@pytest.fixture
def dashboard_seed():
    from types import SimpleNamespace
    from tt import store

    calls, prices, logs = [], [], []

    def add_call(symbol, status, signal_date="2020-01-02", **kw):
        row = {"signal_date": signal_date, "symbol": symbol, "name": symbol, "strategy": "S2",
               "status": status, "mode": "paper", "plan": "{}", **kw}
        with store.tx() as c:
            cur = c.execute("INSERT INTO calls (%s) VALUES (%s)" % (",".join(row), ",".join("?" * len(row))),
                            tuple(row.values()))
            cid = cur.lastrowid
        calls.append(cid)
        return cid

    def add_event(cid, d, kind):
        with store.tx() as c:
            c.execute("INSERT INTO call_events (call_id,date,kind,message) VALUES (?,?,?,?)",
                      (cid, d, kind, "test"))

    def add_price(sym, d, close):
        with store.tx() as c:
            c.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sym, d, "EQ", close, close, close, close, close, 1000, 10, 10, 500, 50))
        prices.append((sym, d))

    def add_log(kind, d, fetched_at):
        with store.tx() as c:
            c.execute("INSERT OR REPLACE INTO sync_log VALUES (?,?,?,?,?,?,?,?,?)",
                      (kind, d, "https://nsearchives.nseindia.com/x.csv", d, d, fetched_at, "ok", 1, ""))
        logs.append((kind, d))

    yield SimpleNamespace(add_call=add_call, add_event=add_event, add_price=add_price, add_log=add_log)

    with store.tx() as c:
        if calls:
            ph = ",".join("?" * len(calls))
            c.execute(f"DELETE FROM call_events WHERE call_id IN ({ph})", tuple(calls))
            c.execute(f"DELETE FROM calls WHERE id IN ({ph})", tuple(calls))
        for sym, d in prices:
            c.execute("DELETE FROM prices WHERE symbol=? AND date=?", (sym, d))
        for kind, d in logs:
            c.execute("DELETE FROM sync_log WHERE kind=? AND date=?", (kind, d))


def _api_client():
    from fastapi.testclient import TestClient
    from tt import app as appmod
    return TestClient(appmod.app)


def test_dashboard_counts(dashboard_seed):
    s = dashboard_seed
    s.add_call("DAW", "closed", entry_date="2020-01-03", entry_price=100, qty=10, qty_open=0,
               realized=1100, charges=20, exit_reason="Manual exit")
    s.add_call("DAL", "closed", entry_date="2020-01-03", entry_price=200, qty=10, qty_open=0,
                         realized=1900, charges=20, exit_reason="Stop-loss (gap down)")
    s.add_call("DAE", "expired")
    opened = s.add_call("DAO", "open", entry_date="2020-01-03", entry_price=300, qty=5, qty_open=5, last_price=330)
    s.add_event(opened, "2020-01-04", "target")
    s.add_call("DAP", "planned")
    d = _api_client().get("/api/dashboard", params={"from": "2020-01-01", "to": "2020-01-10"}).json()
    assert (d["total_calls"], d["triggered"], d["not_triggered"], d["pending"], d["open"], d["closed"]) == (5, 3, 1, 1, 1, 2)
    assert (d["wins"], d["losses"], d["accuracy"], d["target_hits"], d["stop_hits"], d["other_closes"]) == (1, 1, 0.5, 1, 1, 1)
    assert d["range"] == {"from": "2020-01-01", "to": "2020-01-10"}
    assert (d["invested"], d["realized_pnl"], d["unrealized_pnl"], d["net_pnl"]) == (4500, -40, 150, 110)
    assert d["calls"] and d["calls"][0]["signal_date"] == "2020-01-02"


def test_dashboard_date_filter_and_validation(dashboard_seed):
    s = dashboard_seed
    inside = s.add_call("DAIN", "planned", signal_date="2020-01-02")
    outside = s.add_call("DAOUT", "planned", signal_date="2020-01-20")
    c = _api_client()
    d = c.get("/api/dashboard", params={"from": "2020-01-01", "to": "2020-01-10"}).json()
    assert [r["id"] for r in d["calls"]] == [inside] and d["total_calls"] == 1
    assert outside not in {r["id"] for r in d["calls"]}
    bad = c.get("/api/dashboard", params={"from": "2020-02-30"})
    assert bad.status_code == 400 and "YYYY-MM-DD" in bad.json()["detail"]
    reversed_ = c.get("/api/dashboard", params={"from": "2020-01-10", "to": "2020-01-01"})
    assert reversed_.status_code == 400 and "after" in reversed_.json()["detail"]


def test_price_endpoint(dashboard_seed):
    s = dashboard_seed
    s.add_price("DXP", "2020-01-01", 100.0)
    s.add_price("DXP", "2020-01-02", 101.5)
    s.add_log("bhav", "2020-01-02", "2020-01-02T19:05:00+05:30")
    c = _api_client()
    assert c.get("/api/price/UNKNOWNXYZ").status_code == 404
    r = c.get("/api/price/DXP")
    assert r.status_code == 200
    assert r.json() == {"symbol": "DXP", "close": 101.5, "price_date": "2020-01-02",
                        "fetched_at": "2020-01-02T19:05:00+05:30"}


def test_dashboard_zero_closed_accuracy_is_null(dashboard_seed):
    dashboard_seed.add_call("DAP0", "planned", signal_date="2020-02-01")
    d = _api_client().get("/api/dashboard", params={"from": "2020-02-01", "to": "2020-02-02"}).json()
    assert d["closed"] == 0 and d["accuracy"] is None
