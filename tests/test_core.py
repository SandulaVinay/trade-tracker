"""Core calculation tests (run: python -m pytest -q)."""
import os
import tempfile
from datetime import date

os.environ.setdefault("TT_DATA_DIR", tempfile.mkdtemp())

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tt import costs, tradeplan  # noqa: E402
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
