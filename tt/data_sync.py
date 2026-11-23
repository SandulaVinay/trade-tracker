"""Download Tier-1 NSE data into SQLite with two-date verification.

Every file is accepted only if the date in its URL/filename (label date) equals the date inside
its content (content date) and equals the trading day we asked for.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

from . import store
from .config import HISTORY_START
from .nse import DateMismatch, NotPublished, client

log = logging.getLogger("tt.sync")

PRICE_COLS = ["SYMBOL", "SERIES", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE",
              "PREV_CLOSE", "TTL_TRD_QNTY", "TURNOVER_LACS", "NO_OF_TRADES", "DELIV_QTY", "DELIV_PER"]


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.replace({"-": None, "": None}), errors="coerce")


def _log(kind: str, d: date, url: str, status: str, label=None, content=None, rows=0, detail=""):
    with store.tx() as c:
        c.execute("INSERT OR REPLACE INTO sync_log VALUES (?,?,?,?,?,?,?,?,?)",
                  (kind, d.isoformat(), url, str(label or ""), str(content or ""),
                   store.now_ist().isoformat(), status, rows, detail))


def _done(kind: str) -> dict[str, str]:
    return {r["date"]: r["status"] for r in store.q("SELECT date,status FROM sync_log WHERE kind=?", (kind,))}


def sync_universe() -> int:
    f = client.nifty500_list()
    df = f.data
    with store.tx() as c:
        c.execute("DELETE FROM universe")
        c.executemany("INSERT INTO universe VALUES (?,?,?,?,?,?)", [
            (r["Symbol"].strip(), r["Company Name"].strip(), r["Industry"].strip(), r["ISIN Code"].strip(),
             f.url, f.fetched_at.isoformat()) for _, r in df.iterrows()])
    store.kv_set("universe_meta", {"url": f.url, "fetched_at": f.fetched_at.isoformat(), "count": len(df)})
    return len(df)


def load_bhavcopy(d: date) -> str:
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
    try:
        f = client.bhavcopy(d)
    except NotPublished:
        _log("bhav", d, url, "missing")
        return "missing"
    except DateMismatch as e:
        _log("bhav", d, url, "date_mismatch", detail=str(e))
        return "date_mismatch"
    df = f.data
    df = df[df["SERIES"].isin(["EQ", "BE", "BZ"])][PRICE_COLS].copy()
    for c in PRICE_COLS[2:]:
        df[c] = _num(df[c])
    rows = [(r.SYMBOL, d.isoformat(), r.SERIES, r.OPEN_PRICE, r.HIGH_PRICE, r.LOW_PRICE, r.CLOSE_PRICE,
             r.PREV_CLOSE, r.TTL_TRD_QNTY, r.TURNOVER_LACS, r.NO_OF_TRADES, r.DELIV_QTY, r.DELIV_PER)
            for r in df.itertuples(index=False)]
    with store.tx() as c:
        c.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    _log("bhav", d, f.url, "ok", f.label_date, f.content_date, len(rows))
    return "ok"


def load_index(d: date) -> str:
    url = f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"
    try:
        f = client.index_close(d)
    except NotPublished:
        _log("index", d, url, "missing")
        return "missing"
    except DateMismatch as e:
        _log("index", d, url, "date_mismatch", detail=str(e))
        return "date_mismatch"
    df = f.data
    rows = []
    for r in df.itertuples(index=False):
        vals = list(r)
        def n(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        rows.append((str(vals[0]).strip(), d.isoformat(), n(vals[2]), n(vals[3]), n(vals[4]), n(vals[5]),
                     n(vals[10]), n(vals[11]), n(vals[12])))
    with store.tx() as c:
        c.executemany("INSERT OR REPLACE INTO index_prices VALUES (?,?,?,?,?,?,?,?,?)", rows)
    _log("index", d, f.url, "ok", f.label_date, f.content_date, len(rows))
    return "ok"


def weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def sync_range(start: date, end: date, workers: int = 4, retry_missing_days: int = 5) -> dict:
    """Download all weekdays in [start, end] not yet loaded. Recent 'missing' days are retried."""
    stats = {"ok": 0, "missing": 0, "date_mismatch": 0, "error": 0}
    recent_cut = (end - timedelta(days=retry_missing_days)).isoformat()
    jobs = []
    for kind, fn in (("bhav", load_bhavcopy), ("index", load_index)):
        done = _done(kind)
        for d in weekdays(start, end):
            st = done.get(d.isoformat())
            if st == "ok" or (st == "missing" and d.isoformat() < recent_cut):
                continue
            jobs.append((fn, d))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, d): (fn.__name__, d) for fn, d in jobs}
        for i, fut in enumerate(as_completed(futs)):
            try:
                stats[fut.result()] += 1
            except Exception as e:  # network etc.
                stats["error"] += 1
                log.warning("sync %s %s failed: %s", *futs[fut], e)
            if i % 200 == 0 and i:
                log.info("sync progress %d/%d %s", i, len(jobs), stats)
    if jobs:
        store.checkpoint()
    return stats


def sync_all(full: bool = False) -> dict:
    today = store.now_ist().date()
    if not store.q1("SELECT 1 FROM universe LIMIT 1") or full:
        sync_universe()
    start = date.fromisoformat(HISTORY_START)
    stats = sync_range(start, today)
    from . import corp_actions
    stats["corporate_actions"] = corp_actions.sync_all()
    store.kv_set("last_sync", {"at": store.now_ist().isoformat(), "stats": stats})
    return stats


def latest_data_date() -> str | None:
    r = store.q1("SELECT MAX(date) d FROM sync_log WHERE kind='bhav' AND status='ok'")
    return r["d"] if r else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(sync_all())
