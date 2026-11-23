"""NSE client: Tier-1 data only (nsearchives.nseindia.com files and www.nseindia.com JSON APIs).

Every fetch returns the URL it came from so the UI and PDF can link to the source.
"""
from __future__ import annotations

import io
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
ARCHIVE = "https://nsearchives.nseindia.com"
SITE = "https://www.nseindia.com"


class NotPublished(Exception):
    """File for this date does not exist (holiday, or not yet published)."""


class DateMismatch(Exception):
    """Label date and content date disagree: refuse to use the file."""


@dataclass
class Fetched:
    url: str
    fetched_at: datetime
    label_date: date | None
    content_date: date | None
    data: object


class NSEClient:
    def __init__(self, timeout: int = 30, min_interval: float = 0.35):
        self.timeout = timeout
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self._session = None
        self._session_at = 0.0

    # ------------------------------------------------------------ session
    def _sess(self) -> requests.Session:
        if self._session is None or time.time() - self._session_at > 240:
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept": "application/json,text/html,*/*",
                              "Accept-Language": "en-US,en;q=0.9"})
            try:
                s.get(SITE, timeout=self.timeout)
            except requests.RequestException:
                pass
            self._session, self._session_at = s, time.time()
        return self._session

    def _throttle(self):
        with self._lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def get(self, url: str, retries: int = 3, api: bool = False) -> requests.Response:
        err = None
        for attempt in range(retries):
            self._throttle()
            try:
                s = self._sess()
                r = s.get(url, timeout=self.timeout,
                          headers={"Referer": SITE + "/"} if api else None)
                if r.status_code == 404:
                    raise NotPublished(url)
                if r.status_code in (401, 403):
                    self._session = None  # refresh cookies
                    raise requests.HTTPError(f"{r.status_code} for {url}")
                r.raise_for_status()
                if not api and r.content[:15].lower().startswith(b"<!doctype html"):
                    raise NotPublished(url)
                return r
            except NotPublished:
                raise
            except requests.RequestException as e:
                err = e
                time.sleep(1.5 * (attempt + 1))
        raise err  # type: ignore[misc]

    def api(self, path: str):
        url = f"{SITE}/api/{path}"
        r = self.get(url, api=True)
        return url, r.json()

    # ------------------------------------------------------------ archive files
    def bhavcopy(self, d: date) -> Fetched:
        """Full bhavcopy with delivery data. Two-date check: filename date == DATE1 column."""
        url = f"{ARCHIVE}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
        r = self.get(url)
        df = pd.read_csv(io.BytesIO(r.content), skipinitialspace=True)
        df.columns = [c.strip() for c in df.columns]
        for c in df.select_dtypes("object"):
            df[c] = df[c].astype(str).str.strip()
        dates = pd.to_datetime(df["DATE1"], format="%d-%b-%Y").dt.date.unique()
        if len(dates) != 1 or dates[0] != d:
            raise DateMismatch(f"bhavcopy {d}: content dates {list(dates)}")
        return Fetched(url, datetime.now(), d, dates[0], df)

    def index_close(self, d: date) -> Fetched:
        url = f"{ARCHIVE}/content/indices/ind_close_all_{d:%d%m%Y}.csv"
        r = self.get(url)
        df = pd.read_csv(io.BytesIO(r.content))
        df.columns = [c.strip() for c in df.columns]
        dates = pd.to_datetime(df["Index Date"], format="%d-%m-%Y").dt.date.unique()
        if len(dates) != 1 or dates[0] != d:
            raise DateMismatch(f"index file {d}: content dates {list(dates)}")
        return Fetched(url, datetime.now(), d, dates[0], df)

    def fo_ban(self, d: date) -> Fetched:
        url = f"{ARCHIVE}/archives/fo/sec_ban/fo_secban_{d:%d%m%Y}.csv"
        r = self.get(url)
        lines = r.text.strip().splitlines()
        head = lines[0] if lines else ""
        content_date = None
        try:
            content_date = datetime.strptime(head.split("Trade Date")[1].strip(" :"), "%d-%b-%Y").date()
        except Exception:
            pass
        if content_date != d:
            raise DateMismatch(f"fo ban {d}: header '{head}'")
        syms = [ln.split(",", 1)[1].strip() for ln in lines[1:] if "," in ln]
        return Fetched(url, datetime.now(), d, content_date, syms)

    def fo_bhavcopy(self, d: date) -> Fetched:
        url = f"{ARCHIVE}/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
        r = self.get(url)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]))
        dates = pd.to_datetime(df["TradDt"]).dt.date.unique()
        if len(dates) != 1 or dates[0] != d:
            raise DateMismatch(f"fo bhavcopy {d}: content dates {list(dates)}")
        return Fetched(url, datetime.now(), d, dates[0], df)

    def cm_udiff(self, d: date) -> Fetched:
        """Second, independent NSE cash-market file (UDiFF format) used to cross-check prices."""
        url = f"{ARCHIVE}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
        r = self.get(url)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]))
        dates = pd.to_datetime(df["TradDt"]).dt.date.unique()
        if len(dates) != 1 or dates[0] != d:
            raise DateMismatch(f"cm udiff {d}: content dates {list(dates)}")
        return Fetched(url, datetime.now(), d, dates[0], df)

    def nifty500_list(self) -> Fetched:
        url = f"{ARCHIVE}/content/indices/ind_nifty500list.csv"
        r = self.get(url)
        df = pd.read_csv(io.BytesIO(r.content))
        df.columns = [c.strip() for c in df.columns]
        return Fetched(url, datetime.now(), None, None, df)

    # ------------------------------------------------------------ JSON APIs
    def holidays(self):
        url, js = self.api("holiday-master?type=trading")
        out = set()
        for row in js.get("CM", []):
            out.add(datetime.strptime(row["tradingDate"], "%d-%b-%Y").date())
        return url, out

    def announcements(self, symbol: str):
        return self.api(f"corporate-announcements?index=equities&symbol={requests.utils.quote(symbol)}")

    def event_calendar(self):
        return self.api("event-calendar?index=equities")

    def asm(self):
        return self.api("reportASM")

    def gsm(self):
        return self.api("reportGSM")

    def pledge(self, symbol: str):
        return self.api(f"corporate-pledgedata?index=equities&symbol={requests.utils.quote(symbol)}")

    def financial_results(self, symbol: str):
        return self.api(f"corporates-financial-results?index=equities&symbol={requests.utils.quote(symbol)}&period=Quarterly")

    def corporate_actions(self, symbol: str):
        return self.api(f"corporates-corporateActions?index=equities&symbol={requests.utils.quote(symbol)}")

    def fii_dii(self):
        return self.api("fiidiiTradeReact")

    def xbrl(self, url: str) -> bytes:
        return self.get(url).content


client = NSEClient()
