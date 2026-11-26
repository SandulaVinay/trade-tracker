"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (v) => v != null && v !== "" && !isNaN(v);
const rs = (v) => (!isNum(v) ? "-" : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const pct = (v, d = 1) => (!isNum(v) ? "-" : (v * 100).toFixed(d) + "%");
const num = (v, d = 2) => (!isNum(v) ? "-" : Number(v).toFixed(d));
const crore = (v) => (!isNum(v) ? "-" : "₹" + (v / 1e7).toLocaleString("en-IN", { maximumFractionDigits: 0 }) + " cr");
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const arr = (v) => (Array.isArray(v) ? v : []);
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});
const dash = (v) => (v == null || v === "" ? "-" : v);

async function fetchWithRetry(path, opts) {
  // The free Render instance can be asleep or restarting; a single dropped connection is retried once.
  for (let attempt = 1; ; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 60000);
    try {
      return await fetch(path, { headers: { "Content-Type": "application/json" }, signal: ctrl.signal, ...opts });
    } catch (e) {
      if (attempt >= 2) {
        const why = e.name === "AbortError" ? "the server took longer than 60 seconds" : "the connection to the server was lost";
        throw new Error(`Could not reach the server (${why}). It may be waking up or restarting on the free hosting plan. Wait a minute, then press Retry.`);
      }
      await new Promise((res) => setTimeout(res, 1500));
    } finally {
      clearTimeout(timer);
    }
  }
}

async function api(path, opts = {}) {
  const r = await fetchWithRetry(path, opts);
  if (!r.ok) {
    let t = await r.text();
    const raw = t.trim();
    const bare = !raw || ["Internal Server Error", "Bad Gateway", "Gateway Timeout", "Service Unavailable"].includes(raw);
    try { const d = JSON.parse(t).detail; t = typeof d === "string" ? d : d ? JSON.stringify(d) : t; } catch (e) {}
    if (bare) t = `The server could not complete this request (HTTP ${r.status}). Please try again in a minute.`;
    throw new Error(t || `${r.status} ${r.statusText}`);
  }
  return r.json();
}
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });

// ------------------------------------------------------------------ tabs
const loaders = {};
const tabButtons = () => document.querySelectorAll("#tabs button");
function currentTab() { const b = $("#tabs button.active"); return b ? b.dataset.tab : "dashboard"; }

async function runLoader(name) {
  const fn = loaders[name];
  if (!fn) return;
  const el = $("#tab-" + name);
  if (name !== "chat" && el && !el.innerHTML.trim()) el.innerHTML = `<div class="card muted">Loading…</div>`;
  try {
    await fn();
  } catch (e) {
    if (name === "chat") { addMsg("assistant", "Could not load chat history: " + e.message); return; }
    if (!el) return;
    el.innerHTML = `<div class="card"><div class="notice bad">Could not load this page: ${esc(e.message)}</div>
      <button type="button" class="btn" data-retry>Retry</button></div>`;
    const b = $("[data-retry]", el);
    if (b) b.onclick = () => { el.innerHTML = ""; runLoader(name); };
  }
}

tabButtons().forEach((b) =>
  b.addEventListener("click", () => {
    tabButtons().forEach((x) => {
      x.classList.toggle("active", x === b);
      if (x === b) x.setAttribute("aria-current", "page"); else x.removeAttribute("aria-current");
    });
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.id === "tab-" + b.dataset.tab));
    try { localStorage.setItem("tab", b.dataset.tab); } catch (e) {}
    runLoader(b.dataset.tab);
  })
);

// ------------------------------------------------------------------ status bar
// The bar is built once; only the pills are refreshed, so the buttons keep keyboard focus between polls.
let lastStatus = {};
let statusLoaded = false;
let lastJobFinished = null;
let pillsHtml = null;

function jobText(job) {
  const log = arr(job && job.log);
  return log.length ? log.join("\n") : job && job.name ? `${job.name}: starting…` : "No job has run since the server started.";
}

function initStatusbar() {
  $("#statusbar").innerHTML = `<span id="statuspills" class="statuspills" aria-live="polite"></span>
    <button type="button" class="btn" id="btnDaily" data-job>Sync + scan now</button>
    <button type="button" class="btn" id="btnCap">Capital</button>`;
  $("#btnDaily").onclick = () => runJob("daily");
  $("#btnCap").onclick = async () => {
    const v = prompt("Trading capital in ₹ (used for position sizing, minimum 10,000):", lastStatus.capital ?? "");
    if (v == null || !v.trim()) return;
    const n = Number(v.replace(/[,₹\s]/g, ""));
    if (!isFinite(n) || n < 10000) { alert("Enter a number of at least 10,000."); return; }
    try { await post("/api/settings", { capital: n }); } catch (e) { alert(e.message); }
    loadStatus();
  };
}

function setPills(html) {
  if (html === pillsHtml) return;
  pillsHtml = html;
  $("#statuspills").innerHTML = html;
}

async function loadStatus() {
  let s;
  try { s = obj(await api("/api/status")); } catch (e) {
    setPills(`<span class="pill bad">Server offline</span>`);
    document.querySelectorAll("[data-job]").forEach((b) => (b.disabled = true));
    return;
  }
  lastStatus = s;
  const job = obj(s.job);
  const running = !!(job.name && !job.finished);
  // the backend reports fresh=true when both dates are null (empty DB), so require a real date
  const fresh = !!(s.fresh && s.latest_data);
  setPills(`
    <span class="pill ${fresh ? "good" : "warn"}" title="Latest NSE bhavcopy in the database vs the latest trading day that should be published">
      NSE data ${esc(s.latest_data || "none")} ${fresh ? "✓ fresh" : "· expected " + esc(s.expected_data || "-")}</span>
    <span class="pill">Strategy ${esc(s.active_strategy || "-")}</span>
    <span class="pill">Capital ${rs(s.capital)}</span>
    <span class="pill ${s.llm ? "good" : ""}" title="Optional free LLM for chat summaries">AI ${s.llm ? "on" : "off (rules only)"}</span>
    ${running ? `<span class="pill warn">Running: ${esc(job.name)}…</span>` : ""}
    ${!running && job.finished && job.ok === false ? `<span class="pill bad" title="${esc(arr(job.log).slice(-1)[0] || "")}">Last ${esc(job.name)} job failed</span>` : ""}`);
  document.querySelectorAll("[data-job]").forEach((b) => (b.disabled = running));
  window.__job = job;
  document.querySelectorAll(".joblog[data-live]").forEach((jl) => (jl.textContent = jobText(job)));
  if (statusLoaded && job.finished && job.finished !== lastJobFinished) runLoader(currentTab());
  lastJobFinished = job.finished || null;
  statusLoaded = true;
}
async function runJob(name) {
  try { await post("/api/run/" + name); } catch (e) { alert(e.message); }
  await loadStatus();
}
setInterval(loadStatus, 4000);

// ------------------------------------------------------------------ PDF download
// /api/pdf/{name} is served as an attachment, so a download link works without popup blockers.
async function pdfDownload(btn, getName) {
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Making PDF…"; }
  try {
    const name = await getName();
    if (!name) throw new Error("No PDF was produced.");
    const a = document.createElement("a");
    a.href = "/api/pdf/" + encodeURIComponent(name);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (e) {
    alert(e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}
async function chatPdf(message) {
  const r = obj(await post("/api/chat", { message }));
  if (!r.pdf) throw new Error(r.text || "No PDF was produced.");
  return r.pdf;
}

// ------------------------------------------------------------------ charts (inline SVG)
function lineChart(points, opts = {}) {
  points = arr(points).filter((p) => p && isFinite(p.y));
  if (points.length < 2) return `<div class="muted small">No chart data.</div>`;
  const refsIn = arr(opts.refs).filter((r) => r && isFinite(r.y));
  const points2 = arr(opts.points2).filter((p) => p && isFinite(p.y));
  // viewBox width follows the screen so labels are not squashed on phones (the SVG stretches to fit)
  const W = Math.max(300, Math.min(640, Math.round((window.innerWidth || 640) - 90))), H = 180, P = 28;
  const ys = points.map((p) => p.y).concat(refsIn.map((r) => r.y)).concat(points2.map((p) => p.y));
  const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
  const X = (i, n) => P + (i * (W - 2 * P)) / (n - 1);
  const Y = (v) => H - P + 6 - ((v - lo) / span) * (H - 2 * P);
  const path = (pts) => pts.map((p, i) => (i ? "L" : "M") + X(i, pts.length).toFixed(1) + "," + Y(p.y).toFixed(1)).join("");
  const refs = refsIn.map((r) => `<line class="ref" x1="${P}" x2="${W - P}" y1="${Y(r.y)}" y2="${Y(r.y)}" stroke="${r.color}"/>
     <text x="${W - P + 2}" y="${Y(r.y) + 3}" style="fill:${r.color}">${esc(r.label)}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W + 40} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(opts.label || "chart")}">
    ${refs}<path class="line" d="${path(points)}"/>${points2.length > 1 ? `<path class="line2" d="${path(points2)}"/>` : ""}
    <text x="${P}" y="12">${esc(points[0].x)}</text><text x="${W - P - 60}" y="12">${esc(points[points.length - 1].x)}</text>
    <text x="2" y="${Y(hi) + 4}">${num(hi, 0)}</text><text x="2" y="${Y(lo)}">${num(lo, 0)}</text></svg>`;
}

// ------------------------------------------------------------------ fact table
function factsTable(facts) {
  facts = arr(facts);
  if (!facts.length) return `<div class="muted small">No facts.</div>`;
  return `<div class="tablewrap"><table><tr><th>Fact</th><th>Value</th><th>As of</th><th>Dates</th><th class="n">Conf.</th><th>Status</th><th>Source</th></tr>
    ${facts.map((f) => `<tr><td>${esc(f.label)}</td><td class="small">${esc(String(f.value ?? "").slice(0, 160))}</td><td>${esc(f.as_of)}</td>
      <td><span class="pill ${f.date_check === "pass" ? "good" : f.date_check === "fail" ? "bad" : ""}">${esc(f.date_check || "n/a")}</span></td>
      <td class="n">${num(f.score)}</td><td><span class="pill ${f.status === "fact" ? "good" : f.status === "unverified" ? "warn" : f.status === "background" ? "" : "bad"}">${esc(f.status || "-")}</span></td>
      <td>${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${f.tier ? "T" + esc(f.tier) + " " : ""}link</a>` : "-"}</td></tr>`).join("")}</table></div>`;
}

// ------------------------------------------------------------------ plan view
const listItems = (xs) => arr(xs).map((w) => `<li>${esc(w)}</li>`).join("") || `<li class="muted">-</li>`;

function planView(plan, scan, opts = {}) {
  if (!plan || typeof plan !== "object") return `<div class="muted small">No plan details available.</div>`;
  const pos = obj(plan.position);
  const c = obj(plan.company);
  const res = obj(c.results), sh = obj(c.shareholding), cur = obj(res.current);
  const fo = obj(plan.fo);
  const bz = arr(plan.buy_zone);
  const blocked = arr(plan.blocked);
  const expiry = obj(plan.expiry);
  const chart = arr(plan.chart).map((p) => ({ x: p.date, y: p.close }));
  const marketFacts = arr(scan && scan.market_facts);
  const companyFacts = arr(plan.company_facts);
  return `
  <div class="plan-head"><div><div class="name">${esc(plan.name || plan.symbol)} <span class="muted">${esc(plan.symbol)}</span></div>
    <div class="small muted">${esc(plan.industry || "")} · ${esc(plan.strategy_name || "-")} (${esc(plan.strategy || "-")}) · NSE close ${esc(plan.data_date || "-")}</div></div>
    <div class="row">${opts.actions || ""}</div></div>
  ${blocked.length ? `<div class="notice bad"><b>Blocked:</b> ${blocked.map(esc).join("; ")}</div>` : ""}
  <div class="levels">
    <div class="level"><div class="l">Buy zone</div><div class="v">${bz.length >= 2 ? `${rs(bz[0])} – ${rs(bz[1])}` : "-"}</div></div>
    <div class="level"><div class="l">Don't chase above</div><div class="v">${rs(plan.no_chase_above)}</div></div>
    <div class="level stop"><div class="l">Stop-loss</div><div class="v">${rs(plan.stop)}</div></div>
    <div class="level tgt"><div class="l">Target 1 (1:${esc(dash(plan.rr_t1))})</div><div class="v">${rs(plan.target1)}</div>
      <div class="small muted">median ${esc(dash(plan.t1_days_median))} sessions · hit ${pct(plan.t1_hit_rate, 0)}</div></div>
    <div class="level tgt"><div class="l">Target 2 (1:${esc(dash(plan.rr_t2))})</div><div class="v">${rs(plan.target2)}</div>
      <div class="small muted">median ${esc(dash(plan.t2_days_median))} sessions · hit ${pct(plan.t2_hit_rate, 0)}</div></div>
    <div class="level"><div class="l">Position</div><div class="v">${esc(pos.qty ?? 0)} sh · ${rs(pos.value)}</div>
      <div class="small muted">${esc(dash(pos.pct_capital))}% of capital · max loss ${rs(pos.max_loss)} (${esc(dash(pos.max_loss_pct))}%)</div></div>
  </div>
  <div class="small muted">Stop reason: ${esc(plan.stop_reason || "-")} · Expected hold ≈ ${esc(dash(plan.expected_hold_days))} sessions (backtest avg) · time exit day ${esc(dash(plan.time_exit_day))}</div>
  ${lineChart(chart, { label: (plan.symbol || "") + " close", refs: [
      { y: plan.stop, label: "SL", color: "var(--bad)" }, { y: plan.target1, label: "T1", color: "var(--good)" },
      { y: plan.target2, label: "T2", color: "var(--good)" }] })}
  <div class="grid">
    <div><h3>Why</h3><ul class="tight">${listItems(plan.why)}</ul></div>
    <div><h3>If it goes up</h3><ul class="tight">${listItems(plan.if_up)}</ul></div>
    <div><h3>If it goes down</h3><ul class="tight">${listItems(plan.if_down)}</ul></div>
  </div>
  <h3>Company checks (NSE filings)</h3>
  <div class="tablewrap"><table>
    <tr><td>Latest quarter</td><td>${isNum(cur.revenue) || isNum(cur.pat) ? `Q ended ${esc(res.quarter_end || "-")}: revenue ${crore(cur.revenue)}, PAT ${crore(cur.pat)} (${esc(cur.basis || "-")})` : `<span class="pill warn">data missing</span> ${esc(res.error || "")}`}</td>
      <td>${cur.url ? `<a href="${esc(cur.url)}" target="_blank" rel="noopener">NSE XBRL · filed ${esc(cur.filed || "-")}</a>` : ""}</td></tr>
    <tr><td>YoY</td><td>Revenue <span class="${cls(res.revenue_yoy)}">${pct(res.revenue_yoy)}</span> · PAT <span class="${cls(res.pat_yoy)}">${pct(res.pat_yoy)}</span></td><td>date check: ${esc(res.date_check || "n/a")}</td></tr>
    <tr><td>Promoter</td><td>${esc(dash(sh.promoter_pct))}% (prev ${esc(dash(sh.promoter_prev))}%) as of ${esc(sh.as_of || "-")} · pledge ${esc(dash(sh.pledge_pct))}%</td>
      <td>${sh.url ? `<a href="${esc(sh.url)}" target="_blank" rel="noopener">NSE shareholding</a>` : ""}</td></tr>
    <tr><td>F&amp;O (untested)</td><td>${fo.in_fo ? `${esc(fo.buildup || "-")} · max call OI strike ${esc(dash(fo.max_call_oi_strike))} · max put OI ${esc(dash(fo.max_put_oi_strike))}` : plan.fo ? "Not in F&amp;O" : "-"}</td>
      <td>${expiry.next_monthly ? `Next monthly expiry ${esc(expiry.next_monthly)} (${esc(dash(expiry.trading_days_to))} sessions)` : ""}</td></tr>
    ${arr(c.warnings).map((w) => `<tr><td><span class="pill warn">warning</span></td><td colspan="2">${esc(w)}</td></tr>`).join("")}
    ${arr(c.positives).map((w) => `<tr><td><span class="pill">info</span></td><td colspan="2">${esc(w)} — positives never trigger a buy</td></tr>`).join("")}
  </table></div>
  <details><summary class="small">Sources (${marketFacts.length + companyFacts.length}) — every fact with date, link and confidence</summary>
    ${factsTable(marketFacts.concat(companyFacts))}</details>`;
}

let dashboardRange = { from: "", to: "" };
function istToday() {
  return lastStatus && lastStatus.now_ist ? String(lastStatus.now_ist).slice(0, 10) : new Date().toISOString().slice(0, 10);
}
function shiftIso(iso, days) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d - days)).toISOString().slice(0, 10);
}
const kpiCard = (label, value, extra = "") => `<div class="kpi"><div class="l">${label}</div><div class="v ${extra}">${value}</div></div>`;

function dashboardTable(rows) {
  if (!rows.length) return `<div class="muted small">No calls in this date range.</div>`;
  return `<div class="tablewrap"><table><tr><th>#</th><th>Stock</th><th>Status</th><th>Signal</th><th class="n">Entry</th>
    <th class="n">Qty</th><th class="n">P&amp;L</th><th class="n">Return</th><th class="n">Last</th><th>Reason</th></tr>
    ${rows.map((r) => `<tr data-call="${esc(r.id)}" tabindex="0"><td>#${esc(r.id)}</td><td>${esc(r.symbol)}</td>
      <td><span class="pill ${r.status === "open" ? "good" : r.status === "planned" ? "warn" : ""}">${esc(r.status)}</span></td>
      <td>${esc(r.signal_date)}</td><td class="n">${rs(r.entry_price)}</td><td class="n">${esc(r.qty ?? 0)}</td>
      <td class="n ${cls(r.pnl)}">${r.pnl == null ? "-" : rs(r.pnl)}</td><td class="n ${cls(r.ret)}">${pct(r.ret)}</td>
      <td class="n">${rs(r.last_price)}</td><td class="small">${esc(r.exit_reason || "")}</td></tr>`).join("")}</table></div>`;
}

loaders.dashboard = async function () {
  const el = $("#tab-dashboard");
  const q = new URLSearchParams();
  if (dashboardRange.from) q.set("from", dashboardRange.from);
  if (dashboardRange.to) q.set("to", dashboardRange.to);
  const d = obj(await api("/api/dashboard" + (q.toString() ? "?" + q : "")));
  const range = obj(d.range), rows = arr(d.calls);
  const accuracy = d.accuracy == null ? "n/a" : pct(d.accuracy, 0);
  el.innerHTML = `
  <div class="card"><div class="plan-head"><h2>Dashboard</h2>
    <form class="row" id="dashForm">
      <label class="small">From <input class="inp" type="date" id="dFrom" value="${esc(dashboardRange.from)}" aria-label="From signal date"></label>
      <label class="small">To <input class="inp" type="date" id="dTo" value="${esc(dashboardRange.to)}" aria-label="To signal date"></label>
      <button type="submit" class="btn primary">Apply</button>
      <button type="button" class="btn" data-range="today">Today</button>
      <button type="button" class="btn" data-range="7">7 days</button>
      <button type="button" class="btn" data-range="30">30 days</button>
      <button type="button" class="btn" data-range="all">All</button>
    </form></div>
    <div class="small muted">Filters by the day the pick was made (signal date). Showing ${esc(range.from || "all")} to ${esc(range.to || "all")}. Default is All.</div></div>
  <div class="card"><h2>At a glance</h2><div class="kpis">
    ${kpiCard("Total calls", esc(d.total_calls ?? 0))}
    ${kpiCard("Triggered", esc(d.triggered ?? 0))}
    ${kpiCard("Not triggered (expired/cancelled)", esc(d.not_triggered ?? 0))}
    ${kpiCard("Pending", esc(d.pending ?? 0))}
    ${kpiCard("Open", esc(d.open ?? 0))}
    ${kpiCard("Closed", esc(d.closed ?? 0))}</div></div>
  <div class="grid">
    <div class="card"><h2>Accuracy</h2><div class="kpis">
      ${kpiCard("Closed calls", esc(d.closed ?? 0))}
      ${kpiCard("Wins", esc(d.wins ?? 0))}
      ${kpiCard("Losses", esc(d.losses ?? 0))}
      ${kpiCard("Accuracy % (wins / closed)", accuracy)}
      ${kpiCard("Target hits", esc(d.target_hits ?? 0))}
      ${kpiCard("Stop hits", esc(d.stop_hits ?? 0))}
      ${kpiCard("Other closes", esc(d.other_closes ?? 0))}</div>
      <div class="small muted">A target hit and a stop hit can both apply to the same closed trade; “Other closes” excludes either.</div></div>
    <div class="card"><h2>Money</h2><div class="kpis">
      ${kpiCard("Invested", rs(d.invested))}
      ${kpiCard("Realised P&amp;L", rs(d.realized_pnl), cls(d.realized_pnl))}
      ${kpiCard("Unrealised P&amp;L", rs(d.unrealized_pnl), cls(d.unrealized_pnl))}
      ${kpiCard("Net P&amp;L after charges", rs(d.net_pnl), cls(d.net_pnl))}</div>
      <div class="small muted">Open position value (last price × open qty): ${rs(d.open_value)}. Unrealised P&amp;L subtracts the cost of the open quantity.</div></div>
  </div>
  <div class="card"><h2>Calls in range <span class="small muted">${rows.length}</span></h2>
    ${dashboardTable(rows)}
    <div class="small muted">Newest signal first. Click a row for the full call timeline.</div></div>`;
  $("#dashForm").onsubmit = async (e) => {
    e.preventDefault();
    dashboardRange = { from: $("#dFrom").value, to: $("#dTo").value };
    runLoader("dashboard");
  };
  el.querySelectorAll("[data-range]").forEach((b) => (b.onclick = () => {
    const today = istToday();
    dashboardRange = { today: { from: today, to: today }, 7: { from: shiftIso(today, 6), to: today },
      30: { from: shiftIso(today, 29), to: today }, all: { from: "", to: "" } }[b.dataset.range];
    runLoader("dashboard");
  }));
  el.querySelectorAll("tr[data-call]").forEach((tr) => {
    const open = () => openCall(tr.dataset.call);
    tr.onclick = open;
    tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
};

// ------------------------------------------------------------------ TODAY
loaders.today = async function () {
  const el = $("#tab-today");
  const [sc0, sum0] = await Promise.all([api("/api/today"), api("/api/summary")]);
  const sc = obj(sc0), sum = obj(sum0);
  const r = sc.regime && typeof sc.regime === "object" ? sc.regime : null;
  const hasScan = !!sc.status;
  let html = `<div class="card"><div class="plan-head"><h2>Daily scan ${r ? "· NSE close " + esc(r.date) : ""}</h2>
      <span class="small muted">${hasScan ? `run ${esc((sc.run_at || "").slice(0, 16).replace("T", " "))} IST · ` : ""}auto-runs after 18:45 IST on trading days</span></div>
    <div class="notice ${sc.status === "pick" ? "good" : sc.status === "stale" ? "bad" : ""}">${esc(sc.message || "No scan yet. Click “Sync + scan now” to download NSE data and run the first scan.")}</div>
    ${hasScan ? "" : `<ol class="tight small"><li>Click <b>Sync + scan now</b> (top of the page). The first sync downloads NSE history and can take several minutes.</li>
      <li>Open <b>Strategy Lab</b> and click <b>Run backtests</b> to choose a strategy from the data.</li>
      <li>When a scan finds a pick, a paper call appears in <b>Calls</b> and is tracked every trading day.</li></ol>`}
    <div class="small muted">Job log</div><div class="joblog" data-live>${esc(jobText(window.__job || {}))}</div></div>`;
  const setups = arr(sc.setups), lastScan = obj(sc.last_scan);
  const noScan = !hasScan || sc.status === "stale";
  let setupsHtml = `<div class="card"><h2>Setups to watch</h2>`;
  if (noScan) {
    setupsHtml += `<div class="notice bad">${hasScan ? "The latest scan is stale; showing the last verified data for reference only." : "No scan has run yet."}
      Last scan: ${esc(lastScan.run_at ? fetchedIst(lastScan.run_at) : "none")}${lastScan.data_date ? " · NSE close " + esc(lastScan.data_date) : ""}.
      Click <b>Sync + scan now</b> at the top of the page to refresh.</div>`;
  } else if (setups.length) {
    setupsHtml += `<div class="small muted">Pass the base trend and liquidity filters, but <b>not a pick: no signal today</b>. Ordered by how close they are to the S3 pullback trigger (lowest RSI(2) first, then nearest the 20-day average).</div>
      <div class="tablewrap"><table><tr><th>Stock</th><th class="n">Close</th><th class="n">Buy zone</th><th class="n">RSI(2)</th><th class="n">From 20-DMA (ATR)</th><th>Status</th></tr>
      ${setups.map((s) => `<tr><td><a href="#" data-an="${esc(s.symbol)}">${esc(s.symbol)}</a> <span class="small muted">${esc(s.name || "")}</span></td>
        <td class="n">${rs(s.close)}</td><td class="n">${arr(s.buy_zone).length === 2 ? `${rs(s.buy_zone[0])} – ${rs(s.buy_zone[1])}` : "-"}</td>
        <td class="n">${isNum(s.rsi2) ? esc(s.rsi2) : "-"}</td><td class="n">${isNum(s.from_20dma_atr) ? esc(s.from_20dma_atr) : "-"}</td>
        <td class="small">not a pick: no signal today</td></tr>`).join("")}</table></div>`;
  } else {
    setupsHtml += `<div class="muted small">No stock passes the base filters without a signal today.</div>`;
  }
  html += setupsHtml + `</div>`;
  if (r) {
    html += `<div class="card"><h2>Market regime <span class="pill ${r.state === "ON" ? "good" : r.state === "OFF" ? "bad" : "warn"}">${esc(r.state || "-")}</span></h2>
      <div class="kpis">
        <div class="kpi"><div class="l">Nifty 500</div><div class="v">${num(r.index)}</div></div>
        <div class="kpi"><div class="l">200-DMA</div><div class="v">${num(r.index_200dma)}</div></div>
        <div class="kpi"><div class="l">Breadth (&gt; own 200-DMA)</div><div class="v">${pct(r.breadth, 0)}</div></div>
        <div class="kpi"><div class="l">Position size</div><div class="v">×${esc(dash(r.size_mult))}</div></div>
        <div class="kpi"><div class="l">Paper record</div><div class="v">${sum.closed || 0} closed · ${pct(sum.win_rate, 0)} win</div></div>
      </div>
      ${lineChart(arr(r.history).map((h) => ({ x: h.date, y: isNum(h.breadth) ? h.breadth * 100 : NaN })), { label: "Market breadth, last 120 sessions", refs: [{ y: 50, label: "50%", color: "var(--warn)" }] })}
      <div class="small muted">Breadth % of the Nifty 500 above their own 200-DMA, last 120 sessions. ON = index above 200-DMA and breadth &gt; 50% (full size); PARTIAL = one of them (half size); OFF = neither (no new buys).</div></div>`;
  }
  if (sc.status === "pick" && sc.pick) {
    html += `<div class="card"><h2>Pick for the next session</h2>${planView(sc.pick, sc, { actions:
      `<button type="button" class="btn primary" id="pdfPick">PDF</button>` })}</div>`;
  }
  const checked = arr(sc.checked);
  if (checked.length) {
    html += `<div class="card"><h2>Candidates checked</h2><div class="tablewrap"><table><tr><th>Stock</th><th>Result</th><th>Reasons / warnings</th></tr>
      ${checked.map((c) => `<tr><td><a href="#" data-an="${esc(c.symbol)}">${esc(c.symbol)}</a> <span class="muted small">${esc(c.name)}</span></td>
        <td>${c.blocked ? `<span class="pill bad">rejected</span>` : `<span class="pill good">passed</span>`}</td>
        <td class="small">${esc(arr(c.block_reasons).concat(arr(c.warnings)).join("; ") || "-")}</td></tr>`).join("")}</table></div></div>`;
  }
  if (arr(sc.market_facts).length && sc.status !== "pick") html += `<div class="card"><h2>Market data sources</h2>${factsTable(sc.market_facts)}</div>`;
  el.innerHTML = html;
  const b = $("#pdfPick");
  if (b) b.onclick = () => pdfDownload(b, () => chatPdf("pdf today"));
  bindAnalyse(el);
};

function bindAnalyse(el) {
  el.querySelectorAll("[data-an]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openAnalysis(a.dataset.an); }));
}

function fetchedIst(value) {
  const d = new Date(value);
  if (isNaN(d.getTime())) return String(value).slice(0, 16).replace("T", " ") + " IST";
  try {
    return new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit", hour12: true }).format(d) + " IST";
  } catch (e) {
    return String(value).slice(0, 16).replace("T", " ") + " IST";
  }
}

function priceText(p, error) {
  if (error) return `<span class="pill bad">${esc(error)}</span>`;
  const px = obj(p);
  const when = px.fetched_at ? `fetched ${esc(fetchedIst(px.fetched_at))}` : "fetch time unknown";
  return `${rs(px.close)} · NSE close of ${esc(px.price_date || "-")} · ${when}`;
}

function priceBox(symbol) {
  return `<div class="small muted" id="priceInfo" data-symbol="${esc(symbol)}">Current price (last NSE end-of-day close, not a live quote):
    <span id="priceText">Loading…</span> <a href="#" id="priceRefresh">Refresh</a></div>`;
}

function bindPrice(symbol) {
  const a = $("#priceRefresh"), txt = $("#priceText");
  if (!a || !txt) return;
  a.onclick = async (e) => {
    e.preventDefault();
    const box = $("#priceInfo"), txt = $("#priceText");
    if (!txt) return;
    txt.textContent = "Refreshing…";
    try { const p = obj(await api("/api/price/" + encodeURIComponent(symbol)));
      if ($("#priceInfo") === box) $("#priceText", box).innerHTML = priceText(p); }
    catch (er) { if ($("#priceInfo") === box) $("#priceText", box).innerHTML = priceText(null, er.message); }
  };
}

async function loadPrice(symbol) {
  const box = $("#priceInfo");
  if (!box || box.dataset.symbol !== symbol) return;
  try { const p = obj(await api("/api/price/" + encodeURIComponent(symbol)));
    if ($("#priceInfo") === box) $("#priceText", box).innerHTML = priceText(p); }
  catch (e) { if ($("#priceInfo") === box) $("#priceText", box).innerHTML = priceText(null, e.message); }
}

async function openAnalysis(sym) {
  showModal(`<div class="muted">Analysing ${esc(sym)} from NSE data and filings…</div>`);
  try {
    const a = obj(await api("/api/analyse/" + encodeURIComponent(sym)));
    if (a.error) return showModal(`<div class="notice bad">${esc(a.error)}</div>`);
    showModal(`<div class="small muted">Signals today: ${esc(arr(a.signals_fired).join(", ") || "none")} · regime ${esc(obj(a.regime).state || "-")}</div>` +
      priceBox(sym) +
      planView(a.plan, { market_facts: [] }, { actions: `<button type="button" class="btn" id="mWatch">+ Watchlist</button><button type="button" class="btn primary" id="mPdf">PDF</button>` }));
    bindPrice(sym);
    loadPrice(sym);
    const mPdf = $("#mPdf"), mWatch = $("#mWatch");
    if (mPdf) mPdf.onclick = () => pdfDownload(mPdf, () => chatPdf("pdf " + sym));
    if (mWatch) mWatch.onclick = async () => {
      try {
        await post("/api/watchlist", { symbol: sym });
        mWatch.textContent = "Added ✓"; mWatch.disabled = true;
        if (currentTab() === "watch") runLoader("watch");
      } catch (e) { alert(e.message); }
    };
  } catch (e) { showModal(`<div class="notice bad">${esc(e.message)}</div>`); }
}

// ------------------------------------------------------------------ modal (Escape closes, focus moves in and is restored)
let modalReturnFocus = null;
const modalOpen = () => !$("#modal").classList.contains("hidden");
function showModal(html) {
  $("#modalbody").innerHTML = html;
  if (!modalOpen()) {
    modalReturnFocus = document.activeElement;
    $("#modal").classList.remove("hidden");
    document.body.classList.add("modal-open");
  }
  $("#modalbox").focus();
}
function closeModal() {
  if (!modalOpen()) return;
  $("#modal").classList.add("hidden");
  document.body.classList.remove("modal-open");
  if (modalReturnFocus && document.contains(modalReturnFocus) && modalReturnFocus.focus) modalReturnFocus.focus();
  modalReturnFocus = null;
}
$("#modalx").onclick = closeModal;
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => {
  if (!modalOpen()) return;
  if (e.key === "Escape") { e.preventDefault(); closeModal(); return; }
  if (e.key !== "Tab") return;
  const f = [...$("#modalbox").querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea, summary, [tabindex]:not([tabindex="-1"])')]
    .filter((x) => x.offsetParent !== null);
  if (!f.length) { e.preventDefault(); return; }
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && (document.activeElement === first || document.activeElement === $("#modalbox"))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  else if (!$("#modalbox").contains(document.activeElement)) { e.preventDefault(); first.focus(); }
});

// ------------------------------------------------------------------ CALLS
loaders.calls = async function () {
  const el = $("#tab-calls");
  const [rows0, sum0] = await Promise.all([api("/api/calls"), api("/api/summary")]);
  const rows = arr(rows0), sum = obj(sum0);
  const open = rows.filter((r) => ["open", "planned"].includes(r.status));
  const past = rows.filter((r) => !["open", "planned"].includes(r.status));
  el.innerHTML = `
  <div class="card"><h2>Track record (paper)</h2><div class="kpis">
    <div class="kpi"><div class="l">Closed calls</div><div class="v">${sum.closed || 0}</div></div>
    <div class="kpi"><div class="l">Win rate</div><div class="v">${pct(sum.win_rate, 0)}</div></div>
    <div class="kpi"><div class="l">Avg win / loss</div><div class="v"><span class="pos">${pct(sum.avg_win)}</span> / <span class="neg">${pct(sum.avg_loss)}</span></div></div>
    <div class="kpi"><div class="l">Expectancy / trade</div><div class="v ${cls(sum.expectancy)}">${pct(sum.expectancy, 2)}</div></div>
    <div class="kpi"><div class="l">Net P&amp;L (after charges)</div><div class="v ${cls(sum.total_pnl)}">${rs(sum.total_pnl)}</div></div>
    <div class="kpi"><div class="l">Open / planned</div><div class="v">${sum.open || 0} / ${sum.planned || 0}</div></div></div>
    <div class="small muted">${rows.length ? "" : "No calls yet. "}Compare these with the Strategy Lab backtest. If live expectancy stays well below the backtest after ~30 trades, the kill-switch rule says pause.</div></div>
  <div class="card"><h2>Open &amp; planned</h2>${callsTable(open, true, "No open or planned calls. A paper call is created automatically when the daily scan finds a pick.")}</div>
  <div class="card"><h2>Old calls</h2>${callsTable(past, false, "No closed, expired or cancelled calls yet.")}</div>`;
  el.querySelectorAll("[data-call]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openCall(a.dataset.call); }));
  el.querySelectorAll("[data-price]").forEach(async (cell) => {
    const sym = cell.dataset.price;
    try {
      const p = obj(await api("/api/price/" + encodeURIComponent(sym)));
      if (document.contains(cell)) cell.innerHTML = `${rs(p.close)}<div class="small muted">NSE close ${esc(p.price_date || "-")} · fetched ${esc(p.fetched_at ? fetchedIst(p.fetched_at) : "time unknown")}</div>`;
    } catch (e) {
      if (document.contains(cell)) cell.innerHTML = `${rs(cell.dataset.last)}<div class="small muted">${esc(cell.dataset.lastdate || "date unknown")} · fetch time unavailable</div>`;
    }
  });
};

function callsTable(rows, live, emptyText) {
  if (!rows.length) return `<div class="muted small">${esc(emptyText || "None yet.")}</div>`;
  return `<div class="tablewrap"><table><tr><th>#</th><th>Stock</th><th>Status</th><th>Signal</th><th class="n">Entry</th><th class="n">Stop</th>
    <th class="n">T1 / T2</th><th class="n">${live ? "Last" : "Exit"}</th><th class="n">Return</th><th>${live ? "Mode" : "Reason"}</th></tr>
    ${rows.map((r) => { const ps = obj(r.plan_summary); return `<tr><td><a href="#" data-call="${esc(r.id)}">${esc(r.id)}</a></td><td><a href="#" data-call="${esc(r.id)}">${esc(r.symbol)}</a></td>
      <td><span class="pill ${r.status === "open" ? "good" : r.status === "planned" ? "warn" : ""}">${esc(r.status)}</span></td>
      <td>${esc(r.signal_date)}</td><td class="n">${rs(r.entry_price)}</td><td class="n">${rs(r.stop)}</td>
      <td class="n">${rs(ps.target1)} / ${rs(ps.target2)}</td>
      <td class="n" ${live ? `data-price="${esc(r.symbol)}" data-last="${esc(r.last_price ?? "")}" data-lastdate="${esc(r.last_date || "")}"` : ""}>${rs(live ? r.last_price : r.exit_price)}${live ? `<div class="small muted">${esc(r.last_date || "date unknown")} · fetch time loading…</div>` : ""}</td>
      <td class="n ${cls(r.ret)}">${pct(r.ret)}</td>
      <td class="small">${esc(live ? r.mode : r.exit_reason || "")}</td></tr>`; }).join("")}</table></div>`;
}

async function openCall(id) {
  showModal(`<div class="muted">Loading call #${esc(id)}…</div>`);
  let c;
  try { c = obj(await api("/api/calls/" + encodeURIComponent(id))); } catch (e) { return showModal(`<div class="notice bad">${esc(e.message)}</div>`); }
  const can = { planned: ["buy", "cancel", "live"], open: ["sell", "average", "live"] }[c.status] || [];
  const events = arr(c.events);
  showModal(`<div class="row" style="justify-content:space-between"><h2 style="margin:0">Call #${esc(c.id)} · ${esc(c.symbol)} <span class="pill">${esc(c.status)}</span> <span class="pill">${esc(c.mode)}</span></h2>
    <button type="button" class="btn" id="cPdf">PDF</button></div>
    <div class="kpis" style="margin:10px 0">
      <div class="kpi"><div class="l">Entry</div><div class="v">${rs(c.entry_price)} × ${c.qty || 0}</div></div>
      <div class="kpi"><div class="l">Open qty</div><div class="v">${c.qty_open || 0}</div></div>
      <div class="kpi"><div class="l">Current stop</div><div class="v">${rs(c.stop)}</div></div>
      <div class="kpi"><div class="l">Last</div><div class="v">${rs(c.last_price)} <span class="small muted">${esc(c.last_date || "")}</span></div></div>
      <div class="kpi"><div class="l">Charges</div><div class="v">${rs(c.charges)}</div></div></div>
    ${priceBox(c.symbol)}
    ${can.length ? `<form class="row card" id="aForm"><b>Log action</b>
      <select id="aAct" class="inp" aria-label="Action">${can.map((a) => `<option>${a}</option>`).join("")}</select>
      <input id="aPx" class="inp" type="number" step="0.05" min="0" placeholder="price" aria-label="Price in rupees (needed for buy, sell, average)">
      <input id="aQty" class="inp" type="number" min="1" step="1" placeholder="qty (optional)" aria-label="Quantity (optional)">
      <button type="submit" class="btn primary" id="aGo">Save</button><span id="aMsg" class="small" role="status"></span></form>` : ""}
    <h3>Timeline</h3>${events.map((e) => `<div class="ev"><b>${esc(e.date)}</b> <span class="pill">${esc(e.kind)}</span> ${esc(e.message)}</div>`).join("") || `<div class="muted small">No events yet.</div>`}
    <h3>Original plan</h3>${planView(c.plan, null)}`);
  bindPrice(c.symbol);
  loadPrice(c.symbol);
  const cPdf = $("#cPdf");
  if (cPdf) cPdf.onclick = () => pdfDownload(cPdf, async () => obj(await post(`/api/pdf/call/${encodeURIComponent(id)}`)).pdf);
  const form = $("#aForm");
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    const act = $("#aAct").value, px = Number($("#aPx").value), qty = Number($("#aQty").value);
    const msg = $("#aMsg");
    if (["buy", "sell", "average"].includes(act) && !(px > 0)) { msg.textContent = "Enter the fill price."; $("#aPx").focus(); return; }
    if (act === "cancel" && !confirm(`Cancel planned call #${c.id} ${c.symbol}? This cannot be undone.`)) return;
    try {
      await post(`/api/calls/${encodeURIComponent(id)}/action`, { action: act, price: px > 0 ? px : null, qty: qty > 0 ? Math.floor(qty) : null });
      openCall(id); runLoader("calls");
    } catch (er) { msg.textContent = er.message; }
  };
}

// ------------------------------------------------------------------ WATCHLIST
loaders.watch = async function () {
  const el = $("#tab-watch");
  const w = obj(await api("/api/watchlist"));
  const manual = arr(w.manual), upcoming = arr(w.upcoming), toAvg = arr(w.to_average);
  const sigs = Object.entries(obj(w.signals));
  const noScan = !w.data_date;
  el.innerHTML = `
  <div class="card"><h2>My watchlist</h2>
    <form class="row" id="wForm"><input class="inp" id="wSym" placeholder="Symbol or name" aria-label="Stock symbol or company name" required style="width:220px;max-width:100%">
      <button type="submit" class="btn primary">Add</button><span id="wMsg" class="small" role="status"></span></form>
    ${manual.length ? `<div class="tablewrap"><table><tr><th>Stock</th><th>Industry</th><th class="n">Close</th><th class="n">Day</th><th>Date</th><th><span class="sr-only">Actions</span></th></tr>
      ${manual.map((r) => `<tr><td><a href="#" data-an="${esc(r.symbol)}">${esc(r.symbol)}</a> <span class="small muted">${esc(r.name || "")}</span></td>
        <td class="small">${esc(r.industry || "")}</td><td class="n">${rs(r.close)}</td><td class="n ${cls(r.chg)}">${pct(r.chg, 2)}</td><td>${esc(r.date || "")}</td>
        <td><button type="button" class="btn" data-del="${esc(r.symbol)}" aria-label="Remove ${esc(r.symbol)} from watchlist">Remove</button></td></tr>`).join("")}</table></div>`
      : `<div class="muted small">Empty. Add a stock by NSE symbol or company name to analyse it any time.</div>`}</div>
  <div class="card"><h2>Upcoming (within 3% of a 52-week-high breakout) <span class="small muted">NSE close ${esc(w.data_date || "-")}</span></h2>
    ${upcoming.length ? `<div class="tablewrap"><table><tr><th>Stock</th><th>Industry</th><th class="n">Close</th><th class="n">52w high</th><th class="n">Away</th><th class="n">Vol ×avg</th></tr>
      ${upcoming.map((u) => `<tr><td><a href="#" data-an="${esc(u.symbol)}">${esc(u.symbol)}</a> <span class="small muted">${esc(u.name)}</span></td><td class="small">${esc(u.industry)}</td>
        <td class="n">${rs(u.close)}</td><td class="n">${rs(u.hi252)}</td><td class="n">${isNum(u.gap_pct) ? esc(u.gap_pct) + "%" : "-"}</td><td class="n">${num(u.vol_ratio)}</td></tr>`).join("")}</table></div>`
      : `<div class="muted small">${noScan ? "Run a scan first (Today → Sync + scan now)." : "No stock is within 3% of its 52-week high on this close."}</div>`}
    <div class="small muted">Not buy signals. They become signals only if they close above the 52-week high on 1.5× volume with all gates passing.</div></div>
  <div class="card"><h2>Stocks to average (open positions below entry)</h2>
    ${toAvg.length ? `<div class="tablewrap"><table><tr><th>Call</th><th>Cause of fall</th><th>Higher low</th><th>Above 20-DMA</th><th>Verdict</th></tr>
      ${toAvg.map((r) => `<tr><td>#${esc(r.id)} ${esc(r.symbol)}</td><td class="small">${esc(r.cause)}</td><td>${r.higher_low ? "yes" : "no"}</td><td>${r.above_20dma ? "yes" : "no"}</td>
        <td>${r.averaged ? `<span class="pill">already averaged</span>` : r.average_ok ? `<span class="pill good">one tranche allowed</span>` : String(r.cause || "").startsWith("stock") ? `<span class="pill bad">exit, don't average</span>` : `<span class="pill warn">wait</span>`}</td></tr>`).join("")}</table></div>`
      : `<div class="muted small">No open position is more than 5% below its entry.</div>`}
    <div class="small muted">Rule: average only when the fall is market-wide, the thesis is intact, the stock made a higher low and closed back above its 20-DMA. Once, size ≤ original. This rule is from the research plan and is not backtested.</div></div>
  <div class="card"><h2>Today's signals by strategy</h2>${sigs.map(([k, v]) =>
      `<div class="small"><b>${esc(k)}</b>: ${arr(v).length ? arr(v).map((s) => `<a href="#" data-an="${esc(s)}">${esc(s)}</a>`).join(", ") : "none"}</div>`).join("")
      || `<div class="muted small">${noScan ? "Run a scan first (Today → Sync + scan now)." : "No signals in the latest scan."}</div>`}</div>`;
  $("#wForm").onsubmit = async (e) => {
    e.preventDefault();
    const v = $("#wSym").value.trim();
    if (!v) { $("#wMsg").textContent = "Type a symbol or company name."; return; }
    $("#wMsg").textContent = "Adding…";
    try { await post("/api/watchlist", { symbol: v }); runLoader("watch"); } catch (er) { $("#wMsg").textContent = er.message; }
  };
  el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { await api("/api/watchlist/" + encodeURIComponent(b.dataset.del), { method: "DELETE" }); runLoader("watch"); }
    catch (e) { b.disabled = false; alert(e.message); }
  }));
  bindAnalyse(el);
};

// ------------------------------------------------------------------ STRATEGY LAB
loaders.lab = async function () {
  const el = $("#tab-lab");
  const b = obj(await api("/api/backtests"));
  const results = arr(b.results).map((r) => ({ ...r, metrics: obj(r.metrics) }));
  const job = window.__job || {};
  const running = !!(job.name && !job.finished);
  const metricRows = [["trades", "Trades", (v) => esc(v)], ["win_rate", "Win rate", (v) => pct(v, 0)], ["avg_win", "Avg win", (v) => pct(v)], ["avg_loss", "Avg loss", (v) => pct(v)],
    ["expectancy", "Expectancy/trade", (v) => pct(v, 2)], ["profit_factor", "Profit factor", (v) => num(v)], ["avg_hold_days", "Avg hold (sessions)", (v) => num(v, 0)],
    ["max_losing_streak", "Longest losing streak", (v) => esc(v)], ["max_drawdown", "Max drawdown", (v) => pct(v, 0)],
    ["cagr_after_cost_tax", "CAGR after cost+tax", (v) => pct(v)], ["bench_cagr", "Nifty 500 CAGR (price)", (v) => pct(v)], ["t1_hit_rate", "T1 hit rate", (v) => pct(v, 0)],
    ["t1_days_median", "Median sessions to T1", (v) => num(v, 0)], ["t2_days_median", "Median sessions to T2", (v) => num(v, 0)],
    ["bench_max_drawdown", "Nifty 500 max drawdown", (v) => pct(v, 0)], ["avg_capital_deployed", "Avg capital deployed", (v) => pct(v, 0)]];
  const act = results.find((r) => r.strategy === b.active);
  let verdict = "";
  if (act) {
    const f = obj(act.metrics.full), c = obj(act.metrics.with_cash_yield);
    const beat = isNum(f.cagr_after_cost_tax) && isNum(f.bench_cagr) && f.cagr_after_cost_tax > f.bench_cagr;
    verdict = `<div class="notice ${beat ? "good" : "bad"}"><b>Honest comparison (${esc(act.strategy)}, Jan 2021 – now):</b>
      ${pct(f.cagr_after_cost_tax)} a year after costs and tax (${pct(c.cagr_after_cost_tax)} if idle cash earns ~5% in a liquid/arbitrage fund)
      vs ${pct(f.bench_cagr)} for simply holding the Nifty 500 (price index, before dividends).
      Max drawdown ${pct(f.max_drawdown, 0)} vs ${pct(f.bench_max_drawdown, 0)} for the index. Average capital deployed ${pct(f.avg_capital_deployed, 0)}.
      ${beat ? "The strategy beat buy-and-hold in this sample." : "The strategy did NOT beat buy-and-hold in this sample."}
      ${isNum(f.max_drawdown) && isNum(f.bench_max_drawdown) ? (f.max_drawdown > f.bench_max_drawdown ? " Its worst drawdown was shallower than the index's." : " Its worst drawdown was also deeper than the index's.") : ""}
      ${beat ? "" : " This matches the research plan: keep most money in an index core and treat this as a small, rule-based satellite."}</div>`;
  }
  const trades = arr(b.trades);
  const tradeRows = (list) => list.slice().sort((x, y) => String(y.exit_date || y.entry_date || "").localeCompare(String(x.exit_date || x.entry_date || "")))
    .map((t) => `<tr><td>${esc(t.symbol)}</td><td>${esc(t.entry_date)}</td><td>${esc(t.exit_date)}</td>
      <td class="n">${rs(t.entry)}</td><td class="n">${rs(t.avg_exit)}</td><td class="n ${cls(t.ret)}">${pct(t.ret, 2)}</td>
      <td class="n">${esc(dash(t.hold_days))}</td><td class="small">${esc(t.reason || "-")}</td></tr>`).join("")
    || `<tr><td colspan="8" class="muted">No trades match this filter.</td></tr>`;
  const stockRows = (list) => {
    const by = new Map();
    list.forEach((t) => {
      const k = String(t.symbol || "").toUpperCase(), g = by.get(k) || { symbol: k, n: 0, wins: 0, ret: 0 };
      g.n++; g.wins += Number(t.pnl) > 0 ? 1 : 0; g.ret += Number(t.ret) || 0; by.set(k, g);
    });
    const groups = [...by.values()].sort((x, y) => y.n - x.n || x.symbol.localeCompare(y.symbol));
    return groups.map((g) => `<tr><td>${esc(g.symbol)}</td><td class="n">${g.n}</td><td class="n">${g.wins}</td>
      <td class="n">${pct(g.n ? g.wins / g.n : null, 0)}</td><td class="n ${cls(g.ret)}">${pct(g.ret, 2)}</td></tr>`).join("")
      || `<tr><td colspan="5" class="muted">No stocks match this filter.</td></tr>`;
  };
  const renderTradeTables = (filter) => {
    const q = String(filter || "").trim().toUpperCase();
    const shown = trades.filter((t) => String(t.symbol || "").toUpperCase().includes(q));
    const tb = $("#tradeBody"), sb = $("#stockBody");
    if (tb) tb.innerHTML = tradeRows(shown);
    if (sb) sb.innerHTML = stockRows(shown);
  };
  const distinctStocks = new Set(trades.map((t) => String(t.symbol || "").toUpperCase())).size;
  const wins = trades.filter((t) => Number(t.pnl) > 0).length;
  const avgReturn = trades.length ? trades.reduce((s, t) => s + (Number(t.ret) || 0), 0) / trades.length : null;
  const tradeSection = `<div class="card"><h2>Trades of ${esc(b.active || "-")}</h2>
    <div class="small">${trades.length} trades · ${distinctStocks} distinct stocks · win rate ${pct(trades.length ? wins / trades.length : null, 0)} · average return ${pct(avgReturn, 2)}</div>
    <label class="small">Filter by stock <input class="inp" id="tradeSearch" aria-label="Filter trades by stock symbol" placeholder="e.g. TCS"></label>
    <div class="tablewrap"><table><tr><th>Stock</th><th>Entry date</th><th>Exit date</th><th class="n">Entry Rs</th>
      <th class="n">Exit Rs</th><th class="n">Return %</th><th class="n">Hold (sessions)</th><th>Exit reason</th></tr>
      <tbody id="tradeBody"></tbody></table></div>
    <h3>Per stock</h3><div class="tablewrap"><table><tr><th>Stock</th><th class="n">Trades</th><th class="n">Wins</th>
      <th class="n">Win rate</th><th class="n">Total return %</th></tr><tbody id="stockBody"></tbody></table></div>
    <div class="small muted">Newest exit first. Search matches stock symbols case-insensitively and applies to both tables.</div></div>`;
  let html = `<div class="card"><div class="plan-head"><h2>Strategy discovery (NSE data from Jan 2020, trades from Jan 2021)</h2>
    <button type="button" class="btn primary" id="btnBt" data-job ${running ? "disabled" : ""}>Run backtests</button></div>
    <div class="small muted">Signal at close, buy next open (skipped above the no-chase price), stop 2.5×ATR, book ⅓ at T1 (1.5R) and ⅓ at T2 (3R), trail 3×ATR, time exit day 45.
    Costs: STT, stamp, exchange, SEBI, GST, DP + 0.10% slippage per side. Tax: 20% STCG per financial year. Regime gate applies unless stated.
    The winner must have positive expectancy in BOTH 2021–23 and 2024–now and stay positive with stops ±20%; among those, the best worst-period return wins.</div>
    <div class="notice">Limits: today's Nifty 500 list is used for history (survivorship bias, flatters results). No 2008-style crash in the sample. Benchmark is the price index without dividends (~1.2%/yr lower than TRI).</div>
    ${b.winner ? `<div class="notice good">Robust winner: <b>${esc(b.winner)}</b>. Active strategy: <b>${esc(b.active)}</b>.</div>` : results.length ? `<div class="notice bad">No strategy passed every robustness test. Active strategy: <b>${esc(b.active)}</b>. Treat picks with extra caution.</div>` : ""}
    ${verdict}
    ${running || job.name === "backtest" ? `<div class="small muted">Job log</div><div class="joblog" data-live>${esc(jobText(job))}</div>` : ""}</div>`;
  html += tradeSection;
  if (!results.length) html += `<div class="card muted">No backtest yet. Sync NSE data first (Sync + scan now), then click “Run backtests” (takes a few minutes). Until then the default strategy <b>${esc(b.active || "-")}</b> is used.</div>`;
  else {
    const n = results.length + 1;
    html += `<div class="card tablewrap"><table><tr><th>Metric</th>${results.map((r) => `<th class="n">${esc(r.strategy)}${r.strategy === b.active ? " ★<span class=\"sr-only\"> (active)</span>" : ""}</th>`).join("")}</tr>
      ${["full", "early", "recent"].map((per) => `<tr><th colspan="${n}">${{ full: "Full period", early: "2021–2023", recent: "2024 – now" }[per]}</th></tr>` +
        metricRows.filter((m) => per === "full" || ["trades", "win_rate", "expectancy", "max_drawdown", "cagr_after_cost_tax", "bench_cagr"].includes(m[0])).map(([k, lab, f]) =>
          `<tr><td>${lab}</td>${results.map((r) => { const v = obj(r.metrics[per])[k]; return `<td class="n ${["expectancy", "cagr_after_cost_tax"].includes(k) ? cls(v) : ""}">${v == null ? "-" : f(v)}</td>`; }).join("")}</tr>`).join("")).join("")}
      <tr><th colspan="${n}">Robustness (stops/trail ×0.8 and ×1.2): CAGR</th></tr>
      ${["atr_x0.8", "atr_x1.2"].map((k) => `<tr><td>${k}</td>${results.map((r) => { const v = obj(obj(r.metrics.sensitivity)[k]).cagr_after_cost_tax; return `<td class="n ${cls(v)}">${pct(v)}</td>`; }).join("")}</tr>`).join("")}
      <tr><th colspan="${n}">Idle cash in a liquid/arbitrage fund (~5% after tax)</th></tr>
      <tr><td>CAGR after cost+tax</td>${results.map((r) => { const v = obj(r.metrics.with_cash_yield).cagr_after_cost_tax; return `<td class="n ${cls(v)}">${pct(v)}</td>`; }).join("")}</tr>
      <tr><td></td>${results.map((r) => `<td class="n">${r.strategy === b.active ? `<span class="pill good">active</span>` : `<button type="button" class="btn" data-act="${esc(r.strategy)}" aria-label="Use ${esc(r.strategy)} as the active strategy">Use</button>`}</td>`).join("")}</tr>
    </table></div>`;
    html += results.map((r) => { const full = obj(r.metrics.full); return `<div class="card"><h2>${esc(r.strategy)} · ${esc(r.name)}</h2><div class="small muted">${esc(r.description)}</div>
      <ul class="tight small">${arr(r.rules).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      ${lineChart(arr(r.equity).filter(Array.isArray).map((e) => ({ x: e[0], y: e[1] })), { label: r.strategy + " equity curve" })}
      <div class="small">Yearly: ${Object.entries(obj(full.yearly)).map(([y, v]) => `${esc(y)} <span class="${cls(v)}">${pct(v)}</span>`).join(" · ") || "-"}</div>
      <div class="small muted">Exit reasons: ${Object.entries(obj(full.exit_reasons)).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ") || "-"}</div></div>`; }).join("");
  }
  el.innerHTML = html;
  renderTradeTables("");
  const tradeSearch = $("#tradeSearch");
  if (tradeSearch) tradeSearch.oninput = () => renderTradeTables(tradeSearch.value);
  $("#btnBt").onclick = () => runJob("backtest");
  el.querySelectorAll("[data-act]").forEach((x) => (x.onclick = async () => {
    try { await post("/api/strategy/active", { key: x.dataset.act }); } catch (e) { alert(e.message); }
    runLoader("lab"); loadStatus();
  }));
};

// ------------------------------------------------------------------ CHAT
function addMsg(role, text, pdf, replace) {
  const d = replace || document.createElement("div");
  d.className = "msg " + (role === "user" ? "user" : "assistant");
  d.textContent = String(text ?? "").replace(/\*\*(.+?)\*\*/g, "$1");
  if (pdf) { const a = document.createElement("a"); a.href = "/api/pdf/" + encodeURIComponent(pdf); a.target = "_blank"; a.rel = "noopener"; a.textContent = "📄 Open PDF"; a.style.display = "block"; d.appendChild(a); }
  if (!replace) $("#chatlog").appendChild(d);
  $("#chatlog").scrollTop = 1e9;
}
loaders.chat = async function () {
  if ($("#chatlog").dataset.loaded) return;
  const h = arr(await api("/api/chat"));
  $("#chatlog").innerHTML = "";
  if (!h.length) addMsg("assistant", 'Hi. Ask "help" to see what I can do. Every number I give comes from NSE data with a date and link.');
  h.forEach((m) => addMsg(m.role, m.text, m.attachment));
  $("#chatlog").dataset.loaded = 1;
};
const SLASH_COMMANDS = [
  ["/today", "Today's pick, or why there is none"],
  ["/pick", "Same as /today"],
  ["/holdings", "Open and planned paper calls"],
  ["/average", "Open positions below entry and averaging verdict"],
  ["/upcoming", "Stocks close to a 52-week-high breakout"],
  ["/regime", "Market health: Nifty 500 vs 200-DMA and breadth"],
  ["/strategy", "Backtest results and the active strategy"],
  ["/watch SYMBOL", "Add a stock to the watchlist"],
  ["/unwatch SYMBOL", "Remove a stock from the watchlist"],
  ["/analyse SYMBOL", "Full rule-based trade-setup verdict"],
  ["/pdf SYMBOL|today", "Generate a PDF for a stock or today's pick"],
  ["/bought N at PRICE", "Log a buy on call #N"],
  ["/sold N at PRICE", "Log a sell on call #N"],
  ["/averaged N at PRICE", "Log an average on call #N"],
  ["/help", "Show all commands"],
];
let cmdActive = -1;
function closeCmdMenu() {
  const menu = $("#cmdmenu"), inp = $("#chatin");
  if (!menu || !inp) return;
  menu.classList.add("hidden");
  menu.innerHTML = "";
  inp.setAttribute("aria-expanded", "false");
  inp.removeAttribute("aria-activedescendant");
  cmdActive = -1;
}
function setCmdActive() {
  const menu = $("#cmdmenu"), inp = $("#chatin");
  menu.querySelectorAll('[role="option"]').forEach((el, i) => {
    const on = i === cmdActive;
    el.classList.toggle("active", on);
    el.setAttribute("aria-selected", on ? "true" : "false");
  });
  if (cmdActive >= 0) inp.setAttribute("aria-activedescendant", "cmdopt-" + cmdActive);
  else inp.removeAttribute("aria-activedescendant");
}
function fillSlashCommand(cmd) {
  const inp = $("#chatin");
  inp.value = cmd + " ";
  inp.focus();
  cmdActive = 0;
  renderCmdMenu();
}
function renderCmdMenu() {
  const inp = $("#chatin"), menu = $("#cmdmenu");
  if (!inp || !menu) return;
  const v = inp.value;
  if (!v.startsWith("/")) return closeCmdMenu();
  const matches = SLASH_COMMANDS.filter((c) => c[0].toLowerCase().startsWith(v.toLowerCase()));
  if (!matches.length) return closeCmdMenu();
  cmdActive = cmdActive < 0 ? 0 : Math.min(cmdActive, matches.length - 1);
  menu.innerHTML = matches.map(([cmd, help], i) => `<div id="cmdopt-${i}" role="option" aria-selected="${i === cmdActive}"
    class="${i === cmdActive ? "active" : ""}" data-cmd="${esc(cmd)}"><span class="cmd">${esc(cmd)}</span><span class="help">${esc(help)}</span></div>`).join("");
  menu.classList.remove("hidden");
  inp.setAttribute("aria-expanded", "true");
  setCmdActive();
  menu.querySelectorAll("[data-cmd]").forEach((el) => el.addEventListener("mousedown", (e) => {
    e.preventDefault();
    fillSlashCommand(el.dataset.cmd);
  }));
}
$("#chatin").addEventListener("input", renderCmdMenu);
$("#chatin").addEventListener("keydown", (e) => {
  const menu = $("#cmdmenu");
  if (!menu || menu.classList.contains("hidden")) return;
  const opts = menu.querySelectorAll('[role="option"]');
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (opts.length) cmdActive = (cmdActive + (e.key === "ArrowDown" ? 1 : -1) + opts.length) % opts.length;
    setCmdActive();
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (opts[cmdActive]) fillSlashCommand(opts[cmdActive].dataset.cmd);
  } else if (e.key === "Escape") {
    e.preventDefault();
    closeCmdMenu();
  }
});
document.addEventListener("click", (e) => { if (!e.target.closest(".chatinwrap")) closeCmdMenu(); });
$("#chatform").onsubmit = async (e) => {
  e.preventDefault();
  const v = $("#chatin").value.trim();
  if (!v) return;
  closeCmdMenu();
  $("#chatin").value = "";
  addMsg("user", v);
  const wait = document.createElement("div"); wait.className = "msg assistant muted"; wait.textContent = "Working…"; $("#chatlog").appendChild(wait);
  // the reply fills its own placeholder, so answers stay next to their questions even if a later one is faster
  try { const r = obj(await post("/api/chat", { message: v })); addMsg("assistant", r.text || "(no reply)", r.pdf, wait); }
  catch (er) { addMsg("assistant", "Error: " + er.message, null, wait); }
};

// ------------------------------------------------------------------ DATA
loaders.data = async function () {
  const el = $("#tab-data");
  const s = obj(await api("/api/sources"));
  const summary = arr(s.summary), recent = arr(s.recent), rejected = arr(s.rejected), uni = obj(s.universe);
  const statusPill = (st) => `<span class="pill ${st === "ok" ? "good" : st === "date_mismatch" ? "bad" : ""}">${esc(st || "-")}</span>`;
  const noData = `<div class="muted small">No NSE files synced yet. Click “Sync + scan now” at the top of the page.</div>`;
  el.innerHTML = `<div class="card"><h2>Data freshness &amp; two-date verification</h2>
    <div class="small muted">Every NSE file is accepted only if the date in its file name equals the date inside the file and equals the trading day requested. Rejected files are listed below and never used.</div>
    ${summary.length ? `<div class="tablewrap"><table><tr><th>Feed</th><th>Status</th><th class="n">Files</th><th>First</th><th>Last</th></tr>
    ${summary.map((r) => `<tr><td>${esc(r.kind)}</td><td>${statusPill(r.status)}</td><td class="n">${esc(dash(r.n))}</td><td>${esc(r.first)}</td><td>${esc(r.last)}</td></tr>`).join("")}</table></div>` : noData}
    <div class="small">Universe: ${uni.url ? `<a href="${esc(uni.url)}" target="_blank" rel="noopener">Nifty 500 list</a>` : "Nifty 500 list (not fetched yet)"} (${esc(uni.count || "-")} stocks, fetched ${esc((uni.fetched_at || "").slice(0, 16).replace("T", " ") || "-")}) ·
      Holidays: ${s.holidays_url ? `<a href="${esc(s.holidays_url)}" target="_blank" rel="noopener">NSE holiday master</a>` : "-"}</div></div>
  <div class="card"><h2>Latest files</h2>${recent.length ? `<div class="tablewrap"><table><tr><th>Feed</th><th>Trading day</th><th>Label date</th><th>Content date</th><th>Fetched</th><th>Status</th><th class="n">Rows</th><th>Source</th></tr>
    ${recent.map((r) => `<tr><td>${esc(r.kind)}</td><td>${esc(r.date)}</td><td>${esc(r.label_date)}</td><td>${esc(r.content_date)}</td><td class="small">${esc((r.fetched_at || "").slice(0, 16))}</td>
      <td>${statusPill(r.status)}</td><td class="n">${esc(dash(r.rows))}</td><td>${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">link</a>` : "-"}</td></tr>`).join("")}</table></div>` : noData}</div>
  <div class="card"><h2>Rejected by the two-date check</h2>${rejected.length ? `<div class="tablewrap"><table><tr><th>Feed</th><th>Day</th><th>Detail</th></tr>${rejected.map((r) => `<tr><td>${esc(r.kind)}</td><td>${esc(r.date)}</td><td class="small">${esc(r.detail)}</td></tr>`).join("")}</table></div>` : `<div class="muted small">None.</div>`}
    <div class="small muted">Typically exchange holidays, where NSE serves a previous day's file under the holiday's name.</div></div>
  <div class="card"><h2>Source tiers</h2><ul class="tight small">
    <li><b>Tier 1 (1.0)</b>: NSE/BSE files and filings, SEBI, RBI, AMFI, NSE Indices, rating agencies. Only Tier 1 feeds prices and trade levels.</li>
    <li><b>Tier 2 (0.8)</b>: Reuters, Bloomberg, Business Standard, ET, Mint, BusinessLine, FE, Moneycontrol. Context only.</li>
    <li><b>Tier 3 (0.5)</b>: aggregators and broker blogs. Not shown. <b>Tier 4</b> (social media, Telegram, YouTube, forums): blocked.</li>
    <li>Score ≥ 0.85 = fact · 0.60–0.85 = unverified (not used for decisions) · &lt; 0.60 = discarded. Unverified bad news can block a buy; unverified good news can never trigger one.</li></ul></div>`;
};

// ------------------------------------------------------------------ boot
(async function () {
  initStatusbar();
  await loadStatus();
  let t = "dashboard";
  try { t = localStorage.getItem("tab") || "today"; } catch (e) {}
  const b = $(`#tabs button[data-tab="${CSS.escape(t)}"]`) || $("#tabs button");
  b.click();
})();
