const app = document.getElementById("app");
let currentPage = "dashboard";

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}

/* ---------- shared display helpers ---------- */

function dirLabel(direction) {
  if (direction === "UP") return `<span class="dir dir-up">&#9650; UP</span>`;
  if (direction === "DOWN") return `<span class="dir dir-down">&#9660; DOWN</span>`;
  return esc(direction || "-");
}

function tradeBadge(status) {
  const map = {
    WON: ["badge-win", "Won"],
    LOST: ["badge-loss", "Lost"],
    OPEN: ["badge-pending", "Pending"],
    PENDING: ["badge-pending", "Pending"],
  };
  const [cls, label] = map[status] || ["badge-neutral", status || "-"];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function signalBadge(status) {
  const map = {
    QUALIFIED: ["badge-pending", "Qualified"],
    EXECUTED: ["badge-neutral", "Executed"],
    PARTIALLY_EXECUTED: ["badge-pending", "Partial"],
    EXECUTION_ERROR: ["badge-error", "Error"],
    NO_SIGNAL: ["badge-neutral", "No signal"],
  };
  const [cls, label] = map[status] || ["badge-neutral", status || "-"];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function pnlClass(value) {
  const n = Number(value);
  return n > 0 ? "dir-up" : n < 0 ? "dir-down" : "";
}

/* ---------- persistent chrome (topbar) ---------- */

function updateTopbar(s) {
  const dot = document.getElementById("connDot");
  const label = document.getElementById("connLabel");
  const modeBadge = document.getElementById("modeBadge");
  if (!dot || !label || !modeBadge) return;
  const statusMap = {
    RUNNING: ["running", "Connected"],
    STARTING: ["reconnecting", "Starting"],
    RECONNECTING: ["reconnecting", "Reconnecting"],
    STOPPED: ["stopped", "Stopped"],
  };
  const [cls, text] = statusMap[s.bot_status] || ["stopped", s.bot_status || "Unknown"];
  dot.className = "conn-dot " + cls;
  label.textContent = text;
  modeBadge.textContent = esc(s.mode).toUpperCase();
  modeBadge.classList.toggle("live", s.mode === "live");
}

async function refreshTopbar() {
  try {
    const s = await getJSON("/api/status");
    updateTopbar(s);
  } catch (e) { /* transient network hiccup -- next poll will retry */ }
}

/* ---------- 180s candle countdown ring (dashboard only) ---------- */

const RING_CIRCUMFERENCE = 2 * Math.PI * 38;

function updateCountdownRing() {
  const ring = document.getElementById("ringProgress");
  const label = document.getElementById("ringLabel");
  if (!ring || !label) return;
  const period = 180;
  const now = Math.floor(Date.now() / 1000);
  const elapsed = now % period;
  const remaining = period - elapsed;
  const progress = elapsed / period;
  ring.style.strokeDasharray = `${RING_CIRCUMFERENCE}`;
  ring.style.strokeDashoffset = `${RING_CIRCUMFERENCE * (1 - progress)}`;
  const m = Math.floor(remaining / 60);
  const sec = remaining % 60;
  label.textContent = `${m}:${String(sec).padStart(2, "0")}`;
}

/* ---------- pages ---------- */

async function renderDashboard() {
  const s = await getJSON("/api/status");
  updateTopbar(s);
  const t = s.today;
  const signal = s.current_signal;
  const hasSignal = signal && signal.status !== "NO_SIGNAL";
  app.innerHTML = `
    <div class="card hero">
      <div class="ring-wrap">
        <svg viewBox="0 0 88 88">
          <circle class="ring-track" cx="44" cy="44" r="38"/>
          <circle class="ring-progress" id="ringProgress" cx="44" cy="44" r="38"/>
        </svg>
        <div class="ring-label" id="ringLabel">--</div>
      </div>
      <div class="hero-body">
        <div class="hero-status">${esc(s.bot_status)}</div>
        <div class="muted">${esc(s.symbol)} &middot; ${esc(s.timeframe_seconds)}s candles &middot; barrier ${Number(s.barrier_atr_fraction).toFixed(2)}&times; ATR</div>
        ${hasSignal ? `
          <div class="hero-detail">
            <div class="detail-item"><span class="detail-label">Direction</span><span class="detail-value">${dirLabel(signal.direction)}</span></div>
            <div class="detail-item"><span class="detail-label">Contract</span><span class="detail-value">${esc(signal.contract_type || "-")}</span></div>
            <div class="detail-item"><span class="detail-label">Score</span><span class="detail-value">${esc(signal.score ?? "-")}</span></div>
            <div class="detail-item"><span class="detail-label">Barrier</span><span class="detail-value">${signal.barrier_offset != null ? Number(signal.barrier_offset).toFixed(3) : "-"}</span></div>
            <div class="detail-item"><span class="detail-label">Status</span><span class="detail-value">${signalBadge(signal.status)}</span></div>
          </div>
          ${signal.reason ? `<p class="muted" style="margin-top:10px">${esc(signal.reason)}</p>` : ""}
        ` : `<p class="muted" style="margin-top:10px">Waiting for the next candle boundary.</p>`}
        ${s.last_error ? `<p style="margin-top:10px;color:var(--down);font-size:12.5px">${esc(s.last_error)}</p>` : ""}
      </div>
    </div>

    <div class="stat-grid">
      <div class="stat"><div class="stat-label">Today's PnL</div><div class="stat-value ${pnlClass(t.pnl)}">${Number(t.pnl).toFixed(2)}</div></div>
      <div class="stat"><div class="stat-label">Win Rate</div><div class="stat-value">${t.win_rate}%</div></div>
      <div class="stat"><div class="stat-label">Wins</div><div class="stat-value dir-up">${t.wins}</div></div>
      <div class="stat"><div class="stat-label">Losses</div><div class="stat-value dir-down">${t.losses}</div></div>
    </div>
  `;
  updateCountdownRing();
}

async function renderSignals() {
  const rows = await getJSON("/api/signals");
  app.innerHTML = `<div class="card">
    <h2>Signals</h2>
    ${rows.length ? `<div class="table-wrap"><table><thead><tr>
      <th>Time</th><th>Direction</th><th>Contract</th><th class="num">Barrier</th><th class="num">Score</th><th>Status</th>
    </tr></thead><tbody>
    ${rows.map(x => `<tr>
      <td class="time">${esc(x.created_at)}</td>
      <td>${dirLabel(x.direction)}</td>
      <td>${esc(x.contract_type)}</td>
      <td class="num">${x.barrier_offset != null ? Number(x.barrier_offset).toFixed(3) : "-"}</td>
      <td class="num">${esc(x.score)}</td>
      <td>${signalBadge(x.status)}</td>
    </tr>`).join("")}
    </tbody></table></div>` : `<div class="empty-state">No signals yet. They'll show up here as soon as the bot detects one.</div>`}
  </div>`;
}

async function renderTrades() {
  const rows = await getJSON("/api/trades");
  app.innerHTML = `<div class="card">
    <h2>Trades</h2>
    ${rows.length ? `<div class="table-wrap"><table><thead><tr>
      <th>Time</th><th>Mode</th><th>Direction</th><th class="num">Barrier</th><th class="num">Stake</th><th class="num">Profit</th><th>Status</th>
    </tr></thead><tbody>
    ${rows.map(x => `<tr>
      <td class="time">${esc(x.created_at)}</td>
      <td>${esc(x.mode)}</td>
      <td>${dirLabel(x.direction)}</td>
      <td class="num">${esc(x.barrier || "-")}</td>
      <td class="num">${Number(x.stake).toFixed(2)}</td>
      <td class="num ${pnlClass(x.profit)}">${x.profit != null ? Number(x.profit).toFixed(2) : "-"}</td>
      <td>${tradeBadge(x.status)}</td>
    </tr>`).join("")}
    </tbody></table></div>` : `<div class="empty-state">No trades yet.</div>`}
  </div>`;
}

function buildPnlChartSvg(points) {
  const w = 600, h = 200, pad = 12;
  const values = points.map(p => p.cum);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const range = (max - min) || 1;
  const stepX = points.length > 1 ? (w - pad * 2) / (points.length - 1) : 0;
  const coords = points.map((p, i) => {
    const x = pad + i * stepX;
    const y = h - pad - ((p.cum - min) / range) * (h - pad * 2);
    return [x, y];
  });
  const last = values[values.length - 1];
  const color = last >= 0 ? "#2dd4a7" : "#f2545b";
  const zeroY = h - pad - ((0 - min) / range) * (h - pad * 2);
  const linePath = coords.map((c, i) => `${i === 0 ? "M" : "L"}${c[0].toFixed(1)},${c[1].toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L${coords[coords.length - 1][0].toFixed(1)},${zeroY.toFixed(1)} L${coords[0][0].toFixed(1)},${zeroY.toFixed(1)} Z`;
  return `<svg id="pnlChart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <line x1="${pad}" y1="${zeroY.toFixed(1)}" x2="${w - pad}" y2="${zeroY.toFixed(1)}" style="stroke:#232b3a;stroke-width:1"/>
    <path d="${areaPath}" style="fill:${color};opacity:.14;stroke:none"/>
    <path d="${linePath}" style="fill:none;stroke:${color};stroke-width:2"/>
  </svg>`;
}

async function renderPnL() {
  const rows = await getJSON("/api/pnl-history?limit=60");
  const sorted = [...rows].sort((a, b) => a.date.localeCompare(b.date));
  let cum = 0;
  const points = sorted.map(x => { cum += Number(x.pnl || 0); return { date: x.date, cum }; });
  app.innerHTML = `
    <div class="card">
      <h2>Cumulative PnL</h2>
      ${points.length ? buildPnlChartSvg(points) : `<div class="empty-state">Not enough history yet.</div>`}
    </div>
    <div class="card">
      <h2>Daily breakdown</h2>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr>
        <th>Date</th><th class="num">Trades</th><th class="num">Wins</th><th class="num">Losses</th><th class="num">Win rate</th><th class="num">PnL</th>
      </tr></thead><tbody>
      ${rows.map(x => `<tr>
        <td class="time">${esc(x.date)}</td>
        <td class="num">${x.trades}</td>
        <td class="num">${x.wins}</td>
        <td class="num">${x.losses}</td>
        <td class="num">${x.trades ? (x.wins / x.trades * 100).toFixed(1) : "0"}%</td>
        <td class="num ${pnlClass(x.pnl)}">${Number(x.pnl).toFixed(2)}</td>
      </tr>`).join("")}
      </tbody></table></div>` : `<div class="empty-state">No trade history yet.</div>`}
    </div>
  `;
}

async function renderSettings() {
  const s = await getJSON("/api/status");
  app.innerHTML = `
    <div class="card">
      <h2>Trading mode</h2>
      <div class="settings-row">
        <div>
          <div class="settings-row-label">Demo / Live</div>
          <div class="settings-row-desc">Saves automatically and restarts the bot if it's currently running, so the new mode takes effect immediately.</div>
        </div>
        <div class="segmented" id="modeSegmented">
          <button type="button" data-mode="demo" class="${s.mode === "demo" ? "active demo" : ""}">Demo</button>
          <button type="button" data-mode="live" class="${s.mode === "live" ? "active live" : ""}">Live</button>
        </div>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-row-label">Auto-trade</div>
          <div class="settings-row-desc">Whether qualified signals execute automatically. Set via AUTO_TRADE.</div>
        </div>
        <span class="chip">${s.auto_trade ? "ON" : "OFF"}</span>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-row-label">Barrier size</div>
          <div class="settings-row-desc">Distance from spot, as a fraction of the recent average candle range. Set via BARRIER_ATR_FRACTION.</div>
        </div>
        <span class="chip">${Number(s.barrier_atr_fraction).toFixed(2)}&times; ATR</span>
      </div>
    </div>
    <div class="card">
      <h2>Diagnostics</h2>
      <p class="muted">Copies a summary of recent bot activity and errors (build version, recent events, signals, trades) so a problem can be shared without exporting raw platform logs.</p>
      <div style="margin-top:12px"><button id="copyDiagnosticsBtn" class="btn">Copy diagnostics to clipboard</button></div>
      <p class="muted" style="margin-top:20px">Live-asks Deriv what's actually valid for this account/symbol (contract types, barrier and duration limits) &mdash; requires the bot to be running.</p>
      <div style="margin-top:12px"><button id="copyContractsForBtn" class="btn">Copy contract specs (live query)</button></div>
    </div>
  `;
  document.getElementById("copyDiagnosticsBtn").onclick = copyDiagnostics;
  document.getElementById("copyContractsForBtn").onclick = copyContractsFor;
  document.querySelectorAll("#modeSegmented button").forEach(btn => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
}

async function setMode(mode) {
  if (mode === "live") {
    if (!window.confirm("Switch to LIVE mode? The bot will trade with real money on your live account.")) return;
  }
  const r = await fetch("/api/settings/mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  const data = await r.json();
  if (data.error) {
    window.alert("Could not change mode: " + data.error);
    return;
  }
  await renderSettings();
  await refreshTopbar();
}

/* ---------- diagnostics (unchanged logic, restyled buttons) ---------- */

function formatDiagnostics(d) {
  const lines = [];
  lines.push("Deriv Higher/Lower Bot diagnostics");
  lines.push(`Generated: ${d.generated_at}`);
  lines.push(`Build: ${d.build_version}`);
  lines.push(`Status: ${d.bot_status} | Mode: ${d.mode} | Symbol: ${d.symbol} | Auto-trade: ${d.auto_trade}`);
  lines.push(`Barrier ATR fraction: ${d.barrier_atr_fraction}`);
  lines.push(`Last error: ${d.last_error || "(none)"}`);
  lines.push("");
  lines.push(`Recent events (${d.recent_events.length}):`);
  d.recent_events.forEach(e => lines.push(`  [${e.created_at}] ${String(e.level).toUpperCase()} ${e.event_type}: ${e.message}`));
  lines.push("");
  lines.push(`Recent signals (${d.recent_signals.length}):`);
  d.recent_signals.forEach(x => lines.push(`  [${x.created_at}] ${x.direction} status=${x.status} score=${x.score} barrier_offset=${x.barrier_offset} reason=${x.reason}`));
  lines.push("");
  lines.push(`Recent trades (${d.recent_trades.length}):`);
  d.recent_trades.forEach(x => lines.push(`  [${x.created_at}] ${x.direction} status=${x.status} barrier=${x.barrier} stake=${x.stake} profit=${x.profit} contract=${x.contract_id}`));
  return lines.join("\n");
}

async function copyTextToClipboard(btn, text) {
  const original = btn.textContent;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      btn.textContent = "Copied!";
    } else {
      window.prompt("Copy this text:", text);
      btn.textContent = "Ready to copy";
    }
  } catch (e) {
    btn.textContent = "Failed - see console";
    console.error(e);
  }
  setTimeout(() => { btn.textContent = original; }, 2500);
}

async function copyDiagnostics() {
  const btn = document.getElementById("copyDiagnosticsBtn");
  try {
    const d = await getJSON("/api/diagnostics");
    await copyTextToClipboard(btn, formatDiagnostics(d));
  } catch (e) {
    btn.textContent = "Failed - see console";
    console.error(e);
    setTimeout(() => { btn.textContent = "Copy diagnostics to clipboard"; }, 2500);
  }
}

async function copyContractsFor() {
  const btn = document.getElementById("copyContractsForBtn");
  try {
    const d = await getJSON("/api/diagnostics/contracts-for");
    const text = d.error
      ? `contracts_for error for ${d.symbol || "?"}:\n${d.error}`
      : `contracts_for result for ${d.symbol} (generated ${d.generated_at}):\n${JSON.stringify(d.result, null, 2)}`;
    await copyTextToClipboard(btn, text);
  } catch (e) {
    btn.textContent = "Failed - see console";
    console.error(e);
    setTimeout(() => { btn.textContent = "Copy contract specs (live query)"; }, 2500);
  }
}

/* ---------- nav / routing / lifecycle ---------- */

const PAGE_TITLES = { dashboard: "Dashboard", signals: "Signals", trades: "Trades", pnl: "P&L History", settings: "Settings" };

async function render() {
  document.getElementById("pageTitle").textContent = PAGE_TITLES[currentPage] || currentPage;
  if (currentPage === "dashboard") await renderDashboard();
  if (currentPage === "signals") await renderSignals();
  if (currentPage === "trades") await renderTrades();
  if (currentPage === "pnl") await renderPnL();
  if (currentPage === "settings") await renderSettings();
}

document.querySelectorAll(".navlink").forEach(btn => btn.addEventListener("click", () => {
  document.querySelectorAll(".navlink").forEach(x => x.classList.remove("active"));
  btn.classList.add("active");
  currentPage = btn.dataset.page;
  render();
}));

document.getElementById("startBtn").onclick = async () => { await fetch("/api/bot/start", { method: "POST" }); render(); };
document.getElementById("stopBtn").onclick = async () => { await fetch("/api/bot/stop", { method: "POST" }); render(); };

render();
refreshTopbar();
setInterval(updateCountdownRing, 1000);
setInterval(() => { if (currentPage === "dashboard") renderDashboard(); else refreshTopbar(); }, 5000);
