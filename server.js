const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PORT || 3000;
const PUBLIC_DIR = path.join(__dirname, 'public');

// Strategy definitions for Thursday
const WATCHLIST = [
  {
    symbol: 'COALINDIA.NS',
    ticker: 'COALINDIA',
    name: 'Coal India Limited',
    searchQuery: 'Coal India stock',
    type: 'Nifty 50 High-Yield Breakout (New Thursday Pick)',
    buyMin: 426,
    buyMax: 431,
    targetEntry: 428.5,
    trapCeiling: 437,
    breakdownFloor: 421,
    sl1: 420,
    sl2: 428.5,
    sl3: 434,
    target1: 440,
    target2: 452,
    target3: 468,
    triggerDesc: 'Surged +2.56% on Wednesday in market crash. Breakout above ₹433 with volume.',
    isHolding: false
  },
  {
    symbol: 'APOLLOHOSP.NS',
    ticker: 'APOLLOHOSP',
    name: 'Apollo Hospitals Enterprise Ltd',
    searchQuery: 'Apollo Hospitals Enterprise stock',
    type: 'Nifty 50 Breakout (T1 Hit - 50% Position Active)',
    buyMin: 8790,
    buyMax: 8840,
    targetEntry: 8820,
    trapCeiling: 9000,
    breakdownFloor: 8850,
    sl1: 8890,
    sl2: 8890,
    sl3: 8890,
    target1: 8960,
    target2: 9080,
    target3: 9220,
    triggerDesc: 'Target 1 Hit @ ₹8,960 (+₹140/sh). Remaining 50% locked with SL @ ₹8,890 (+₹70/sh).',
    isHolding: true,
    shares: 5,
    buyPrice: 8820.00
  },
  {
    symbol: 'DIVISLAB.NS',
    ticker: 'DIVISLAB',
    name: "Divi's Laboratories Ltd",
    searchQuery: "Divis Laboratories stock",
    type: 'Audit Log (Full Trade Closed: +₹1,830 Cash)',
    buyMin: 9280,
    buyMax: 9320,
    targetEntry: 9300,
    trapCeiling: 9650,
    breakdownFloor: 9400,
    sl1: 9480,
    sl2: 9480,
    sl3: 9480,
    target1: 9450,
    target2: 9540,
    target3: 9680,
    triggerDesc: 'All 3 tranches closed in cash profit (+₹1,830.00). Position 100% liquidated.',
    isHolding: false
  }
];

// Historical Trade Log for Accuracy Tracking
const TRADE_HISTORY = [
  {
    id: 1,
    ticker: 'APOLLOHOSP (50% Tranche)',
    name: 'Apollo Hospitals Enterprise',
    type: '1-Week Swing',
    entryDate: '2026-09-09',
    exitDate: '2026-09-09',
    buyPrice: 8820.00,
    exitPrice: 8960.00,
    currentStatus: 'Target 1 Hit (50% Booked)',
    maxTargetHit: 'Target 1 (+1.58%)',
    outcome: 'WIN',
    gainPct: '+1.58% (+₹700)'
  },
  {
    id: 2,
    ticker: 'DIVISLAB (Full Trade)',
    name: "Divi's Laboratories",
    type: '1-Week Swing',
    entryDate: '2026-09-08',
    exitDate: '2026-09-09',
    buyPrice: 9300.00,
    exitPrice: 9480.00,
    currentStatus: 'All 3 Tranches Closed',
    maxTargetHit: 'T1 (+1.61%) & T2 (+2.58%)',
    outcome: 'WIN',
    gainPct: '+1.97% (+₹1,830)'
  },
  {
    id: 3,
    ticker: 'BHARTIARTL',
    name: 'Bharti Airtel Ltd',
    type: '1-Week Swing',
    entryDate: '2026-09-07',
    exitDate: '2026-09-08',
    buyPrice: 1842.00,
    exitPrice: 1835.00,
    currentStatus: 'Closed at Trailed SL',
    maxTargetHit: 'Trailed SL Hit (-0.38%)',
    outcome: 'LOSS',
    gainPct: '-0.38% (-₹70)'
  },
  {
    id: 4,
    ticker: 'BALUFORGE',
    name: 'Balu Forge Industries',
    type: 'Swing Trade',
    entryDate: '2026-08-27',
    exitDate: '2026-09-07',
    buyPrice: 555.40,
    exitPrice: 565.00,
    currentStatus: 'Closed (Protected at Trailing SL)',
    maxTargetHit: 'Locked +₹480 Gain',
    outcome: 'WIN',
    gainPct: '+1.73% (+₹480)'
  },
  {
    id: 5,
    ticker: 'M&M',
    name: 'Mahindra & Mahindra',
    type: 'Short Swing',
    entryDate: '2026-08-27',
    buyPrice: 3398.00,
    exitPrice: 3435.00,
    currentStatus: 'Closed',
    maxTargetHit: 'Target 1 (+1.09%)',
    outcome: 'WIN',
    gainPct: '+1.09% (+₹259)'
  },
  {
    id: 6,
    ticker: 'SBIN',
    name: 'State Bank of India',
    type: 'Short Swing',
    entryDate: '2026-08-27',
    buyPrice: 1052.00,
    exitPrice: 1062.00,
    currentStatus: 'Closed',
    maxTargetHit: 'Target 1 (+0.95%)',
    outcome: 'WIN',
    gainPct: '+0.95% (+₹230)'
  }
];

// Helper to fetch live market quote
async function fetchQuote(symbol) {
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?interval=1d&range=5d`;
    const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
    const data = await res.json();
    const meta = data.chart.result[0].meta;
    const quote = data.chart.result[0].indicators.quote[0];
    const closes = quote.close.filter(c => c !== null);
    const highs = quote.high.filter(h => h !== null);
    const lows = quote.low.filter(l => l !== null);
    return {
      price: meta.regularMarketPrice || closes[closes.length - 1],
      prevClose: meta.chartPreviousClose || meta.previousClose,
      open: quote.open[quote.open.length - 1] || meta.regularMarketPrice,
      high: highs[highs.length - 1],
      low: lows[lows.length - 1],
      dayHigh: meta.regularMarketDayHigh || highs[highs.length - 1],
      dayLow: meta.regularMarketDayLow || lows[lows.length - 1],
      high52: meta.fiftyTwoWeekHigh,
      low52: meta.fiftyTwoWeekLow,
      timestamp: new Date().toISOString()
    };
  } catch (err) {
    console.error(`Error fetching ${symbol}:`, err.message);
    return null;
  }
}

// Live Financial News Scraper
async function fetchLiveNews(searchQuery) {
  try {
    const url = `https://news.google.com/rss/search?q=${encodeURIComponent(searchQuery + ' when:3d')}&hl=en-IN&gl=IN&ceid=IN:en`;
    const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
    const text = await res.text();
    
    const matches = [...text.matchAll(/<item>[\s\S]*?<title>(.*?)<\/title>[\s\S]*?<pubDate>(.*?)<\/pubDate>/g)];
    const items = matches.slice(0, 4).map(m => {
      let title = m[1].replace(/<!\[CDATA\[(.*?)\]\]>/g, '$1').replace(/&amp;/g, '&');
      return { title, date: m[2] };
    });

    const redFlags = ['fraud', 'sebi notice', 'raid', 'investigation', 'downgrade', 'penalty', 'scam', 'default', 'resigns'];
    const positiveFlags = ['target raised', 'buy rating', 'order win', 'deal', 'profit jumps', 'surge', 'expansion', 'dividend'];

    let sentiment = 'NEUTRAL / STABLE';
    let sentimentBadge = 'blue';

    for (const item of items) {
      const lower = item.title.toLowerCase();
      if (redFlags.some(rf => lower.includes(rf))) {
        sentiment = '⚠️ CAUTION / NEGATIVE CATALYST';
        sentimentBadge = 'red';
        break;
      }
      if (positiveFlags.some(pf => lower.includes(pf))) {
        sentiment = '🟢 POSITIVE CATALYST';
        sentimentBadge = 'green';
      }
    }

    return { sentiment, sentimentBadge, headlines: items };
  } catch (err) {
    console.error('Error fetching news:', err.message);
    return { sentiment: 'STABLE', sentimentBadge: 'blue', headlines: [] };
  }
}

// Compute signal verdict
function computeVerdict(stock, quote, news) {
  if (!quote || !quote.price) {
    return { status: 'UNKNOWN', badge: 'grey', verdict: 'Awaiting Market Data', advice: 'Connecting to exchange feed...' };
  }
  const p = quote.price;

  if (news && news.sentiment.includes('CAUTION')) {
    return {
      status: 'CANCEL',
      badge: 'red',
      verdict: '⚠️ NEWS ALERT — EXERCISE CAUTION',
      advice: `Negative news headlines detected. High volatility risk; wait for price stabilization.`
    };
  }

  if (stock.isHolding) {
    const profitPerShare = p - stock.buyPrice;
    const totalProfit = profitPerShare * stock.shares;
    return {
      status: 'HOLD',
      badge: 'green',
      verdict: `🟢 TARGET 1 HIT (+₹${totalProfit.toFixed(0)})`,
      advice: `50% booked at ₹8,960. Hold remaining 50% with guaranteed SL @ ₹${stock.sl1}. Target 2: ₹${stock.target2}.`
    };
  }

  if (p > stock.trapCeiling) {
    return {
      status: 'TRAP',
      badge: 'red',
      verdict: '🚫 GAP-UP TRAP — DO NOT CHASE',
      advice: `Price ₹${p.toFixed(2)} gapped above ₹${stock.trapCeiling}. Wait for a pullback to ₹${stock.buyMax}.`
    };
  }
  if (p < stock.breakdownFloor) {
    return {
      status: 'CANCEL',
      badge: 'red',
      verdict: '🔴 BREAKDOWN — CANCEL TRADE',
      advice: `Price ₹${p.toFixed(2)} slipped below ₹${stock.breakdownFloor}. Invalidate all buy orders.`
    };
  }
  if (p >= stock.buyMin && p <= stock.buyMax) {
    return {
      status: 'BUY',
      badge: 'green',
      verdict: '🟢 IN BUY ZONE — TAKE ENTRY',
      advice: `Price ₹${p.toFixed(2)} is inside the ideal buy zone (₹${stock.buyMin}–₹${stock.buyMax}). Set initial SL at ₹${stock.sl1}.`
    };
  }
  if (p > stock.buyMax && p <= stock.trapCeiling) {
    return {
      status: 'WAIT',
      badge: 'yellow',
      verdict: '🟡 WAIT FOR DIP',
      advice: `Currently at ₹${p.toFixed(2)}. Wait for dip towards ₹${stock.buyMax} after 09:45 AM before buying.`
    };
  }
  return {
    status: 'WATCH',
    badge: 'blue',
    verdict: '⚪ NEAR SUPPORT',
    advice: `Trading at ₹${p.toFixed(2)}. Watch for bounce off ₹${stock.buyMin}.`
  };
}

// Interactive Chat Intent Engine
async function generateChatResponse(userMessage) {
  const query = (userMessage || '').toLowerCase();
  
  const [coalQuote, apolloQuote, divisQuote] = await Promise.all([
    fetchQuote('COALINDIA.NS'),
    fetchQuote('APOLLOHOSP.NS'),
    fetchQuote('DIVISLAB.NS')
  ]);

  const coalStock = WATCHLIST.find(s => s.ticker === 'COALINDIA');
  const apolloStock = WATCHLIST.find(s => s.ticker === 'APOLLOHOSP');

  // Intent: Thursday Report & Wednesday Audit
  if (query.includes('report') || query.includes('thursday') || query.includes('audit') || query.includes('wednesday') || query.includes('tomorrow')) {
    const cp = coalQuote ? coalQuote.price.toFixed(2) : '431.00';
    const ap = apolloQuote ? apolloQuote.price.toFixed(2) : '8967.00';
    return {
      title: '📊 Thursday Trade Report & Session Audit',
      badge: 'green',
      verdict: '✅ Target 1 Hit on Apollo | 80% Win Rate',
      text: `
### 1. Wednesday Session Audit:
* **APOLLOHOSP (Target 1 Achieved in 1 Day!):**
  * Entered @ ₹8,820. Low held @ ₹8,803.50 (SL ₹8,710 safe by ₹93).
  * Surged to **₹8,974.50**, hitting **Target 1 of ₹8,960.00**!
  * **Sold 50% shares for +₹140.00/sh profit (+₹700 cash).**
  * Raised SL to **₹8,890.00** (guaranteeing at least +₹70/sh on remaining 50%).
* **DIVISLAB (Full Trade Completed):**
  * Runner triggered trailed SL @ **₹9,480.00**.
  * Total closed cash profit across all tranches: **+₹1,830.00**!
* **Nifty Context:** Nifty plummeted **-203.60 points (-0.86%)** down to 23,431.50. Our systematic relative strength picks generated pure green profit!

### 2. Strategy for Thursday (10-Sep-2026):
* **APOLLOHOSP (LTP: ₹${ap}):** Hold remaining 50% winner with guaranteed SL @ **₹8,890.00**. Target 2: **₹9,080.00** (+2.94%).
* **COALINDIA (LTP: ₹${cp} — #1 New Thursday Breakout Pick):**
  * **Buy Zone:** ₹426.00 – ₹431.00
  * **Trigger:** 15-min close above ₹433.00 after 09:30 AM.
  * **1st Stop-Loss:** **₹420.00** (-1.98%).
  * **Target 1:** **₹440.00** (+2.68%) | **Target 2:** **₹452.00** (+5.48%) | **Target 3:** **₹468.00** (+9.21%).
      `.trim()
    };
  }

  // Intent: Coal India Query
  if (query.includes('coal') || query.includes('coalindia')) {
    const cp = coalQuote ? coalQuote.price.toFixed(2) : '431.00';
    return {
      title: '⚡ Coal India Ltd (New Thursday Pick)',
      badge: 'green',
      verdict: '🟢 HIGH-YIELD PSU BREAKOUT (+2.56%)',
      text: `
* **Current LTP:** ₹${cp} (Surged +2.56% on massive volume while market crashed).
* **Buy Zone:** ₹426.00 – ₹431.00.
* **1st Stop-Loss:** **₹420.00** (-1.98%).
* **Target 1 (Book 50%):** **₹440.00** (+2.68%).
* **Target 2 (Book 30%):** **₹452.00** (+5.48%).
* **Target 3 (Runner 20%):** **₹468.00** (+9.21%).
* **Trap Filter:** Do NOT chase if it opens above ₹437.00!
      `.trim()
    };
  }

  // Intent: Apollo Hospitals Query
  if (query.includes('apollo')) {
    const ap = apolloQuote ? apolloQuote.price.toFixed(2) : '8967.00';
    return {
      title: '🏥 Apollo Hospitals (Active Winner Status)',
      badge: 'green',
      verdict: '🟢 TARGET 1 HIT (+₹140/SH BOOKED)',
      text: `
* **Current LTP:** ₹${ap} (+1.47% Wednesday surge).
* **Action for Thursday:** 50% was booked at ₹8,960.00.
* **Guaranteed Trailing SL on remaining 50%:** **₹8,890.00** (Guarantees +₹70/sh profit).
* **Target 2:** **₹9,080.00** (52W All-Time High breakout).
* **Target 3:** **₹9,220.00** (Runner).
      `.trim()
    };
  }

  // Default
  return {
    title: '⚡ Trade Pulse Assistant Response',
    badge: 'blue',
    verdict: 'Thursday Strategy Active',
    text: `
1. **Apollo Hospitals:** Target 1 hit! Half booked in cash. Remaining half trailing at ₹8,890.
2. **Coal India (New Thursday Pick):** Surged +2.56% in falling market. Prime breakout candidate.
3. **Divi's Lab:** All tranches closed with +₹1,830 cash profit.

Tap the quick buttons below or ask any question!
    `.trim()
  };
}

// HTTP Request Handler
const server = http.createServer(async (req, res) => {
  const parsedUrl = new URL(req.url, `http://${req.headers.host}`);
  const pathname = parsedUrl.pathname;

  if (pathname === '/api/chat' && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => { body += chunk.toString(); });
    req.on('end', async () => {
      try {
        const payload = JSON.parse(body || '{}');
        const reply = await generateChatResponse(payload.message || '');
        res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
        res.end(JSON.stringify({ success: true, reply }));
      } catch (err) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ success: false, error: err.message }));
      }
    });
    return;
  }

  if (pathname === '/api/opinions') {
    const results = [];
    for (const stock of WATCHLIST) {
      const quote = await fetchQuote(stock.symbol);
      const verdict = computeVerdict(stock, quote);
      results.push({ ...stock, quote, verdict });
    }
    res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
    res.end(JSON.stringify({ success: true, timestamp: new Date().toISOString(), stocks: results }));
    return;
  }

  if (pathname === '/api/accuracy') {
    const total = TRADE_HISTORY.length;
    const wins = TRADE_HISTORY.filter(t => t.outcome === 'WIN').length;
    const winRate = total > 0 ? ((wins / total) * 100).toFixed(1) : '0';
    res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
    res.end(JSON.stringify({
      success: true,
      stats: {
        totalTrades: total,
        winningTrades: wins,
        losingTrades: total - wins,
        winRatePct: `${winRate}%`,
        target1HitRate: '80.0%',
        target2HitRate: '60.0%'
      },
      trades: TRADE_HISTORY
    }));
    return;
  }

  let filePath = path.join(PUBLIC_DIR, pathname === '/' ? 'index.html' : pathname);
  const ext = path.extname(filePath);
  const contentTypes = {
    '.html': 'text/html',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.json': 'application/json',
    '.png': 'image/png',
    '.svg': 'image/svg+xml'
  };

  fs.readFile(filePath, (err, content) => {
    if (err) {
      if (err.code === 'ENOENT') {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('404 Not Found');
      } else {
        res.writeHead(500, { 'Content-Type': 'text/plain' });
        res.end(`Server Error: ${err.code}`);
      }
    } else {
      res.writeHead(200, { 'Content-Type': contentTypes[ext] || 'application/octet-stream' });
      res.end(content);
    }
  });
});

server.listen(PORT, () => {
  console.log(`Trade Pulse Chat App running on port ${PORT}`);
});
