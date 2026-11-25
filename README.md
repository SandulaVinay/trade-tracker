# Trade Tracker: rules-based NSE swing picks with a paper-trading tracker

A web app that runs every trading evening. It:

1. **Downloads official NSE files**: the bhavcopy with delivery %, index closes, F&O, surveillance lists and company filings. It accepts a file only if the **date in its name equals the date inside it** and equals the expected trading day (the two-date check).
2. **Checks the market regime.** Nifty 500 vs its 200-DMA, plus breadth: the share of Nifty 500 stocks above their own 200-DMA.
3. **Applies the robust strategy chosen by the backtest** through these gates: liquidity, trend, 52-week high, no-chase, results blackout, ASM/GSM, F&O ban, red-flag filings, and a second-source price check.
4. **Suggests one stock** for the next session, or says **"No trade today"** and explains why.
5. **Gives a full plan:**
   - buy zone and no-chase price;
   - stop and its reason;
   - T1/T2 with backtested days-to-target;
   - position size;
   - what to do if it rises;
   - what to do if it falls: cause analysis and averaging rules;
   - the time exit.
6. **Shows every fact** with its date, source link and confidence score, on screen and in a PDF.
7. **Tracks every call as a paper trade:** fills, stops, targets, trailing stop, time exit and averaging alerts, so you can compare live results with the backtest.

Analysis from published rules and NSE data. Not investment advice, not SEBI-registered research. You make the final decision.

**Live copy:** https://trade-tracker-nse.onrender.com (password-protected; see [Login](#login)).

## Run it locally

```powershell
pip install -r requirements.txt
python -m tt.app
```

Open http://localhost:8000.

The first run downloads NSE history from January 2020: about 3,600 files plus corporate actions, which takes 30–60 minutes. You can start it from the app, or run it directly:

```powershell
python -m tt.data_sync
```

Then open **Strategy Lab → Run backtests**. The app picks the robust winner and uses it for the daily scans.

While the app runs, the daily scan starts by itself after **18:45 IST** on NSE trading days. **Sync + scan now** runs it any time.

The local copy keeps everything in `data/` (SQLite, price cache, PDFs), so your paper-trade record survives restarts. Use the local copy for a lasting track record.

## Login

Set `TT_PASSWORD` and every page and API call asks for a password; **any username** works. Only `/healthz` stays open.

- **Locally:** put `TT_PASSWORD=...` in a `.env` file in the repo folder. It is git-ignored. Leave it unset for no login.
- **On Render:** see `TT_PASSWORD` under the service's **Environment** tab (click the eye icon).

## Deploy on Render (how the live copy is set up)

The live copy is a regular **free Web Service**. Render's *Blueprint* flow asks for a payment card, so it wasn't used; `render.yaml` documents the same settings for anyone who prefers it.

| Setting | Value |
|---|---|
| Repo / branch | `SandulaVinay/trade-tracker` / `rebuild/quant-trade-tracker` |
| Language | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `python -m tt.app` (binds `0.0.0.0:$PORT`) |
| Instance type | Free ($0/month) |
| Health check path | `/healthz` |
| Env vars | `TT_PASSWORD` (Render **Generate**), `PYTHON_VERSION=3.12.10`, `TT_HISTORY_DAYS=560`, `TT_DATA_DIR=/tmp/tt-data` |

**On first start** the server:
- downloads about 18 months of NSE history (enough for the daily scan), about 11 minutes;
- loads backtest results from `seed/backtests.json` (produced on full local history);
- runs the scan.

**NSE access from Render works.** On 24 Nov 2026 all 760 history files and the corporate-actions API for 501 stocks downloaded with 0 errors. After logging in, `/api/nse-check` re-tests it.

**Free-tier limits:**
- The service sleeps after about 15 minutes without visits, and the next visit takes up to a minute to wake it.
- The disk is wiped on every sleep, restart or deploy. History is re-downloaded on wake, and **paper-trade records on the live copy are lost**.
- The 18:45 IST scan only runs if the service is awake at that time.
- Full backtests need full history, so the server refuses to run them. Run them locally, then re-export the seed (see `HANDOFF.md`).
- For persistence, use a paid instance with a disk and point `TT_DATA_DIR` at it.

## Tabs

| Tab | What it does |
|---|---|
| Today | Regime, breadth chart, the pick (or why there is none), candidates checked and why any were rejected, PDF button |
| Calls | Paper track record (win rate, expectancy, P&L after charges), open/planned calls, old calls and a per-call timeline. You can log a manual buy/sell, one averaging tranche, or mark a call as a live trade |
| Watchlist | Your list; **upcoming** stocks (within 3% of a 52-week-high breakout); **stocks to average** (open positions below entry, with the cause of the fall and the verdict); today's signals by strategy |
| Strategy Lab | All rule sets backtested for the full period, 2021–23 and 2024–now. Includes ±20% robustness, idle-cash scenario, equity curve, yearly returns, exit reasons, and an honest comparison with Nifty 500 buy-and-hold |
| Chat | `analyse TCS`, `pdf today`, `pdf TCS`, `average`, `upcoming`, `regime`, `holdings`, `watch X`, `bought 12 at 1450`, `help` |
| Data & Sources | Sync log, the files rejected by the two-date check, and source tiers |

## Trust rules

- **Tier 1 (score 1.0):** NSE files and filings, SEBI, RBI, AMFI, NSE Indices, rating agencies. Only Tier 1 data sets prices and trade levels.
- **Tier 2 (0.8):** Reuters, Bloomberg, Business Standard, ET, Mint, BusinessLine, FE, Moneycontrol. Used for context only.
- **Tier 3/4:** aggregators, social media, Telegram, YouTube. These are dropped.
- **Score:** a fact scoring ≥ 0.85 is used. Between 0.60 and 0.85 it is marked *unverified* and not used for decisions. Below 0.60 it is discarded. An independent confirmation adds 0.1; a conflict subtracts 0.3; stale data subtracts 0.2.
- **Background facts:** an official filing older than the 7-day news window is still true, but it is marked *background* and not used for today's decision. The exception: a red-flag filing in the last 90 days still blocks a buy.
- **Asymmetric rule:** unverified bad news can block a buy, but unverified good news can never trigger one.
- **No pick on stale data:** if the latest trading day's NSE files are missing or fail the two-date check, you get "No pick today: fresh data not confirmed". The last verified scan is shown only for reference, clearly labelled.
- **Second-source price check:** the pick's close must match NSE's separate UDiFF file within 0.5%.
- **Splits and bonuses** come from NSE's corporate-actions feed, because bhavcopy `PREV_CLOSE` is *not* adjusted. A factor is applied only when the actual price move on the ex-date confirms it.

## Optional AI (free)

The chat works without any AI. To add plain-language summaries, set one of these, in the environment or in `.env`:

```powershell
$env:TT_LLM_PROVIDER="gemini"; $env:GEMINI_API_KEY="..."   # Google AI Studio free tier
```

```powershell
$env:TT_LLM_PROVIDER="groq";   $env:GROQ_API_KEY="..."     # Groq free tier
```

```powershell
$env:TT_LLM_PROVIDER="ollama"                              # local, fully free
```

The AI only rewrites the verified data it is given. Any answer that contains a number not present in that data is **rejected**.

## How the strategy is chosen

Every rule set in the catalog is backtested on NSE data. Signals start in January 2021, because they need 252 sessions of history.

**Backtest rules:**
- The signal fires at the close; the stock is bought at the next open, and skipped if it opens above the no-chase price.
- Stop at 2.5×ATR.
- Book ⅓ at T1 (1.5R) and ⅓ at T2 (3R), then trail the rest at 3×ATR.
- Time exit on day 45.

**Costs and tax:**
- STT, stamp duty, exchange charges, SEBI fee, GST and DP charges.
- 0.10% slippage per side.
- 20% STCG per financial year.

A rule set qualifies only if it meets all of these:
- at least 30 trades;
- positive expectancy **in both** 2021–23 and 2024–now;
- positive returns with stops and trailing stops changed by ±20%.

Among the qualifiers, the app picks the one with the best **worst-period** return after costs and tax.

## What the backtest found (NSE data to 23 Nov 2026)

| | S3 pullback (active) | S2 breakout | S1 monthly momentum | Nifty 500 buy-and-hold |
|---|---|---|---|---|
| Trades | 299 | 334 | 302 | – |
| Win rate | 55% | 48% | 38% | – |
| Expectancy per trade | +2.9% | +1.5% | +1.8% | – |
| CAGR after costs and 20% STCG | 7.8% | 4.2% | 6.5% | 13.0% (price index) |
| CAGR, idle cash earning ~5% | 11.1% | 7.3% | 10.2% | – |
| Max drawdown | −21% | −24% | −23% | −19% |
| 2024–now CAGR | 4.7% | 5.4% | −1.6% | 7.1% |

**No rule set beat simply holding the Nifty 500** over 2021–2026 after costs and tax. That matches the research plan: keep most money in an index core and run this as a small, rule-based satellite. The app shows this comparison in the Strategy Lab.

## Configuration

All thresholds live in `tt/config.py`. The main defaults:
- capital ₹5,00,000;
- risk 0.75% of capital per trade, max 10% of capital in one stock;
- max 8 open positions;
- regime PARTIAL halves the position size.

Environment variables:

| Variable | Purpose |
|---|---|
| `TT_PASSWORD` | Enables login (any username) |
| `TT_DATA_DIR` | Where the SQLite database, price cache and PDFs live (default `data/`) |
| `TT_HISTORY_DAYS` | Keep only this many days of history (cloud); `0` means full history from 2020 |
| `PORT`, `TT_HOST` | Server port and host (binds `0.0.0.0` when `PORT` is set) |
| `TT_NO_SCHEDULER` | Turn off the background daily scan and the first-start download (used by tests) |
| `TT_LLM_PROVIDER`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `TT_OLLAMA_URL`, `TT_OLLAMA_MODEL` | Optional AI |

## Project layout

```
tt/         backend (FastAPI app, NSE client, sync, market panel, strategies, backtest,
            signals, trade plan, tracker, company checks, confidence scoring, PDF, chat, LLM)
web/        single-page frontend (vanilla JS, no build step)
tests/      test_core.py (calculations) + test_api.py (offline end-to-end API/tracker)
seed/       backtests.json: backtest results from full local history, loaded on fresh servers
scripts/    export_seed.py: re-export seed/backtests.json after running backtests locally
render.yaml documented Render settings
HANDOFF.md  project status, decisions, open items and next steps
```

## Known limits

- History starts in January 2020 (NSE's delivery-data file format). There is no 2008-type crash in the sample.
- The backtest universe is today's Nifty 500 (survivorship bias, which makes results look better).
- The benchmark is the Nifty 500 price index, without dividends.
- Demergers (e.g. Siemens, Vedanta) are not price-adjusted, because NSE publishes no ratio for them.
- The averaging rule and the F&O open-interest context come from the research plan and are **not backtested**.
- Quarterly results come from NSE integrated filings (XBRL). Banks and some companies may show "data missing".

## Tests

```powershell
python -m pytest -q
```

26 tests: core calculations (charges, tax, trade-plan levels, expiry, confidence, corporate actions, price cross-check, password gate, synthetic backtest) and offline end-to-end API and tracker flows. The tests never touch the network or the real database.
