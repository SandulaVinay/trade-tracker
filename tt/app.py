"""FastAPI web app + daily scheduler.  Run:  python -m tt.app   (http://localhost:8000)"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from datetime import date, datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analysis, chat, data_sync, llm, pdf, signals, store, tracker
from .config import (DAILY_SCAN_HOUR_IST, DAILY_SCAN_MINUTE_IST, DEFAULT_CAPITAL, REPORTS_DIR, WEB_DIR)
from .strategies import BY_KEY, CATALOG

log = logging.getLogger("tt.app")
app = FastAPI(title="Trade Tracker", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def require_password(request, call_next):
    """HTTP Basic login on every route when TT_PASSWORD is set (needed before exposing the app publicly)."""
    import base64
    import secrets
    from fastapi.responses import Response
    from .config import APP_PASSWORD
    if not APP_PASSWORD or request.url.path == "/healthz":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    ok = False
    if auth.lower().startswith("basic "):
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode("utf8").partition(":")
            ok = secrets.compare_digest(pw.encode(), APP_PASSWORD.encode())
        except Exception:
            ok = False
    if not ok:
        return Response("Login required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Trade Tracker", charset="UTF-8"'})
    return await call_next(request)

# ---------------------------------------------------------------- background jobs
_job_lock = threading.Lock()
JOB: dict = {"name": None, "started": None, "finished": None, "ok": None, "log": [], "result": None}


def _run_job(name: str, fn):
    if not _job_lock.acquire(blocking=False):
        return False

    def wrap():
        JOB.update(name=name, started=store.now_ist().isoformat(), finished=None, ok=None, log=[], result=None)
        try:
            JOB["result"] = fn()
            JOB["ok"] = True
        except Exception as e:  # surfaced in the UI
            JOB["ok"] = False
            JOB["log"].append(f"{type(e).__name__}: {e}")
            log.error(traceback.format_exc())
        finally:
            JOB["finished"] = store.now_ist().isoformat()
            _job_lock.release()
    threading.Thread(target=wrap, daemon=True).start()
    return True


def _step(msg):
    JOB["log"].append(f"{store.now_ist():%H:%M:%S} {msg}")
    log.info(msg)


def daily_pipeline():
    _step("Checking NSE trading calendar and syncing latest files (two-date check)")
    exp = signals.expected_data_date()
    stats = data_sync.sync_range(exp - timedelta(days=10), exp)
    _step(f"Sync: {stats}")
    store.kv_set("last_sync", {"at": store.now_ist().isoformat(), "stats": stats})
    from . import corp_actions
    _step(f"Corporate actions (splits/bonuses, refreshed weekly): {corp_actions.sync_all(max_age_days=7)}")
    _step("Updating open and planned calls with the new session")
    t = tracker.update_all()
    _step(f"Tracker: {t}")
    _step("Running daily scan (regime, gates, ranking, company checks)")
    r = signals.run_scan()
    _step(r.get("message", r.get("status")))
    return {"status": r.get("status"), "message": r.get("message")}


def backtest_job():
    from . import backtest
    from .market import compute_indicators, load_panel
    _step("Loading full NSE history panel")
    p = load_panel()
    if len(p.dates) < 750:
        raise RuntimeError(f"Only {len(p.dates)} sessions of history here; backtests need 3+ years. "
                           "Run backtests on the local copy (full history); this server shows those seeded results.")
    compute_indicators(p)
    _step(f"Panel {p.close.shape[0]} sessions x {p.close.shape[1]} stocks; running {len(CATALOG)} strategies")
    res = backtest.run_catalog(p)
    w = backtest.choose_winner(res)
    if w:
        store.kv_set("active_strategy", w["strategy"])
        _step(f"Winner: {w['strategy']} (positive in both periods, stable under +-20% parameters)")
    else:
        _step("No strategy passed every robustness test; keeping the current active strategy")
    store.kv_set("backtest_winner", w["strategy"] if w else None)
    return {"winner": w["strategy"] if w else None}


def bootstrap_job():
    """First start on an empty server: universe, recent NSE history, corporate actions, then the daily scan."""
    _step("Fresh server: downloading the Nifty 500 list and recent NSE history (two-date checked)")
    stats = data_sync.sync_all()
    _step(f"History sync: {stats}")
    return daily_pipeline()


def import_seed():
    """Load backtest results produced on the full-history local copy (seed/backtests.json) if none exist."""
    from .config import SEED_DIR
    path = SEED_DIR / "backtests.json"
    if store.q1("SELECT 1 FROM backtests LIMIT 1") or not path.exists():
        return
    seed = json.loads(path.read_text(encoding="utf8"))
    with store.tx() as c:
        for r in seed["backtests"]:
            c.execute("INSERT INTO backtests (run_at,strategy,params,metrics,trades) VALUES (?,?,?,?,?)",
                      (r["run_at"], r["strategy"], r["params"], r["metrics"], r["trades"]))
    for k in ("active_strategy", "backtest_winner"):
        if seed.get(k):
            store.kv_set(k, seed[k])
    log.info("Imported %d seeded backtests (from %s)", len(seed["backtests"]), seed.get("exported_at"))


def scheduler():
    while True:
        try:
            now = store.now_ist()
            exp = signals.expected_data_date(now)
            last = store.q1("SELECT data_date FROM scans WHERE status IN ('pick','no_trade') ORDER BY id DESC LIMIT 1")
            due = now.time() >= datetime.strptime(f"{DAILY_SCAN_HOUR_IST}:{DAILY_SCAN_MINUTE_IST}", "%H:%M").time() \
                or exp < now.date()
            if due and (not last or last["data_date"] < exp.isoformat()):
                _run_job("daily", daily_pipeline)
        except Exception as e:
            log.warning("scheduler: %s", e)
        time.sleep(300)


@app.on_event("startup")
def _start():
    import os
    if os.environ.get("TT_NO_SCHEDULER"):
        return
    import_seed()
    if not store.q1("SELECT 1 FROM sync_log WHERE kind='bhav' AND status='ok' LIMIT 1"):
        _run_job("bootstrap", bootstrap_job)  # fresh server: download recent NSE history first
    if not getattr(app.state, "sched", None):
        app.state.sched = threading.Thread(target=scheduler, daemon=True)
        app.state.sched.start()


# ---------------------------------------------------------------- API
class ActionIn(BaseModel):
    action: str
    price: float | None = None
    qty: int | None = None
    date: str | None = None


class ChatIn(BaseModel):
    message: str


class WatchIn(BaseModel):
    symbol: str
    note: str = ""


class SettingsIn(BaseModel):
    capital: float | None = None


class StrategyIn(BaseModel):
    key: str


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness check for the host (no data exposed)."""
    return {"ok": True}


@app.get("/api/nse-check")
def nse_check():
    """Can this server reach NSE? (Cloud data-centre IPs are often blocked.)"""
    from .nse import client
    out = {}
    d = signals.expected_data_date()
    for name, fn in (("archive_bhavcopy", lambda: client.bhavcopy(d)), ("api_holidays", client.holidays)):
        t = time.time()
        try:
            fn()
            out[name] = {"ok": True, "seconds": round(time.time() - t, 2)}
        except Exception as e:
            out[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "seconds": round(time.time() - t, 2)}
    return {"date": str(d), **out}


@app.get("/api/status")
def status():
    latest = data_sync.latest_data_date()
    try:
        exp = signals.expected_data_date().isoformat()
    except Exception:
        exp = None
    return {"latest_data": latest, "expected_data": exp, "fresh": bool(latest) and latest == exp,
            "last_sync": store.kv_get("last_sync"), "capital": store.kv_get("capital") or DEFAULT_CAPITAL,
            "active_strategy": signals.active_strategy()[0], "llm": llm.enabled(), "job": JOB,
            "now_ist": store.now_ist().isoformat(), "universe": store.kv_get("universe_meta")}


@app.post("/api/run/{name}")
def run(name: str):
    fn = {"daily": daily_pipeline, "backtest": backtest_job}.get(name)
    if not fn:
        raise HTTPException(404)
    if not _run_job(name, fn):
        raise HTTPException(409, "Another job is running")
    return {"started": name}


@app.get("/api/today")
def today():
    return signals.latest_scan() or {}


@app.get("/api/scans")
def scans(limit: int = 30):
    return store.q("SELECT id,data_date,run_at,status,json_extract(result,'$.message') message FROM scans ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/calls")
def calls(status: str | None = None):
    sql = "SELECT * FROM calls"
    args: tuple = ()
    if status:
        sql += " WHERE status IN (%s)" % ",".join("?" * len(status.split(",")))
        args = tuple(status.split(","))
    rows = store.q(sql + " ORDER BY id DESC", args)
    for r in rows:
        plan = json.loads(r.pop("plan"))
        r["plan_summary"] = {k: plan.get(k) for k in ("buy_zone", "no_chase_above", "target1", "target2", "strategy_name", "data_date")}
        if r["status"] == "closed" and r["entry_price"]:
            r["pnl"] = round(r["realized"] - r["entry_price"] * r["qty"] - r["charges"], 2)
            r["ret"] = r["pnl"] / (r["entry_price"] * r["qty"])
        elif r["status"] == "open" and r["entry_price"] and r["last_price"]:
            r["unrealized"] = round((r["last_price"] - r["entry_price"]) * r["qty_open"] + r["realized"]
                                    - r["entry_price"] * (r["qty"] - r["qty_open"]) - r["charges"], 2)
            r["ret"] = r["last_price"] / r["entry_price"] - 1
    return rows


@app.get("/api/calls/{cid}")
def call(cid: int):
    r = store.q1("SELECT * FROM calls WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404)
    r["plan"] = json.loads(r["plan"])
    r["events"] = store.q("SELECT * FROM call_events WHERE call_id=? ORDER BY id", (cid,))
    return r


@app.post("/api/calls/{cid}/action")
def call_action(cid: int, a: ActionIn):
    try:
        if a.action in ("buy", "sell", "average") and not (a.price and a.price > 0):
            raise ValueError("a positive price is required")
        return tracker.manual(cid, a.action, a.price or 0.0, a.qty, a.date)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/summary")
def summary():
    return tracker.summary()


@app.get("/api/watchlist")
def watchlist():
    rows = store.q("SELECT * FROM watchlist ORDER BY added_at DESC")
    for r in rows:
        px = store.q("SELECT date, close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 2", (r["symbol"],))
        if px:
            r["close"], r["date"] = px[0]["close"], px[0]["date"]
            r["chg"] = (px[0]["close"] / px[1]["close"] - 1) if len(px) > 1 and px[1]["close"] else None
        u = store.q1("SELECT name, industry FROM universe WHERE symbol=?", (r["symbol"],))
        r.update(u or {})
    sc = signals.latest_scan() or {}
    return {"manual": rows, "upcoming": sc.get("upcoming", []), "to_average": tracker.to_average(),
            "signals": sc.get("signals_by_strategy", {}), "data_date": (sc.get("regime") or {}).get("date")}


@app.post("/api/watchlist")
def watch_add(w: WatchIn):
    sym = analysis.resolve_symbol(w.symbol)
    if not sym:
        raise HTTPException(404, "symbol not found in NSE data")
    with store.tx() as c:
        c.execute("INSERT OR REPLACE INTO watchlist VALUES (?,?,?,?)", (sym, store.now_ist().isoformat(), "manual", w.note))
    return {"symbol": sym}


@app.delete("/api/watchlist/{sym}")
def watch_del(sym: str):
    with store.tx() as c:
        c.execute("DELETE FROM watchlist WHERE symbol=?", (sym.upper(),))
    return {"ok": True}


@app.get("/api/analyse/{q}")
def analyse(q: str):
    sym = analysis.resolve_symbol(q)
    if not sym:
        raise HTTPException(404, "symbol not found")
    return analysis.analyse(sym)


@app.get("/api/search")
def search(q: str):
    ql = f"%{q.upper()}%"
    return store.q("SELECT symbol, name FROM universe WHERE symbol LIKE ? OR UPPER(name) LIKE ? ORDER BY symbol LIMIT 15", (ql, ql))


@app.get("/api/backtests")
def backtests():
    rows = store.q("""SELECT b.* FROM backtests b JOIN (SELECT strategy, MAX(id) id FROM backtests GROUP BY strategy) m
                      ON b.id = m.id ORDER BY b.strategy""")
    out = []
    for r in rows:
        params = json.loads(r["params"])
        eq = params.pop("equity", [])
        step = max(1, len(eq) // 250)
        s = BY_KEY.get(r["strategy"])
        out.append({"strategy": r["strategy"], "name": s.name if s else r["strategy"], "description": s.description if s else "",
                    "rules": s.rules if s else [], "run_at": r["run_at"], "params": params,
                    "metrics": json.loads(r["metrics"]), "equity": eq[::step],
                    "trades_sample": json.loads(r["trades"])[-40:]})
    return {"results": out, "active": signals.active_strategy()[0], "winner": store.kv_get("backtest_winner")}


@app.post("/api/strategy/active")
def set_active(s: StrategyIn):
    if s.key not in BY_KEY:
        raise HTTPException(404)
    store.kv_set("active_strategy", s.key)
    return {"active": s.key}


@app.post("/api/settings")
def settings(s: SettingsIn):
    if s.capital is not None:
        if s.capital < 10000:
            raise HTTPException(400, "capital too small")
        store.kv_set("capital", s.capital)
    return {"capital": store.kv_get("capital") or DEFAULT_CAPITAL}


@app.get("/api/sources")
def sources():
    summ = store.q("SELECT kind, status, COUNT(*) n, MIN(date) first, MAX(date) last FROM sync_log GROUP BY kind, status")
    recent = store.q("SELECT * FROM sync_log ORDER BY date DESC, kind LIMIT 30")
    rejected = store.q("SELECT * FROM sync_log WHERE status='date_mismatch' ORDER BY date DESC LIMIT 50")
    return {"summary": summ, "recent": recent, "rejected": rejected, "universe": store.kv_get("universe_meta"),
            "holidays_url": (store.kv_get("holidays") or {}).get("url")}


@app.post("/api/chat")
def chat_post(c: ChatIn):
    try:
        return chat.handle(c.message)
    except Exception as e:
        log.error(traceback.format_exc())
        return {"text": f"Error: {type(e).__name__}: {e}"}


@app.get("/api/chat")
def chat_hist(limit: int = 60):
    return list(reversed(store.q("SELECT * FROM chat ORDER BY id DESC LIMIT ?", (limit,))))


@app.post("/api/pdf/call/{cid}")
def pdf_call(cid: int):
    r = store.q1("SELECT plan, signal_date FROM calls WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404)
    sc = store.q1("SELECT result FROM scans WHERE data_date=? AND status='pick' ORDER BY id DESC LIMIT 1", (r["signal_date"],))
    return {"pdf": pdf.plan_pdf(json.loads(r["plan"]), json.loads(sc["result"]) if sc else None)}


@app.get("/api/pdf/{fname}")
def pdf_get(fname: str):
    path = (REPORTS_DIR / fname).resolve()
    if path.parent != REPORTS_DIR.resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", filename=fname, content_disposition_type="inline")


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    import os
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("TT_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
