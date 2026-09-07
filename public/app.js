let deferredPrompt = null;

// PWA Install prompt listener
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredPrompt = e;
  const installBtn = document.getElementById('installBtn');
  if (installBtn) {
    installBtn.style.display = 'flex';
    installBtn.addEventListener('click', () => {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        deferredPrompt.userChoice.then(() => {
          deferredPrompt = null;
          installBtn.style.display = 'none';
        });
      }
    });
  }
});

// Register Service Worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(err => {
    console.log('SW registration note:', err.message);
  });
}

// Fetch live tickers
async function fetchLiveTickers() {
  try {
    const res = await fetch('/api/opinions');
    const data = await res.json();
    if (data.success && data.stocks) {
      data.stocks.forEach(stock => {
        const p = stock.quote && stock.quote.price ? `₹${stock.quote.price.toFixed(2)}` : 'Connecting...';
        if (stock.ticker === 'BHARTIARTL') {
          document.getElementById('airtelLtp').textContent = p;
          const badge = document.getElementById('airtelBadge');
          badge.textContent = stock.verdict.status === 'BUY' ? 'BUY ZONE' : stock.verdict.status;
          badge.className = `t-badge badge-${stock.verdict.badge}`;
        } else if (stock.ticker === 'HAL') {
          document.getElementById('halLtp').textContent = p;
          const badge = document.getElementById('halBadge');
          badge.textContent = stock.verdict.status;
          badge.className = `t-badge badge-${stock.verdict.badge}`;
        } else if (stock.ticker === 'BALUFORGE') {
          document.getElementById('baluLtp').textContent = p;
          const gain = stock.quote && stock.quote.price ? ((stock.quote.price - stock.buyPrice) * stock.shares).toFixed(0) : '1092';
          const badge = document.getElementById('baluBadge');
          badge.textContent = `+₹${gain} GAIN`;
          badge.className = 't-badge badge-green';
        }
      });
    }
  } catch (err) {
    console.error('Ticker fetch error:', err);
  }
}

// Format markdown text simply into HTML
function formatMarkdown(text) {
  if (!text) return '';
  let html = text
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(/^### (.*$)/gim, '<h4 style="margin:8px 0 4px 0; color:#38bdf8;">$1</h4>')
    .replace(/^## (.*$)/gim, '<h3 style="margin:10px 0 6px 0; color:#f8fafc;">$1</h3>')
    .replace(/^\* (.*$)/gim, '<li>$1</li>');

  // Wrap loose <li> in <ul>
  html = html.replace(/(<li>.*<\/li>)/gims, '<ul>$1</ul>');
  // Format linebreaks
  html = html.replace(/\n\n/g, '<p></p>');
  return html;
}

// Send user message
async function sendUserMessage(msgText) {
  const text = (msgText || '').trim();
  if (!text) return;

  const stream = document.getElementById('messagesStream');

  // 1. Append user message
  const userEl = document.createElement('div');
  userEl.className = 'message-bubble user-bubble';
  userEl.innerHTML = `<div>${text}</div>`;
  stream.appendChild(userEl);
  stream.scrollTop = stream.scrollHeight;

  // 2. Append loading indicator
  const loadingEl = document.createElement('div');
  loadingEl.className = 'message-bubble assistant-bubble';
  loadingEl.innerHTML = `
    <div class="bubble-header"><span class="ai-tag">Analyzing...</span></div>
    <div class="loading-dots"><span></span><span></span><span></span></div>
  `;
  stream.appendChild(loadingEl);
  stream.scrollTop = stream.scrollHeight;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    });
    const data = await res.json();

    loadingEl.remove();

    if (data.success && data.reply) {
      const r = data.reply;
      const assistEl = document.createElement('div');
      assistEl.className = 'message-bubble assistant-bubble';
      assistEl.innerHTML = `
        <div class="bubble-header">
          <span class="ai-tag">${r.title || 'Trade Pulse Engine'}</span>
          <span class="timestamp">${new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' })} IST</span>
        </div>
        ${r.verdict ? `<div class="bubble-verdict-tag badge-${r.badge || 'blue'}">${r.verdict}</div>` : ''}
        <div class="bubble-body">${formatMarkdown(r.text)}</div>
      `;
      stream.appendChild(assistEl);
    } else {
      throw new Error(data.error || 'Invalid response');
    }
  } catch (err) {
    loadingEl.remove();
    const errorEl = document.createElement('div');
    errorEl.className = 'message-bubble assistant-bubble';
    errorEl.innerHTML = `<div style="color: #f87171;">Connection error. Please tap 'Refresh' or ask again.</div>`;
    stream.appendChild(errorEl);
  }

  stream.scrollTop = stream.scrollHeight;
}

function handleFormSubmit(e) {
  e.preventDefault();
  const input = document.getElementById('userInput');
  const val = input.value;
  input.value = '';
  sendUserMessage(val);
}

function sendQuickPrompt(promptText) {
  sendUserMessage(promptText);
}

// On page load
document.addEventListener('DOMContentLoaded', () => {
  fetchLiveTickers();
  setInterval(fetchLiveTickers, 45000); // Live update every 45s
});
