import urllib.request
import json

symbols = [
    '^NSEI', 'DIVISLAB.NS', 'SUNPHARMA.NS', 'CIPLA.NS', 'TCS.NS', 'INFY.NS',
    'HDFCBANK.NS', 'ICICIBANK.NS', 'RELIANCE.NS', 'TRENT.NS', 'BEL.NS',
    'COALINDIA.NS', 'BHARTIARTL.NS', 'M%26M.NS', 'ITC.NS', 'TATAMOTORS.NS',
    'APOLLOHOSP.NS', 'BAJAJ-AUTO.NS', 'DRREDDY.NS'
]

print("Scanning candidates for Wednesday 09-Sep-2026...")
results = []
for s in symbols:
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{s}?interval=1d&range=5d'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        res = urllib.request.urlopen(req, timeout=5)
        data = json.loads(res.read().decode())['chart']['result'][0]
        meta = data['meta']
        quote = data['indicators']['quote'][0]
        closes = [c for c in quote['close'] if c is not None]
        highs = [h for h in quote['high'] if h is not None]
        lows = [l for l in quote['low'] if l is not None]
        vols = [v for v in quote['volume'] if v is not None]
        chg = ((closes[-1] - closes[-2]) / closes[-2]) * 100
        results.append({
            'sym': meta.get('symbol'),
            'ltp': round(closes[-1], 2),
            'chg': round(chg, 2),
            'high': round(highs[-1], 2),
            'low': round(lows[-1], 2),
            'vol': vols[-1],
            'high52': meta.get('fiftyTwoWeekHigh')
        })
    except Exception as e:
        pass

results.sort(key=lambda x: x['chg'], reverse=True)
for r in results:
    print(f"{r['sym']:<15} LTP: {r['ltp']:<8} Change: {r['chg']:>+6.2f}% High: {r['high']:<8} Low: {r['low']:<8} 52W-High: {r['high52']}")
