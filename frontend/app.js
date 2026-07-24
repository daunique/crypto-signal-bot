const app = document.getElementById("app");
let currentPage = "dashboard";

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}

async function renderDashboard() {
  const s = await getJSON("/api/status");
  const t = s.today;
  const signal = s.current_signal;
  const modeBadge = document.getElementById("modeBadge");
  modeBadge.textContent = esc(s.mode).toUpperCase();
  modeBadge.classList.toggle("live", s.mode === "live");
  app.innerHTML = `
    <div class="grid">
      <div class="card"><h3>Today's PnL</h3><div class="metric">${Number(t.pnl).toFixed(2)}</div></div>
      <div class="card"><h3>Win Rate</h3><div class="metric">${t.win_rate}%</div></div>
      <div class="card"><h3>Signals Today</h3><div class="metric">${t.signals}</div></div>
      <div class="card"><h3>Bot Status</h3><div class="metric">${esc(s.bot_status)}</div></div>
    </div>
    <div class="card panel signal">
      <h2>Current Signal</h2>
      ${signal ? `<p><b>Status:</b> ${esc(signal.status)}</p>
      <p><b>Direction:</b> ${esc(signal.direction || "-")}</p>
      <p><b>Contract:</b> ${esc(signal.contract_type || "-")}</p>
      <p><b>Score:</b> ${esc(signal.score || "-")}</p>
      <p><b>Barrier offset:</b> ${signal.barrier_offset != null ? Number(signal.barrier_offset).toFixed(3) : "-"}</p>
      <p><b>Reason:</b> ${esc(signal.reason || "-")}</p>` : "<p>Waiting for the next exact candle boundary.</p>"}
    </div>
    <div class="card panel">
      <h2>System</h2>
      <p>Mode: <b>${esc(s.mode)}</b></p>
      <p>Market: <b>${esc(s.symbol)}</b></p>
      <p>Timeframe: <b>${s.timeframe_seconds}s</b></p>
      <p>Trades today: <b>${t.trades}</b> | Wins: <b>${t.wins}</b> | Losses: <b>${t.losses}</b></p>
    </div>`;
}

async function renderSignals() {
  const rows = await getJSON("/api/signals");
  app.innerHTML = `<div class="card"><h2>Signals</h2><table><thead><tr><th>Time</th><th>Direction</th><th>Contract</th><th>Barrier</th><th>Score</th><th>Status</th></tr></thead><tbody>
  ${rows.map(x => `<tr><td>${esc(x.created_at)}</td><td>${esc(x.direction)}</td><td>${esc(x.contract_type)}</td><td>${x.barrier_offset != null ? Number(x.barrier_offset).toFixed(3) : "-"}</td><td>${esc(x.score)}</td><td><span class="badge">${esc(x.status)}</span></td></tr>`).join("")}
  </tbody></table></div>`;
}

async function renderTrades() {
  const rows = await getJSON("/api/trades");
  app.innerHTML = `<div class="card"><h2>Trades</h2><table><thead><tr><th>Time</th><th>Mode</th><th>Direction</th><th>Barrier</th><th>Stake</th><th>Profit</th><th>Status</th></tr></thead><tbody>
  ${rows.map(x => `<tr><td>${esc(x.created_at)}</td><td>${esc(x.mode)}</td><td>${esc(x.direction)}</td><td>${esc(x.barrier || "-")}</td><td>${esc(x.stake)}</td><td>${esc(x.profit)}</td><td><span class="badge">${esc(x.status)}</span></td></tr>`).join("")}
  </tbody></table></div>`;
}

async function renderPnL() {
  const rows = await getJSON("/api/pnl-history?limit=365");
  const byDay = {};
  rows.forEach(x => {
    byDay[x.date] = {
      trades: Number(x.trades || 0),
      wins: Number(x.wins || 0),
      losses: Number(x.losses || 0),
      pnl: Number(x.pnl || 0)
    };
  });
  app.innerHTML = `<div class="card"><h2>Daily PnL History</h2><table><thead><tr><th>Date</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>PnL</th></tr></thead><tbody>
  ${Object.entries(byDay).sort().reverse().map(([d,x]) => `<tr><td>${d}</td><td>${x.trades}</td><td>${x.wins}</td><td>${x.losses}</td><td>${x.trades ? (x.wins/x.trades*100).toFixed(2) : "0"}%</td><td>${x.pnl.toFixed(2)}</td></tr>`).join("")}
  </tbody></table></div>`;
}

async function renderSettings() {
  const s = await getJSON("/api/status");
  app.innerHTML = `<div class="card"><h2>Settings</h2>
    <p>Strategy and execution settings are currently controlled by environment variables.</p>
    <p>Mode: <b>Demo/Live is selected server-side via BOT_MODE.</b></p>
    <p>Barrier size: <b>${Number(s.barrier_atr_fraction).toFixed(2)}&times; recent average candle range (BARRIER_ATR_FRACTION)</b></p>
    <p>API tokens are never sent to the browser.</p>
  </div>
  <div class="card">
    <h2>Diagnostics</h2>
    <p>Copies a summary of recent bot activity and errors (build version, recent events, signals, trades) so a problem can be shared without exporting raw platform logs.</p>
    <button id="copyDiagnosticsBtn">Copy diagnostics to clipboard</button>
    <p style="margin-top:16px">Live-asks Deriv what's actually valid for this account/symbol (contract types, barrier and duration limits) -- requires the bot to be running.</p>
    <button id="copyContractsForBtn">Copy contract specs (live query)</button>
  </div>`;
  document.getElementById("copyDiagnosticsBtn").onclick = copyDiagnostics;
  document.getElementById("copyContractsForBtn").onclick = copyContractsFor;
}

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

async function render() {
  document.getElementById("pageTitle").textContent = currentPage[0].toUpperCase() + currentPage.slice(1);
  if (currentPage === "dashboard") await renderDashboard();
  if (currentPage === "signals") await renderSignals();
  if (currentPage === "trades") await renderTrades();
  if (currentPage === "pnl") await renderPnL();
  if (currentPage === "settings") await renderSettings();
}

document.querySelectorAll(".nav").forEach(btn => btn.addEventListener("click", () => {
  document.querySelectorAll(".nav").forEach(x => x.classList.remove("active"));
  btn.classList.add("active");
  currentPage = btn.dataset.page;
  render();
}));

document.getElementById("startBtn").onclick = async () => { await fetch("/api/bot/start", {method: "POST"}); render(); };
document.getElementById("stopBtn").onclick = async () => { await fetch("/api/bot/stop", {method: "POST"}); render(); };

render();
setInterval(() => { if (currentPage === "dashboard") renderDashboard(); }, 5000);
