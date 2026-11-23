"""Split / bonus / consolidation price factors from NSE's corporate-actions feed (Tier 1).

NSE's sec_bhavdata_full PREV_CLOSE is NOT adjusted for corporate actions, so we derive the
adjustment factor from the announced ratio and apply it to all prices before the ex-date.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from . import store
from .nse import client

log = logging.getLogger("tt.ca")

BONUS = re.compile(r"bonus[^\d]*(\d+)\s*:\s*(\d+)", re.I)
FV = re.compile(r"(?:rs\.?|re\.?|inr)\s*([\d.]+)\s*/?-?.*?(?:to|into)\s*(?:rs\.?|re\.?|inr)\s*([\d.]+)", re.I)


def parse_factor(subject: str) -> float | None:
    """Multiplier for prices before the ex-date (0.5 = prices halve)."""
    s = subject or ""
    if re.search(r"ncrps|preference|debenture|ncd\b|warrant", s, re.I):
        return None  # not an equity share change
    f = 1.0
    m = BONUS.search(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            f *= b / (a + b)
    low = s.lower()
    if "split" in low or "sub-division" in low or "subdivision" in low or "consolidat" in low:
        m = FV.search(s)
        if m:
            old, new = float(m.group(1)), float(m.group(2))
            if old > 0 and new > 0 and old != new:
                f *= new / old
    return None if f == 1.0 else f


def sync_symbol(symbol: str) -> int:
    try:
        url, js = client.corporate_actions(symbol)
    except Exception as e:
        log.warning("corporate actions %s: %s", symbol, e)
        return -1
    rows = []
    for a in js if isinstance(js, list) else []:
        f = parse_factor(a.get("subject", ""))
        if f is None:
            continue
        try:
            ex = datetime.strptime(a["exDate"].strip(), "%d-%b-%Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        rows.append((symbol, ex, a.get("subject", "").strip(), f, url, store.now_ist().isoformat()))
    with store.tx() as c:
        c.execute("DELETE FROM corp_actions WHERE symbol=?", (symbol,))
        c.executemany("INSERT OR REPLACE INTO corp_actions VALUES (?,?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (f"ca_synced:{symbol}", '"' + store.now_ist().date().isoformat() + '"',
                                                                 store.now_ist().isoformat()))
    return len(rows)


def sync_all(symbols: list[str] | None = None, max_age_days: int = 7) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    symbols = symbols or [r["symbol"] for r in store.q("SELECT symbol FROM universe")]
    today = store.now_ist().date()
    todo = []
    for s in symbols:
        last = store.kv_get(f"ca_synced:{s}")
        if not last or (today - datetime.fromisoformat(last).date()).days >= max_age_days:
            todo.append(s)
    with ThreadPoolExecutor(max_workers=3) as ex:
        res = list(ex.map(sync_symbol, todo))
    return {"symbols": len(todo), "actions": sum(r for r in res if r > 0), "errors": sum(1 for r in res if r < 0)}


def factors() -> list[dict]:
    return store.q("SELECT symbol, ex_date, factor, subject, url FROM corp_actions")
