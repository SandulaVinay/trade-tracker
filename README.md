# Trade Tracker: rules-based NSE swing picks with a paper-trading tracker

A local web app that, every trading evening:

1. Downloads the day's **official NSE files** (bhavcopy with delivery %, index closes) and accepts each file only if the **date in its name equals the date inside it** (two-date check).
2. Checks the **market regime**: Nifty 500 vs its 200-DMA, and breadth (the share of Nifty 500 stocks above their own 200-DMA).
3. Runs the **backtest-selected strategy** through these gates: liquidity, trend, 52-week high, no-chase, results blackout, ASM/GSM, F&O ban, and red-flag filings.
4. Suggests **one stock** for the next session, or says **"No trade today"** and explains why.
5. Gives a full plan: buy zone, no-chase price, stop and its reason, T1/T2 with backtested days-to-target, position size, what to do if it rises, what to do if it falls (cause analysis and averaging rules), and the time exit.
6. Shows **every fact with its date, source link and confidence score**.
7. Tracks every call as a **paper trade** (fills, stops, targets, trailing stop, time exit, averaging alerts) so you can compare live results with the backtest.

Analysis from published rules and NSE data. Not investment advice, not SEBI-registered research. You make the final decision.

## Run it

```powershell
pip install -r requirements.txt
python -m tt.app
```

Open http://localhost:8000.

The first run downloads NSE history from January 2020. That is about 3,600 files and takes 30–60 minutes. You can start it from the app, or run it directly:

```powershell
python -m tt.data_sync
```

Then open **Strategy Lab → Run backtests**. The app picks the robust winner and uses it for the daily scans.

While the app is running, the daily scan runs by itself after **18:45 IST** on NSE trading days. You can also click **Sync + scan now**. Your PC must be on and the app running at that time; NSE blocks most cloud servers, so this is designed to run locally.

## Tabs

| Tab | What it does |
|---|---|
| Today | Regime, breadth chart, the pick (or why there is none), candidates checked and why they were rejected, PDF button |
| Calls | Track record (win rate, expectancy, P&L after charges), open/planned calls, old calls, per-call timeline. You can log actions such as a manual buy/sell, averaging once, or marking a call as a live trade |
| Watchlist | Your list, **upcoming** stocks (within 3% of a 52-week-high breakout), **stocks to average** (open positions below entry, with the cause of the fall and the verdict), and today's signals by strategy |
| Strategy Lab | All rule sets backtested. Results cover the full period, 2021–23 and 2024–now, plus ±20% parameter robustness, an equity curve, yearly returns and exit reasons |
| Chat | `analyse TCS`, `pdf today`, `pdf TCS`, `average`, `upcoming`, `regime`, `holdings`, `watch X`, `bought 12 at 1450`, `help` |
| Data & Sources | Sync log, the files rejected by the two-date check, and source tiers |

## Trust rules

- **Tier 1 (score 1.0):** NSE files and filings, SEBI, RBI, AMFI, NSE Indices, rating agencies. Only Tier 1 data sets prices and trade levels.
- **Tier 2 (0.8):** Reuters, Bloomberg, Business Standard, ET, Mint, BusinessLine, FE, Moneycontrol. Used for context only.
- **Tier 3/4:** aggregators, social media, Telegram, YouTube. These are dropped.
- **Score:** a fact scoring ≥ 0.85 is used. Between 0.60 and 0.85 it is marked *unverified* and not used for decisions. Below 0.60 it is discarded. An independent confirmation adds 0.1; a conflict subtracts 0.3; stale data subtracts 0.2.
- **Asymmetric rule:** unverified bad news can block a buy, but unverified good news can never trigger one.
- **No pick on stale data:** if the latest trading day's NSE files are missing or fail the two-date check, you get "No pick today: fresh data not confirmed".

## Optional AI (free)

The chat works without any AI. To add plain-language summaries, set one of these before starting the app:

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

## Known limits

- History starts in January 2020 (NSE's delivery-data file format). There is no 2008-type crash in the sample.
- The backtest universe is today's Nifty 500 (survivorship bias, which makes results look better).
- The benchmark is the Nifty 500 price index, without dividends.
- The averaging rule and the F&O open-interest context come from the research plan and are **not backtested**.
- Quarterly results come from NSE integrated filings (XBRL). Banks and some companies may show "data missing".

## Tests

```powershell
python -m pytest -q
```
