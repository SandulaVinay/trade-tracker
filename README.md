# Trade Pulse — AI Market Chat & 10:00 AM Opinion Engine

Mobile-first real-time trade execution advisor and accuracy tracker for Indian Equity Markets (NSE).

## Features
- **Interactive Chat Assistant**: Query real-time verdicts, targets, and stop-loss levels.
- **10:00 AM Live Opinion**: Evaluates market open volatility, buy zones, and trap filters.
- **Live NSE Market Feed**: Real-time quotes for Nifty 50 and broader market momentum picks.
- **PWA Mobile App**: Installable to smartphone home screen with offline service worker.
- **Accuracy Tracker**: Logs historical trades and statistical hit rates.

## Deployment on Render
1. Select **New Web Service** (NOT static site).
2. Connect this repository.
3. Runtime: **Node**.
4. Build Command: `npm install --production`
5. Start Command: `node server.js`
