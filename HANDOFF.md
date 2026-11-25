# Handoff: Trade Tracker rebuild

Last updated **25 Nov 2026**. Read this first when picking the project up. For usage, see `README.md`.

## Where things stand

| Item | State |
|---|---|
| Code | Branch `rebuild/quant-trade-tracker`, pushed. Commits `a9b24ef` (rebuild) and `be59c76` (login and Render support), plus this docs commit |
| Pull request | https://github.com/SandulaVinay/trade-tracker/pull/1 into `main`: **open, mergeable, not merged**. `main` still has the old Node "Trade Pulse" app |
| Tests | `python -m pytest -q` gives **26 passed** (all offline) |
| Live site | https://trade-tracker-nse.onrender.com: Render **free** Web Service `trade-tracker-nse` (id `srv-dc28hlt6e07s73b32rlg`, Ohio), deployed from the branch above. Login: any username plus `TT_PASSWORD` from the service's Environment tab |
| Other Render service | `ttd-1` (Docker) already existed in the account; untouched |
| Local copy | Full history Jan 2020 – 23 Nov 2026 in `data/tracker.db` (~570 MB, git-ignored). **The local app is not running now.** Start it with `python -m tt.app` |
| Active strategy | **S3, pullback in an uptrend** (robust winner) |
| First pick (paper) | **Piramal Pharma (PPLPHARMA)**, signal on 23 Nov 2026: buy zone ₹234.80–239.00, stop ₹215.85, T1 ₹268.45, T2 ₹300, 80 shares at half size |
| Local calls | #1 HONASA (S2) was cancelled before any fill when the strategy changed to S3. #2 PPLPHARMA is still *planned*; it updates with the 24–25 Nov sessions next time the local app runs |

## What was built (summary)

1. **Research to plan.** The starting point was the user's core-satellite research report. It recommends an index core, a rule-based momentum or swing satellite, no averaging into stock-specific problems, and paper trading first. The app implements the satellite.
2. **Old app removed.** The hard-coded Node app (`server.js`, `public/`, `scan_*.py`, old `render.yaml`) was replaced. Its trade notes (Coal India, Power Grid, Apollo) remain in git history before `a9b24ef`.
3. **Data layer** (`tt/nse.py`, `tt/data_sync.py`): Tier-1 NSE sources only, each file two-date checked.
   - The checks rejected 87 holiday files, plus 3 April-2023 index files that NSE published with day and month swapped.
   - Splits and bonuses come from NSE's corporate-actions API (`tt/corp_actions.py`), because bhavcopy `PREV_CLOSE` is unadjusted. A factor is applied only when the ex-date price move confirms it, with factors combined per ex-date.
   - Preference-share "bonuses" (e.g. TVS Motor NCRPS) are ignored.
4. **Strategy discovery** (`tt/strategies.py`, `tt/backtest.py`): 8 rule sets, using the same plan rules as the live picks.
   - Costs: STT, stamp, exchange, SEBI, GST, DP charges and slippage.
   - Tax: 20% STCG per financial year.
   - Results are measured from Jan 2021, including an idle-cash scenario and ±20% robustness.
5. **Daily engine** (`tt/signals.py`, `tt/tradeplan.py`, `tt/news.py`, `tt/confidence.py`):
   - regime gate;
   - gates: ASM/GSM, F&O ban, results blackout, red flags;
   - second-source price cross-check against the UDiFF file;
   - results and shareholding from NSE XBRL;
   - Tier-2 press as context only;
   - confidence-scored facts with links.
6. **Tracker** (`tt/tracker.py`): paper fills, T1/T2 partial exits, breakeven, trailing stop, regime-OFF tightening, the day-45 time exit, cause-of-fall analysis, one-tranche averaging alerts, and manual actions.
7. **Web app** (`tt/app.py`, `web/`): tabs Today, Calls, Watchlist, Strategy Lab, Chat and Data & Sources, plus PDF reports, an optional free LLM with a number guard, and HTTP Basic login.
8. **Parallel agents.**
   - One agent wrote `tests/test_api.py` (14 offline end-to-end tests). It fixed 4 tracker bugs: replaying history from a non-trading date, sessions and held-days on non-trading dates, ₹0 or negative manual trades, and cancelling open positions.
   - Another agent hardened the frontend: null-guards, empty states, 375 px layout, keyboard and focus, and contrast.
9. **Deployment.** `/healthz`, `/api/nse-check`, a cloud bootstrap with recent history only, the seed import, and `0.0.0.0:$PORT` binding.
   - The Render Blueprint flow wanted a payment card, so a plain free Web Service was created instead. No card was entered.
   - NSE access from Render was verified: 760 files and 501 corporate-action calls, 0 errors. The live first scan matched the local pick.

## Key decisions and why

- **The winner rule is the best worst-period CAGR** among rule sets that are positive in both periods and robust to ±20% stops. The first rule (recent expectancy) picked S2 on a hair-thin margin even though S3 was better on almost every measure.
- **Honest benchmark.** No rule set beat Nifty 500 buy-and-hold over 2021–2026 after costs and tax: S3 7.8% vs 13.0%, with a deeper drawdown, −21% vs −19%. The Strategy Lab says so. This supports the research plan's "small satellite" framing; it doesn't prove the plan wrong.
- **Paper trading first**, as the user asked. The live track record starts with PPLPHARMA.
- **Never present stale data as new.** A stale scan never shows an old pick; old official filings are "background", not decision inputs.
- **No secrets in chat or code.** The Render password is generated by Render; the local password lives in `.env` (git-ignored).

## Known gaps and open items

- **Free Render tier.** Disk is wiped on every sleep, restart or deploy, so **live paper trades are lost**, history is re-downloaded (~11 min), and the 18:45 scan runs only if the site is awake. The user was offered a keep-alive ping or a paid disk; neither is done.
- **Settings not enforced:**
  - `MAX_NEW_ENTRIES_PER_MONTH` (8) is defined but not enforced anywhere.
  - `MAX_SECTOR_POSITIONS` is enforced only in the backtest, not in the live scan.
  - `CLOUD` in `tt/config.py` is unused.
- **FastAPI `on_event` deprecation warning** at start-up (harmless); it could move to a lifespan handler.
- **Tracker cosmetics:** when a call closes, `last_date` and `last_price` keep the previous bar's values. `manual()` reports an unknown call id as 400 rather than 404.
- **Not backtested:** the averaging rule and the F&O open-interest context. Demergers are not adjusted.
- **Survivorship bias:** the backtest uses today's Nifty 500 list. Point-in-time membership could be rebuilt from NSE index-change notices.
- **Untested at scale:** the optional LLM path (no key configured) and Tier-2 press parsing on many stocks.

## How to operate

```powershell
python -m tt.app                      # local app at http://localhost:8000 (runs the daily scan after 18:45 IST)
```

```powershell
python -m tt.data_sync                # catch up NSE history manually
```

```powershell
python -m pytest -q                   # 26 tests, offline
```

**Refreshing the live site's backtest results:**
1. Locally, open Strategy Lab → **Run backtests**.
2. Run `python scripts/export_seed.py`.
3. Commit `seed/backtests.json` and push. Render redeploys from `rebuild/quant-trade-tracker` and imports it on first start.

**If `main` is merged:** the live service still deploys from `rebuild/quant-trade-tracker`. Switch its branch in Render → Settings if the feature branch is deleted.

## Suggested next steps

1. Run the local app every evening, or leave it running, and paper-trade for 30+ calls before risking money. Then compare live expectancy with the backtest in the Calls tab.
2. Decide on the live site: add a keep-alive ping (free, no persistence guarantee) or a paid instance with a disk (persistent).
3. Enforce `MAX_NEW_ENTRIES_PER_MONTH` and the sector cap in the live scan.
4. Review and merge PR #1 when happy.
