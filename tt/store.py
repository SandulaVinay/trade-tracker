"""SQLite storage."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH, IST

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT, date TEXT, series TEXT, open REAL, high REAL, low REAL, close REAL, prev_close REAL,
    volume REAL, turnover_lacs REAL, trades REAL, deliv_qty REAL, deliv_per REAL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_prices_date ON prices(date);
CREATE TABLE IF NOT EXISTS index_prices (
    name TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, pe REAL, pb REAL, dy REAL,
    PRIMARY KEY (name, date)
);
CREATE TABLE IF NOT EXISTS sync_log (
    kind TEXT, date TEXT, url TEXT, label_date TEXT, content_date TEXT, fetched_at TEXT,
    status TEXT, rows INTEGER, detail TEXT, PRIMARY KEY (kind, date)
);
CREATE TABLE IF NOT EXISTS universe (
    symbol TEXT PRIMARY KEY, name TEXT, industry TEXT, isin TEXT, source_url TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS corp_actions (
    symbol TEXT, ex_date TEXT, subject TEXT, factor REAL, url TEXT, fetched_at TEXT,
    PRIMARY KEY (symbol, ex_date, subject)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, data_date TEXT, run_at TEXT, status TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, signal_date TEXT, symbol TEXT, name TEXT,
    strategy TEXT, status TEXT, mode TEXT DEFAULT 'paper', plan TEXT,
    entry_date TEXT, entry_price REAL, qty INTEGER, qty_open INTEGER,
    stop REAL, trail_stop REAL, highest REAL, t1_done INTEGER DEFAULT 0, averaged INTEGER DEFAULT 0,
    exit_date TEXT, exit_price REAL, exit_reason TEXT, realized REAL DEFAULT 0, charges REAL DEFAULT 0,
    last_price REAL, last_date TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS call_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, call_id INTEGER, date TEXT, kind TEXT, message TEXT,
    price REAL, qty INTEGER
);
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY, added_at TEXT, kind TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, strategy TEXT, params TEXT, metrics TEXT,
    trades TEXT
);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, role TEXT, text TEXT, attachment TEXT
);
"""


def clean(o):
    """Replace NaN/inf with None recursively (JSON has no NaN; the API would fail on it)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):  # numpy scalars
        try:
            return clean(o.item())
        except Exception:
            return o
    return o


def dumps(o) -> str:
    return json.dumps(clean(o), default=str)


def loads(s: str):
    return clean(json.loads(s))


def now_ist() -> datetime:
    return datetime.now(IST)


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA journal_size_limit=67108864")
        c.executescript(SCHEMA)
        _local.c = c
    return c


_write_lock = threading.RLock()


@contextmanager
def tx():
    """Serialised write transaction (downloads run in parallel, writes do not)."""
    c = conn()
    with _write_lock:
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def checkpoint() -> None:
    """Fold the write-ahead log back into the database file and truncate it."""
    with _write_lock:
        conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")


def q(sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def kv_get(key: str, default=None):
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    return loads(r["value"]) if r else default


def kv_set(key: str, value) -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                  (key, dumps(value), now_ist().isoformat()))
