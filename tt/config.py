"""Central configuration. Every threshold used by the system lives here so it can be audited."""
from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=value per line). Real environment variables win."""
    try:
        for line in path.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv(ROOT / ".env")

# Password for the web app. When set, every page and API call needs HTTP Basic login (any username).
APP_PASSWORD = os.environ.get("TT_PASSWORD", "")
DATA_DIR = Path(os.environ.get("TT_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "tracker.db"
REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
WEB_DIR = ROOT / "web"

IST = timezone(timedelta(hours=5, minutes=30))

# History start. NSE's sec_bhavdata_full (prices + delivery %) is available from Jan 2020.
HISTORY_START = "2020-01-01"
# Cloud hosts with small, wiped-on-restart disks keep only recent history (enough for the daily scan,
# which needs ~560 calendar days). Full backtests need the full history and run locally.
HISTORY_DAYS = int(os.environ.get("TT_HISTORY_DAYS", "0") or 0)   # 0 = full history from HISTORY_START
CLOUD = bool(os.environ.get("RENDER") or os.environ.get("TT_CLOUD"))
SEED_DIR = ROOT / "seed"

# Bhavcopy is normally published ~18:00 IST. Daily scan runs after this time.
DAILY_SCAN_HOUR_IST = 18
DAILY_SCAN_MINUTE_IST = 45

# ---------------------------------------------------------------- source trust
SOURCE_TIERS = {
    1: {"label": "Official / primary", "score": 1.0},
    2: {"label": "Reputable press", "score": 0.8},
    3: {"label": "Secondary / derived", "score": 0.5},
    4: {"label": "Blocked", "score": 0.0},
}

TIER1_DOMAINS = [
    "nseindia.com", "nsearchives.nseindia.com", "nsearchives.nseindia.in", "niftyindices.com",
    "bseindia.com", "sebi.gov.in", "rbi.org.in", "amfiindia.com",
    "crisil.com", "crisilratings.com", "icra.in", "careratings.com", "indiaratings.co.in",
    "incometaxindia.gov.in",
]
TIER2_DOMAINS = [
    "reuters.com", "bloomberg.com", "business-standard.com", "economictimes.indiatimes.com",
    "livemint.com", "thehindubusinessline.com", "financialexpress.com", "moneycontrol.com",
]
TIER3_DOMAINS = [
    "screener.in", "trendlyne.com", "tickertape.in", "zerodha.com", "angelone.in", "kotakneo.com",
]

CONFIDENCE_USE = 0.85       # >= this: used as fact
CONFIDENCE_RECHECK = 0.60   # between RECHECK and USE: re-verify; below RECHECK: discard
CONFIRM_BONUS = 0.10        # per independent confirming source
CONFIRM_BONUS_MAX = 0.20
CONFLICT_PENALTY = 0.30
STALE_PENALTY = 0.20
NEWS_MAX_AGE_DAYS = 7

DEEP_CHECK_BUDGET_S = float(os.environ.get("TT_DEEP_CHECK_BUDGET_S", "25") or 25)
DEEP_CHECK_TIMEOUT_REASON = "Company checks did not finish in time (not verified). Check NSE surveillance and filings manually"

# ---------------------------------------------------------------- portfolio / risk
DEFAULT_CAPITAL = 500_000.0
RISK_PER_TRADE = 0.0075          # 0.75% of capital at risk to the stop
MAX_POSITION_PCT = 0.10          # max 10% of capital in one stock
MAX_OPEN_POSITIONS = 8
MAX_SECTOR_POSITIONS = 2
MAX_NEW_ENTRIES_PER_MONTH = 8

# ---------------------------------------------------------------- universe gates
MIN_MEDIAN_TURNOVER_CR = 10.0    # 20-day median traded value, Rs crore
MIN_PRICE = 50.0
MAX_ABOVE_50DMA = 0.25           # don't chase: price more than 25% over 50-DMA is rejected
NEAR_52W_HIGH = 0.85             # price >= 85% of 52-week high
RESULTS_BLACKOUT_DAYS = 3        # no entry within N calendar days before a results board meeting

# ---------------------------------------------------------------- regime gate
REGIME_INDEX = "Nifty 500"
BREADTH_ON = 0.50                # > 50% of universe above own 200-DMA
REGIME_PARTIAL_SIZE = 0.5

# ---------------------------------------------------------------- trade plan
ATR_PERIOD = 14
STOP_ATR = 2.5
TRAIL_ATR = 3.0
TARGET1_R = 1.5
TARGET2_R = 3.0
BUY_ZONE_ATR = 0.5               # buy zone: close - 0.25 ATR .. close + 0.25 ATR (width 0.5 ATR)
CHASE_ATR = 1.0                  # do not chase above close + 1 ATR
TIME_EXIT_DAYS = 45              # calendar-ish: trading days
REVIEW_DAYS = 15
T1_BOOK_FRACTION = 1 / 3
AVERAGE_MAX_TRANCHES = 1

# ---------------------------------------------------------------- costs & tax (FY 2026-27)
COST_STT = 0.001                 # each side, delivery
COST_STAMP_BUY = 0.00015
COST_EXCH = 0.0000307            # NSE txn charge approx per side
COST_SEBI = 0.000001             # Rs 10 per crore
COST_GST = 0.18                  # on brokerage + exch + sebi
COST_DP_PER_SELL = 15.34         # Rs per scrip per sell day
BROKERAGE_PER_ORDER = 0.0
SLIPPAGE = 0.001                 # 0.10% per side (backtest assumption)
STCG_RATE = 0.20
LTCG_RATE = 0.125

# ---------------------------------------------------------------- optional LLM
LLM_PROVIDER = os.environ.get("TT_LLM_PROVIDER", "").lower()   # "gemini" | "groq" | "ollama" | ""
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("TT_GEMINI_MODEL", "gemini-2.5-flash")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("TT_GROQ_MODEL", "llama-3.3-70b-versatile")
OLLAMA_URL = os.environ.get("TT_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("TT_OLLAMA_MODEL", "llama3.1")
