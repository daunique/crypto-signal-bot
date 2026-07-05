/* ════════════════════════════════════════════════════════
   POLYBOT TERMINAL — App Logic
   ════════════════════════════════════════════════════════ */

// ── Tab navigation ─────────────────────────────────────────
const tabButtons = document.querySelectorAll('.tab-btn');
const views = {
  overview: document.getElementById('view-overview'),
  markets:  document.getElementById('view-markets'),
  trades:   document.getElementById('view-trades'),
  settings: document.getElementById('view-settings'),
};

function showView(name){
  Object.entries(views).forEach(([key, el]) => {
    el.classList.toggle('hidden', key !== name);
  });
  tabButtons.forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
}

tabButtons.forEach(btn => {
  btn.addEventListener('click', () => showView(btn.dataset.view));
});

// ── Markets filter (segmented control) ─────────────────────
let marketFilter = 'all';
document.querySelectorAll('.seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    marketFilter = btn.dataset.filter;
    renderMarkets(window.__lastMarkets || {});
  });
});

// ── Helpers ──────────────────────────────────────────────────
const fmt = (n, d=4) => (Number(n) || 0).toFixed(d);
const fmtMoney = (n, d=2) => '$' + fmt(n, d);

function setStatus(connected){
  const dot = document.getElementById('pulse-dot');
  const label = document.getElementById('status-label');
  const wrap = document.getElementById('topbar-status');
  if (connected){
    label.textContent = 'LIVE';
    wrap.style.color = 'var(--yes)';
    dot.style.background = 'var(--yes)';
  } else {
    label.textContent = 'OFFLINE';
    wrap.style.color = 'var(--no)';
    dot.style.background = 'var(--no)';
  }
}

// ── Overview ─────────────────────────────────────────────────
async function loadSummary(){
  try{
    const r = await fetch('/api/summary');
    const d = await r.json();
    setStatus(true);

    document.getElementById('hero-balance').textContent = fmtMoney(d.balance, 2);
    document.getElementById('chip-mode').textContent = (d.capital_mode || 'FIXED');

    const dailyEl = document.getElementById('daily-pnl');
    dailyEl.textContent = (d.daily_pnl >= 0 ? '+' : '') + fmtMoney(d.daily_pnl);
    dailyEl.style.color = d.daily_pnl >= 0 ? 'var(--yes)' : 'var(--no)';

    const totalEl = document.getElementById('total-pnl');
    totalEl.textContent = (d.total_pnl >= 0 ? '+' : '') + fmtMoney(d.total_pnl);
    totalEl.style.color = d.total_pnl >= 0 ? 'var(--yes)' : 'var(--no)';

    document.getElementById('win-rate').textContent = fmt(d.win_rate, 1) + '%';
    document.getElementById('win-rate-bar').style.width = Math.min(100, d.win_rate) + '%';

    document.getElementById('trades-today').textContent = d.trades_today ?? 0;
    document.getElementById('avg-edge').textContent = fmt((d.avg_edge||0) * 100, 2) + '¢';

    const deployed = d.deployed || 0;
    const available = d.available || 0;
    const total = deployed + available || 1;
    document.getElementById('deploy-bar-fill').style.width = ((deployed/total)*100) + '%';
    document.getElementById('deployed-amt').textContent = fmtMoney(deployed);
    document.getElementById('available-amt').textContent = fmtMoney(available);

    document.getElementById('set-mode').textContent = d.capital_mode || '—';
    document.getElementById('set-unit').textContent = d.unit_size ? fmtMoney(d.unit_size) : '—';

    if (d.duration_mode && !window.__durationPending){
      window.__currentDurationMode = d.duration_mode;
      syncDurationButtons(d.duration_mode);
    }

    const breakerEl = document.getElementById('health-breaker');
    const haltPanel = document.getElementById('halt-panel');
    if (d.status === 'HALTED'){
      breakerEl.textContent = '●  HALTED';
      breakerEl.className = 'health-pill bad';
      haltPanel.style.display = 'block';
      document.getElementById('halt-reason-text').textContent =
        d.halt_reason || 'Trading halted — reason unavailable.';
    } else {
      breakerEl.textContent = '●  ARMED';
      breakerEl.className = 'health-pill';
      haltPanel.style.display = 'none';
    }

    document.getElementById('last-sync').textContent =
      'synced ' + new Date().toLocaleTimeString();
  }catch(e){
    setStatus(false);
    console.error('summary load failed', e);
  }
}

// ── Markets ──────────────────────────────────────────────────
function renderMarkets(markets){
  window.__lastMarkets = markets;
  const list = document.getElementById('markets-list');
  const entries = Object.entries(markets).filter(([id]) => {
    if (marketFilter === 'all') return true;
    // Exact suffix match, not substring — "5MIN" is literally
    // contained inside "15MIN", so id.includes(marketFilter) was
    // incorrectly showing 15-minute markets when the 5-minute
    // filter was selected (BTC_15MIN.includes("5MIN") === true).
    return id.endsWith('_' + marketFilter);
  });

  document.getElementById('markets-count').textContent = `${entries.length} active`;

  if (!entries.length){
    list.innerHTML = `<div class="empty-state">No active markets right now —<br>discovery runs every 10 seconds</div>`;
    return;
  }

  list.innerHTML = entries.map(([id, m]) => {
    const asset = id.split('_')[0];
    const dur = id.endsWith('_15MIN') ? '15 MIN' : '5 MIN';
    const yesAsk = m.yes_ask ?? 0.5;
    const noAsk  = m.no_ask ?? 0.5;
    const combined = yesAsk + noAsk;
    const edge = 1 - combined;
    // Each side's width is its own price as % of $1 — so the gap between
    // the end of YES+NO and the 100% mark visually IS the edge.
    const yesPct = Math.min(98, Math.max(2, yesAsk * 100));
    const noPct  = Math.min(98, Math.max(2, noAsk * 100));
    const secsLeft = m.seconds_left ?? 0;
    const urgent = secsLeft < 30;
    const isLive = m.is_live !== false;

    return `
      <div class="market-card">
        <div class="market-card-top">
          <div class="market-name">
            ${asset}
            <span class="market-duration">${dur}</span>
            <span class="live-badge ${isLive ? 'on' : 'off'}">${isLive ? 'LIVE' : 'OBS'}</span>
          </div>
          <div class="market-countdown ${urgent ? 'urgent' : ''}">${secsLeft}s</div>
        </div>
        <div class="gauge">
          <div class="gauge-yes" style="width:${yesPct}%">YES ${yesAsk.toFixed(2)}</div>
          <div class="gauge-no" style="width:${noPct}%">NO ${noAsk.toFixed(2)}</div>
          <div class="gauge-threshold" style="left:${Math.min(100, combined*100)}%"></div>
        </div>
        <div class="market-edge-row">
          <span class="${edge > 0.03 ? 'edge-positive' : 'edge-neutral'}">
            edge ${(edge*100).toFixed(1)}¢
          </span>
          <span style="color:var(--text-faint)">combined $${combined.toFixed(3)}</span>
        </div>
      </div>
    `;
  }).join('');
}

async function loadMarkets(){
  try{
    const r = await fetch('/api/markets');
    const data = await r.json();
    renderMarkets(data);
  }catch(e){ console.error('markets load failed', e); }
}

// ── Trades ───────────────────────────────────────────────────
async function loadTrades(){
  try{
    const r = await fetch('/api/trades');
    const trades = await r.json();
    const list = document.getElementById('trades-list');

    document.getElementById('trades-count').textContent = `${trades.length} logged`;

    if (!trades.length){
      list.innerHTML = `<div class="empty-state">No trades yet —<br>the bot is watching for edge</div>`;
      return;
    }

    list.innerHTML = trades.map(t => {
      const profit = t.profit || 0;
      const isProfit = profit >= 0;
      const time = t.time ? t.time.substring(11,19) : '--:--:--';
      return `
        <div class="trade-card ${isProfit ? 'profit' : 'loss'}">
          <div class="trade-left">
            <span class="trade-pair">${t.pair}</span>
            <span class="trade-meta">${time} UTC · $${fmt(t.size,2)} size</span>
          </div>
          <div class="trade-right">
            <div class="trade-profit ${isProfit ? 'pos' : 'neg'}">
              ${isProfit ? '+' : ''}${fmtMoney(profit)}
            </div>
            <div class="trade-edge">edge ${((t.edge||0)*100).toFixed(1)}¢</div>
          </div>
        </div>
      `;
    }).join('');
  }catch(e){ console.error('trades load failed', e); }
}

// ── Duration toggle (live, no restart needed) ──────────────────
const DURATION_HINTS = {
  BOTH:  'Trading both durations. Discovery always tracks both regardless of this toggle, so switching never loses comparison data.',
  '5MIN':  'Only 5-minute markets are live-trading. 15-minute edges are still being logged as "observed" below for comparison.',
  '15MIN': 'Only 15-minute markets are live-trading. 5-minute edges are still being logged as "observed" below for comparison.',
};

function syncDurationButtons(mode){
  document.querySelectorAll('.dur-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
    btn.classList.remove('pending');
  });
  const hintEl = document.getElementById('duration-hint');
  if (hintEl) hintEl.textContent = DURATION_HINTS[mode] || '';
}

document.querySelectorAll('.dur-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const mode = btn.dataset.mode;
    if (mode === window.__currentDurationMode) return;

    window.__durationPending = true;
    document.querySelectorAll('.dur-btn').forEach(b => b.classList.add('pending'));

    try{
      const r = await fetch('/api/settings/duration-mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({duration_mode: mode})
      });
      const d = await r.json();
      if (r.ok && d.duration_mode){
        window.__currentDurationMode = d.duration_mode;
        syncDurationButtons(d.duration_mode);
      } else {
        console.error('duration toggle rejected', d);
        syncDurationButtons(window.__currentDurationMode || 'BOTH');
      }
    }catch(e){
      console.error('duration toggle failed', e);
      syncDurationButtons(window.__currentDurationMode || 'BOTH');
    }finally{
      window.__durationPending = false;
    }
  });
});

// ── Duration comparison (5min vs 15min performance) ────────────
function fillCompareCol(prefix, stats){
  const el = id => document.getElementById(id);
  el(`cmp-${prefix}-real`).textContent = stats.real_trades ?? 0;

  const profitEl = el(`cmp-${prefix}-profit`);
  const p = stats.real_profit || 0;
  profitEl.textContent = (p >= 0 ? '+' : '') + fmtMoney(p);
  profitEl.style.color = p >= 0 ? 'var(--yes)' : 'var(--no)';

  el(`cmp-${prefix}-winrate`).textContent = fmt(stats.real_win_rate || 0, 1) + '%';
  el(`cmp-${prefix}-observed`).textContent =
    `${stats.observed_only ?? 0} (${fmtMoney(stats.observed_profit || 0)})`;

  const potEl = el(`cmp-${prefix}-potential`);
  const pot = stats.combined_potential_profit || 0;
  potEl.textContent = (pot >= 0 ? '+' : '') + fmtMoney(pot);
  potEl.style.color = pot >= 0 ? 'var(--yes)' : 'var(--no)';
}

async function loadDurationComparison(){
  try{
    const r = await fetch('/api/duration-comparison');
    const d = await r.json();
    if (d['5MIN'])  fillCompareCol('5', d['5MIN']);
    if (d['15MIN']) fillCompareCol('15', d['15MIN']);
    document.getElementById('comparison-updated').textContent =
      new Date().toLocaleTimeString();
  }catch(e){ console.error('duration comparison load failed', e); }
}

// ── Circuit breaker manual resume ──────────────────────────────
const resumeBtn = document.getElementById('resume-btn');
if (resumeBtn){
  resumeBtn.addEventListener('click', async () => {
    resumeBtn.disabled = true;
    resumeBtn.querySelector('.dur-btn-label').textContent = 'RESUMING...';
    try{
      const r = await fetch('/api/circuit-breaker/resume', { method: 'POST' });
      const d = await r.json();
      if (r.ok){
        document.getElementById('halt-panel').style.display = 'none';
        loadSummary(); // Refresh immediately to reflect ARMED state
      } else {
        console.error('resume failed', d);
        resumeBtn.querySelector('.dur-btn-label').textContent = 'RESUME TRADING';
      }
    }catch(e){
      console.error('resume request failed', e);
      resumeBtn.querySelector('.dur-btn-label').textContent = 'RESUME TRADING';
    }finally{
      resumeBtn.disabled = false;
    }
  });
}

// ── Refresh loop ─────────────────────────────────────────────
function refreshAll(){
  loadSummary();
  loadMarkets();
  loadTrades();
  loadDurationComparison();
}

refreshAll();
setInterval(refreshAll, 5000);
