"""Price panel (dates x symbols), corporate-action adjustment and indicators."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import store
from .config import ATR_PERIOD, REGIME_INDEX

CA_TOLERANCE = 0.005  # prev_close differs from prior close by > 0.5% => corporate action adjustment


@dataclass
class Panel:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    turnover_cr: pd.DataFrame
    deliv_per: pd.DataFrame
    raw_close: pd.DataFrame
    industry: pd.Series
    names: pd.Series
    index: pd.DataFrame  # columns: index names, close values
    ind: dict = field(default_factory=dict)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index


def _wide(df: pd.DataFrame, col: str) -> pd.DataFrame:
    return df.pivot(index="date", columns="symbol", values=col).sort_index()


PRICE_COLS = "symbol, date, series, open, high, low, close, volume, turnover_lacs, deliv_per"
_mem_cache: dict = {}


def _sql_prices(symbols: list[str]) -> pd.DataFrame:
    ph = ",".join("?" * len(symbols))
    df = pd.read_sql_query(f"SELECT {PRICE_COLS} FROM prices WHERE symbol IN ({ph})", store.conn(), params=symbols)
    df["date"] = pd.to_datetime(df["date"])
    # if a symbol traded in two series on one day keep the EQ row
    return df.sort_values(["symbol", "date", "series"]).drop_duplicates(["symbol", "date"], keep="first")


def _universe_prices() -> pd.DataFrame:
    """Universe prices, cached on disk and in memory; rebuilt whenever a new NSE file is loaded."""
    from .config import DATA_DIR
    key_row = store.q1("SELECT COUNT(*) n, MAX(fetched_at) f FROM sync_log WHERE kind='bhav' AND status='ok'")
    uni_row = store.q1("SELECT COUNT(*) n, MAX(fetched_at) f FROM universe")
    key = f"{key_row['n']}|{key_row['f']}|{uni_row['n']}|{uni_row['f']}"
    if _mem_cache.get("key") == key:
        return _mem_cache["df"]
    path = DATA_DIR / "price_cache.pkl"
    try:
        cached = pd.read_pickle(path)
        if cached.get("key") == key:
            _mem_cache.update(cached)
            return cached["df"]
    except Exception:
        pass
    df = pd.read_sql_query(f"SELECT {PRICE_COLS} FROM prices WHERE symbol IN (SELECT symbol FROM universe)", store.conn())
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date", "series"]).drop_duplicates(["symbol", "date"], keep="first")
    df = df.reset_index(drop=True)
    payload = {"key": key, "df": df}
    try:
        pd.to_pickle(payload, path)
    except Exception:
        pass
    _mem_cache.update(payload)
    return df


def load_panel(symbols: list[str] | None = None, start: str | None = None) -> Panel:
    uni = store.q("SELECT symbol,name,industry FROM universe")
    if symbols is None:
        symbols = [u["symbol"] for u in uni]
    meta = {u["symbol"]: u for u in uni}
    df = _universe_prices()
    extra = [s for s in symbols if s not in meta]
    if extra:
        df = pd.concat([df, _sql_prices(extra)], ignore_index=True)
    df = df[df["symbol"].isin(set(symbols))]
    if start:
        df = df[df["date"] >= pd.Timestamp(start)]
    if df.empty:
        raise RuntimeError("No price data loaded. Run a data sync first.")

    wide = df.pivot(index="date", columns="symbol",
                    values=["open", "high", "low", "close", "volume", "turnover_lacs", "deliv_per"]).sort_index()
    o, h, l, c = (wide[k] for k in ("open", "high", "low", "close"))
    vol = wide["volume"]
    to = wide["turnover_lacs"] / 100.0
    dp = wide["deliv_per"]

    # ---- corporate action adjustment (NSE corporate-actions feed; bhavcopy PREV_CLOSE is unadjusted)
    factor = pd.DataFrame(1.0, index=c.index, columns=c.columns)
    # several actions on one ex-date (e.g. split + bonus) combine into one factor
    for a in store.q("SELECT symbol, ex_date, EXP(SUM(LN(factor))) factor FROM corp_actions GROUP BY symbol, ex_date"):
        if a["symbol"] not in factor.columns:
            continue
        k = c.index.searchsorted(pd.Timestamp(a["ex_date"]))
        if 0 < k < len(c.index):
            j = factor.columns.get_loc(a["symbol"])
            prev = c.iloc[:k, j].dropna()
            ratio = c.iat[k, j] / prev.iat[-1] if len(prev) and pd.notna(c.iat[k, j]) else None
            # second check: the actual price move on the ex-date must confirm the announced ratio
            if ratio is not None and 0.6 * a["factor"] <= ratio <= 1.6 * a["factor"]:
                factor.iat[k, j] *= a["factor"]
    cum = factor[::-1].cumprod()[::-1].shift(-1).fillna(1.0)  # multiply earlier prices
    adj = lambda x: x * cum  # noqa: E731

    idx = pd.DataFrame(store.q("SELECT name,date,close FROM index_prices"))
    if not idx.empty:
        idx["date"] = pd.to_datetime(idx["date"])
        idx = idx.pivot(index="date", columns="name", values="close").sort_index()

    names = pd.Series({s: meta.get(s, {}).get("name", s) for s in c.columns})
    industry = pd.Series({s: meta.get(s, {}).get("industry", "Unknown") for s in c.columns})
    return Panel(adj(o), adj(h), adj(l), adj(c), vol / cum, to, dp, c, industry, names, idx)


# ------------------------------------------------------------------ indicators
def atr(p: Panel, n: int = ATR_PERIOD) -> pd.DataFrame:
    prev = p.close.shift(1)
    tr = pd.concat([(p.high - p.low), (p.high - prev).abs(), (p.low - prev).abs()]).groupby(level=0).max()
    tr = tr.reindex(p.close.index)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_indicators(p: Panel) -> dict:
    c = p.close
    ind: dict = {}
    ind["sma20"] = c.rolling(20, min_periods=20).mean()
    ind["sma50"] = c.rolling(50, min_periods=50).mean()
    ind["sma200"] = c.rolling(200, min_periods=200).mean()
    ind["atr"] = atr(p)
    ind["hi252_prev"] = p.high.rolling(252, min_periods=200).max().shift(1)
    ind["hi252"] = p.high.rolling(252, min_periods=200).max()
    ind["lo20"] = p.low.rolling(20, min_periods=10).min()
    ind["vol20"] = p.volume.rolling(20, min_periods=15).mean().shift(1)
    ind["vol_ratio"] = p.volume / ind["vol20"]
    ind["deliv20"] = p.deliv_per.rolling(20, min_periods=10).mean().shift(1)
    ind["deliv_ratio"] = p.deliv_per / ind["deliv20"]
    ind["med_turnover"] = p.turnover_cr.rolling(20, min_periods=15).median()
    ind["rsi14"] = rsi(c, 14)
    ind["rsi2"] = rsi(c, 2)
    ret = c.pct_change(fill_method=None)
    ind["vol252"] = ret.rolling(252, min_periods=200).std() * np.sqrt(252)
    m12 = c.shift(21) / c.shift(252) - 1
    m6 = c.shift(21) / c.shift(126) - 1
    ind["mom12_1"], ind["mom6_1"] = m12, m6
    ind["mom_score"] = ((m12 + m6) / 2) / ind["vol252"].replace(0, np.nan)
    ind["ret63"] = c / c.shift(63) - 1
    ind["ret21"] = c / c.shift(21) - 1
    # sector strength: mean 3-month return of the industry's members, ranked 0..1 across industries
    sec = ind["ret63"].T.groupby(p.industry).mean().T
    sec_rank = sec.rank(axis=1, pct=True)
    ind["sector_rank"] = pd.DataFrame({s: sec_rank.get(p.industry[s]) for s in c.columns}, index=c.index)
    ind["above200"] = c > ind["sma200"]
    valid = ind["sma200"].notna()
    ind["breadth"] = ind["above200"].where(valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    p.ind = ind
    return ind


def regime_series(p: Panel) -> pd.DataFrame:
    """Daily regime: ON (index > 200DMA and breadth > 50%), PARTIAL (one of them), OFF (neither)."""
    from .config import BREADTH_ON
    if REGIME_INDEX in p.index.columns:
        ix = p.index[REGIME_INDEX].reindex(p.dates).ffill()
    else:  # fallback: equal-weight of universe
        ix = (p.close.pct_change(fill_method=None).mean(axis=1).fillna(0) + 1).cumprod()
    ix200 = ix.rolling(200, min_periods=200).mean()
    breadth = p.ind["breadth"]
    a = ix > ix200
    b = breadth > BREADTH_ON
    state = np.where(a & b, "ON", np.where(a | b, "PARTIAL", "OFF"))
    out = pd.DataFrame({"index": ix, "index_200dma": ix200, "breadth": breadth, "state": state}, index=p.dates)
    out.loc[ix200.isna(), "state"] = "UNKNOWN"
    return out
