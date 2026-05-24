/**
 * Limitless Oracle — Node.js Backend
 * =====================================
 * Signal engine runs SERVER-SIDE on a 15-minute candle cycle.
 * The browser receives live updates via Server-Sent Events (SSE).
 * Orders are placed automatically from the server — no browser needed.
 *
 * Render start command: node server/index.js
 */

import express from "express";
import path from "path";
import { fileURLToPath } from "url";
import crypto from "crypto";
import { ethers } from "ethers";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json());

const PORT     = process.env.PORT || 5000;
const API_BASE = "https://api.limitless.exchange";
const CHAIN_ID = 8453n;
const ZERO_ADDR = "0x0000000000000000000000000000000000000000";
const CANDLE_MS = 15 * 60 * 1000;

// ── Pair config ────────────────────────────────────────────────────
const PAIRS = {
  "BTC-USDT":  { family: 0, label: "BTC" },
  "ETH-USDT":  { family: 0, label: "ETH" },
  "SOL-USDT":  { family: 1, label: "SOL" },
  "DOGE-USDT": { family: 1, label: "DOGE" },
  "XRP-USDT":  { family: 2, label: "XRP" },
  "BNB-USDT":  { family: 2, label: "BNB" },
};

const KNOWN_SLUGS = {
  BTC: "btc-up-or-down-15-min", ETH: "eth-up-or-down-15-min",
  SOL: "sol-up-or-down-15-min", XRP: "xrp-up-or-down-15-min",
  BNB: "bnb-up-or-down-15-min", DOGE: "doge-up-or-down-15-min",
};

// ── In-memory state ────────────────────────────────────────────────
const state = {
  prices:      {},   // { "BTC-USDT": { last, open24h, ... } }
  candles:     {},   // { "BTC-USDT": [...] }
  signals:     [],   // settled signal history (newest first)
  activeSignal: null,
  stats:        { wins: 0, losses: 0, total: 0 },
  pendingSignal: null,
  lastFamily:   null,
  consecutiveLosses: 0,
  cooldownCandles:   0,
  lastCandleStart:   getCurrentCandleStart(),
  settings: {
    mode:             process.env.TRADE_MODE || "shadow",
    positionSize:     Number(process.env.POSITION_SIZE || 10),
    maxContractPrice: Number(process.env.MAX_CONTRACT_PRICE || 0.50),
    privateKey:   process.env.LIMITLESS_PRIVATE_KEY   || "",
    tokenId:      process.env.LIMITLESS_TOKEN_ID      || "",
    tokenSecret:  process.env.LIMITLESS_TOKEN_SECRET  || "",
  },
};

// SSE client registry
const sseClients = new Set();

function broadcast(event, data) {
  const msg = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of sseClients) {
    try { res.write(msg); } catch {}
  }
}

// ══════════════════════════════════════════════════════════════════
// TIME HELPERS
// ══════════════════════════════════════════════════════════════════

function getCurrentCandleStart(now = Date.now()) {
  return Math.floor(now / CANDLE_MS) * CANDLE_MS;
}
function msUntilNextCandle(now = Date.now()) {
  return (getCurrentCandleStart(now) + CANDLE_MS) - now;
}

// ══════════════════════════════════════════════════════════════════
// OKX MARKET DATA
// ══════════════════════════════════════════════════════════════════

async function fetchOKXCandles(instId, limit = 50) {
  try {
    const url = `https://www.okx.com/api/v5/market/candles?instId=${instId}&bar=15m&limit=${limit}`;
    const r = await fetch(url, { signal: AbortSignal.timeout(10000) });
    const j = await r.json();
    if (j.code !== "0") return null;
    return j.data.map(c => ({
      ts: Number(c[0]), open: +c[1], high: +c[2], low: +c[3],
      close: +c[4], vol: +c[5], confirm: c[8] === "1",
    })).reverse();
  } catch { return null; }
}

async function fetchOKXTicker(instId) {
  try {
    const r = await fetch(`https://www.okx.com/api/v5/market/ticker?instId=${instId}`,
      { signal: AbortSignal.timeout(10000) });
    const j = await r.json();
    if (j.code !== "0" || !j.data?.[0]) return null;
    const d = j.data[0];
    return { last: +d.last, open24h: +d.open24h, high24h: +d.high24h, low24h: +d.low24h, vol24h: +d.vol24h };
  } catch { return null; }
}

async function refreshMarketData() {
  const pairs = Object.keys(PAIRS);
  const results = await Promise.allSettled(pairs.map(async p => {
    const [ticker, candles] = await Promise.all([fetchOKXTicker(p), fetchOKXCandles(p, 50)]);
    return { pair: p, ticker, candles };
  }));
  for (const r of results) {
    if (r.status === "fulfilled" && r.value) {
      const { pair, ticker, candles } = r.value;
      if (ticker)  state.prices[pair]  = ticker;
      if (candles) state.candles[pair] = candles;
    }
  }
  broadcast("prices", state.prices);
}

// ══════════════════════════════════════════════════════════════════
// SIGNAL ENGINE  (12 confluence models)
// ══════════════════════════════════════════════════════════════════

function computeSignal(candles) {
  if (!candles || candles.length < 20) return null;
  const closed = candles.filter(c => c.confirm);
  if (closed.length < 15) return null;
  const c = closed, n = c.length;
  const closes = c.map(x => x.close), highs = c.map(x => x.high);
  const lows = c.map(x => x.low), vols = c.map(x => x.vol);
  const last = closes[n - 1];
  const avg = a => a.reduce((s, v) => s + v, 0) / a.length;
  const ema = (a, p) => { const k=2/(p+1); let e=a[0]; for(let i=1;i<a.length;i++) e=a[i]*k+e*(1-k); return e; };
  const sma = (a, p) => avg(a.slice(-p));
  const std = a => { const m=avg(a); return Math.sqrt(avg(a.map(x=>(x-m)**2))); };
  const scores = [];

  // 1. EMA stack
  const ema5=ema(closes.slice(-10),5), ema10=ema(closes.slice(-14),10), ema20=ema(closes.slice(-24),20);
  if(ema5>ema10&&ema10>ema20) scores.push(1);
  else if(ema5<ema10&&ema10<ema20) scores.push(-1); else scores.push(0);

  // 2. RSI(14)
  let g=0,l=0;
  for(let i=n-14;i<n;i++){const d=closes[i]-closes[i-1];if(d>0)g+=d;else l-=d;}
  const rsi=100-100/(1+(g/14)/((l/14)||0.001));
  if(rsi>55&&rsi<75) scores.push(1); else if(rsi<45&&rsi>25) scores.push(-1);
  else if(rsi>=75) scores.push(-0.5); else if(rsi<=25) scores.push(0.5); else scores.push(0);

  // 3. MACD
  const e12=ema(closes.slice(-16),12), e26=ema(closes.slice(-30),26);
  const macd=e12-e26, pe12=ema(closes.slice(-17,-1),12), pe26=ema(closes.slice(-31,-1),26);
  if(macd>0&&macd>(pe12-pe26)) scores.push(1);
  else if(macd<0&&macd<(pe12-pe26)) scores.push(-1); else scores.push(0);

  // 4. Bollinger Bands
  const bbM=sma(closes,20), bbS=std(closes.slice(-20)), bbU=bbM+2*bbS, bbL=bbM-2*bbS;
  if(last>bbM&&last<bbU*.98) scores.push(0.7);
  else if(last<bbM&&last>bbL*1.02) scores.push(-0.7);
  else if(last<=bbL) scores.push(1); else if(last>=bbU) scores.push(-1); else scores.push(0);

  // 5. Volume
  const avgV=avg(vols.slice(-10)), vr=vols[n-1]/avgV, pd=closes[n-1]>closes[n-2]?1:-1;
  scores.push(vr>1.5?pd*1:pd*0.3);

  // 6. Candlestick pattern
  const lc=c[n-1], pc=c[n-2];
  const body=Math.abs(lc.close-lc.open), range=(lc.high-lc.low)||0.0001;
  const uw=lc.high-Math.max(lc.open,lc.close), lw=Math.min(lc.open,lc.close)-lc.low;
  if(body/range<0.1) scores.push(0);
  else if(lw>body*2&&uw<body*.5&&lc.close>lc.open) scores.push(1);
  else if(uw>body*2&&lw<body*.5&&lc.close<lc.open) scores.push(-1);
  else if(lc.close>lc.open&&pc.close<pc.open&&lc.close>pc.open&&lc.open<pc.close) scores.push(1);
  else if(lc.close<lc.open&&pc.close>pc.open&&lc.close<pc.open&&lc.open>pc.close) scores.push(-1);
  else scores.push(0);

  // 7. ROC
  const roc=(closes[n-1]-closes[n-6])/closes[n-6]*100;
  if(roc>0.3) scores.push(1); else if(roc<-0.3) scores.push(-1); else scores.push(0);

  // 8. Stochastic
  const kH=Math.max(...highs.slice(-14)), kL=Math.min(...lows.slice(-14));
  const stoch=(last-kL)/(kH-kL)*100;
  if(stoch>80) scores.push(-0.8); else if(stoch<20) scores.push(0.8);
  else if(stoch>50) scores.push(0.4); else scores.push(-0.4);

  // 9. Price structure HH/HL
  const isHH=highs[n-1]>highs[n-2]&&highs[n-2]>highs[n-3];
  const isHL=lows[n-1]>lows[n-2]&&lows[n-2]>lows[n-3];
  const isLH=highs[n-1]<highs[n-2]&&highs[n-2]<highs[n-3];
  const isLL=lows[n-1]<lows[n-2]&&lows[n-2]<lows[n-3];
  if(isHH&&isHL) scores.push(1); else if(isLH&&isLL) scores.push(-1); else scores.push(0);

  // 10. Close position
  const cp=(lc.close-lc.low)/((lc.high-lc.low)||0.0001);
  if(cp>0.7) scores.push(0.6); else if(cp<0.3) scores.push(-0.6); else scores.push(0);

  // 11. SMA cross
  const s5=sma(closes,5), s20=sma(closes,20);
  const ps5=avg(closes.slice(-6,-1)), ps20=avg(closes.slice(-21,-1));
  if(s5>s20&&ps5<=ps20) scores.push(1.5); else if(s5<s20&&ps5>=ps20) scores.push(-1.5);
  else if(s5>s20) scores.push(0.5); else scores.push(-0.5);

  // 12. VWAP
  let vwN=0,vwD=0;
  for(let i=Math.max(0,n-10);i<n;i++){const t=(highs[i]+lows[i]+closes[i])/3;vwN+=t*vols[i];vwD+=vols[i];}
  const vwap=vwN/vwD;
  if(last>vwap*1.001) scores.push(0.6); else if(last<vwap*.999) scores.push(-0.6); else scores.push(0);

  const total=scores.reduce((a,b)=>a+b,0)/scores.length;
  if(Math.abs(total)<0.12) return null;

  const bbW=(bbU-bbL)/bbM, tStr=Math.abs(ema5-ema20)/ema20*100;
  let mc="Ranging";
  if(bbW>0.04) mc="High Volatility"; else if(bbW<0.01) mc="Compression";
  else if(tStr>1) mc=ema5>ema20?"Uptrend":"Downtrend";
  else if(rsi>70) mc="Euphoria"; else if(rsi<30) mc="Capitulation";

  return {
    direction: total>0?"UP":"DOWN",
    confidence: Math.min(95, Math.round(Math.abs(total)*100+45)),
    score: total, marketCondition: mc,
    rsi: Math.round(rsi), stoch: Math.round(stoch),
  };
}

// ══════════════════════════════════════════════════════════════════
// SETTLE PENDING SIGNAL (called at candle close)
// ══════════════════════════════════════════════════════════════════

function settlePending() {
  const p = state.pendingSignal;
  if (!p) return;
  const currentPrice = state.prices[p.pair]?.last;
  if (!currentPrice) {
    console.log(`[settle] No price for ${p.pair}, skipping`);
    return;
  }
  const won = (p.direction === "UP" && currentPrice > p.openPrice) ||
              (p.direction === "DOWN" && currentPrice < p.openPrice);
  const settled = { ...p, closePrice: currentPrice, result: won ? "WIN" : "LOSS", settledAt: Date.now() };

  // Streak / cooldown tracking
  if (!won) {
    state.consecutiveLosses++;
    if (state.consecutiveLosses >= 2) {
      state.cooldownCandles = 2;
      state.consecutiveLosses = 0;
      console.log("[engine] 2 consecutive losses — cooldown 2 candles");
    }
  } else {
    state.consecutiveLosses = 0;
  }

  // Update stats (daily — reset handled separately)
  state.stats.total++;
  if (won) state.stats.wins++; else state.stats.losses++;

  state.signals.unshift(settled);
  if (state.signals.length > 500) state.signals.length = 500;
  state.pendingSignal = null;
  state.activeSignal  = null;

  console.log(`[settle] ${settled.pair} ${settled.direction} → ${settled.result} | open=${settled.openPrice} close=${currentPrice}`);
  broadcast("signal_settled", settled);
  broadcast("stats", state.stats);
}

// ══════════════════════════════════════════════════════════════════
// EVALUATE & FIRE SIGNAL (called 3s after each new candle opens)
// ══════════════════════════════════════════════════════════════════

async function evaluateAndFire() {
  if (state.cooldownCandles > 0) {
    state.cooldownCandles--;
    console.log(`[engine] Cooldown — ${state.cooldownCandles} candle(s) remaining`);
    broadcast("cooldown", { remaining: state.cooldownCandles });
    return;
  }

  const candidates = [];
  for (const [pair, candles] of Object.entries(state.candles)) {
    const sig = computeSignal(candles);
    if (sig) candidates.push({ pair, ...sig });
  }

  console.log(`[engine] Evaluated ${Object.keys(state.candles).length} pairs, ${candidates.length} candidates`);

  if (candidates.length === 0) {
    broadcast("scan", { result: "no_signal", ts: Date.now() });
    return;
  }

  // Family rotation filter
  const validFamilies = state.lastFamily !== null
    ? [0, 1, 2].filter(f => f !== state.lastFamily)
    : [0, 1, 2];
  const eligible = candidates.filter(c => validFamilies.includes(PAIRS[c.pair].family));

  if (eligible.length === 0) {
    console.log("[engine] All candidates blocked by family rotation");
    broadcast("scan", { result: "family_blocked", ts: Date.now() });
    return;
  }

  eligible.sort((a, b) => b.confidence - a.confidence);
  const best = eligible[0];
  const openPrice = state.prices[best.pair]?.last;
  if (!openPrice) {
    console.log(`[engine] No open price for ${best.pair}`);
    return;
  }

  state.lastFamily = PAIRS[best.pair].family;

  const signal = {
    id:              `sig_${Date.now()}`,
    pair:            best.pair,
    direction:       best.direction,
    confidence:      best.confidence,
    marketCondition: best.marketCondition,
    rsi:             best.rsi,
    candleStart:     getCurrentCandleStart(),
    openPrice,
    closePrice:      null,
    result:          "PENDING",
    family:          PAIRS[best.pair].family,
    orderResult:     null,
    timestamp:       Date.now(),
  };

  state.pendingSignal = signal;
  state.activeSignal  = signal;

  console.log(`[engine] Signal: ${signal.pair} ${signal.direction} @ ${openPrice} | conf=${signal.confidence}% | ${signal.marketCondition}`);
  broadcast("signal_new", signal);

  // Execute order
  const s = state.settings;
  try {
    let orderResult;
    if (s.mode === "live" && s.privateKey && s.tokenId && s.tokenSecret) {
      orderResult = await placeLiveOrder(signal.pair, signal.direction, s.positionSize, s.maxContractPrice, {
        privateKey: s.privateKey, tokenId: s.tokenId, tokenSecret: s.tokenSecret,
      });
    } else {
      orderResult = placeShadowOrder(signal.pair, signal.direction, s.positionSize, s.maxContractPrice);
    }
    state.pendingSignal  = { ...state.pendingSignal, orderResult };
    state.activeSignal   = state.pendingSignal;
    broadcast("signal_order", { id: signal.id, orderResult });
    console.log(`[order] ${s.mode} → success=${orderResult.success}`);
  } catch (e) {
    console.error("[order] Error:", e.message);
  }
}

// ══════════════════════════════════════════════════════════════════
// CANDLE BOUNDARY WATCHER  (server-side, runs forever)
// ══════════════════════════════════════════════════════════════════

async function candleTick() {
  const currentStart = getCurrentCandleStart();
  if (currentStart !== state.lastCandleStart) {
    console.log(`[candle] New candle opened at ${new Date(currentStart).toISOString()}`);
    state.lastCandleStart = currentStart;

    // 1. Settle the previous signal with old data
    settlePending();

    // 2. Refresh market data (fresh candles)
    await refreshMarketData();

    // 3. Wait 3s for the new candle to stabilise then fire
    setTimeout(evaluateAndFire, 3000);

    broadcast("candle_open", { ts: currentStart });
  }
}

// ══════════════════════════════════════════════════════════════════
// DAILY STATS RESET AT 00:00 UTC
// ══════════════════════════════════════════════════════════════════

function scheduleDailyReset() {
  const now  = new Date();
  const next = new Date(now);
  next.setUTCHours(24, 0, 0, 0);
  const ms = next - now;
  setTimeout(() => {
    state.stats = { wins: 0, losses: 0, total: 0 };
    console.log("[engine] Daily stats reset at 00:00 UTC");
    broadcast("stats_reset", state.stats);
    scheduleDailyReset();
  }, ms);
}

// ══════════════════════════════════════════════════════════════════
// CREDENTIAL HELPERS
// ══════════════════════════════════════════════════════════════════

function resolveCreds(body = {}) {
  const c = body.credentials || {};
  return {
    privateKey:  (c.privateKey  || state.settings.privateKey  || "").trim(),
    tokenId:     (c.tokenId     || state.settings.tokenId     || "").trim(),
    tokenSecret: (c.tokenSecret || state.settings.tokenSecret || "").trim(),
  };
}

function getSignerAddress(pk) {
  try { return new ethers.Wallet(pk).address; } catch { return null; }
}

// ══════════════════════════════════════════════════════════════════
// HMAC AUTH
// ══════════════════════════════════════════════════════════════════

function isoTimestamp() { return new Date().toISOString().replace(/(\.\d{3})\d*Z/, "$1Z"); }

function buildHmacHeaders(method, apiPath, body = "", creds) {
  const { tokenId, tokenSecret } = creds;
  if (!tokenId || !tokenSecret) throw new Error("LIMITLESS_TOKEN_ID and LIMITLESS_TOKEN_SECRET required");
  const ts  = isoTimestamp();
  const msg = `${ts}\n${method}\n${apiPath}\n${body}`;
  const key = Buffer.from(tokenSecret, "base64");
  const sig = crypto.createHmac("sha256", key).update(msg, "utf8").digest("base64");
  return { "lmts-api-key": tokenId, "lmts-timestamp": ts, "lmts-signature": sig, "Content-Type": "application/json" };
}

// ══════════════════════════════════════════════════════════════════
// MARKET DISCOVERY
// ══════════════════════════════════════════════════════════════════

const slugCache = {}, marketCache = {};
let ownerIdCache = null, feeRateCache = null;

async function discoverSlug(symbol) {
  if (slugCache[symbol]) return slugCache[symbol];
  const ticker = symbol.replace("-USDT", "").toUpperCase();
  try {
    const r = await fetch(`${API_BASE}/markets/active/slugs`, { signal: AbortSignal.timeout(10000) });
    if (!r.ok) return null;
    const entries = await r.json();
    const matches = [];
    for (const entry of entries) {
      const et = (entry.ticker || "").toUpperCase().replace("-USDT","");
      const es = (entry.slug || "").toLowerCase();
      const children = entry.markets || [];
      const checkSlug = (slug, deadline) => {
        const combined = slug + " " + (entry.title||"") + " " + (entry.frequency||"");
        const is15 = combined.includes("15-min")||combined.includes("15min")||combined.includes("-15-");
        if (is15) matches.push([0, deadline||"", slug]);
      };
      if (et === ticker) {
        if (children.length) children.forEach(ch => checkSlug((ch.slug||"").toLowerCase(), ch.deadline||entry.deadline));
        else checkSlug(es, entry.deadline);
      } else {
        for (const ch of children) {
          const ct = (ch.ticker||"").toUpperCase().replace("-USDT","");
          if (ct === ticker) checkSlug((ch.slug||"").toLowerCase(), ch.deadline||entry.deadline);
        }
      }
    }
    if (!matches.length) return null;
    matches.sort((a,b)=>b[1]>a[1]?1:-1);
    slugCache[symbol] = matches[0][2];
    return slugCache[symbol];
  } catch (e) { console.error("discoverSlug error:", e.message); return null; }
}

async function fetchMarket(slug) {
  if (marketCache[slug]?._merged) return marketCache[slug];
  let market = {};
  try {
    const r = await fetch(`${API_BASE}/markets/${slug}`, { signal: AbortSignal.timeout(10000) });
    if (r.ok) market = await r.json() || {};
  } catch {}
  try {
    const r2 = await fetch(`${API_BASE}/markets/${slug}/orderbook`, { signal: AbortSignal.timeout(10000) });
    if (r2.ok) {
      const ob = await r2.json() || {};
      const venue = typeof market.venue === "object" ? market.venue : {};
      const exchangeAddr = venue?.exchange || market.exchange || null;
      let yesToken = null, noToken = null;
      const bt = market.tokens || {};
      if (Array.isArray(bt)) {
        yesToken = bt.find(t=>["yes","up","1"].includes(String(t?.outcome||"").toLowerCase()))?.tokenId || null;
        noToken  = bt.find(t=>["no","down","0"].includes(String(t?.outcome||"").toLowerCase()))?.tokenId || null;
      } else { yesToken = bt.yes||bt.Yes||null; noToken = bt.no||bt.No||null; }
      yesToken = yesToken || ob.tokenId || ob.yesTokenId || null;
      noToken  = noToken  || ob.noTokenId || null;
      let posIds = market.positionIds || [];
      if (!posIds.length && yesToken) posIds = [yesToken, ...(noToken?[noToken]:[])];
      const conditionId = market.conditionId || market.condition_id || market.ctfConditionId || null;
      Object.assign(market, { slug, exchange: exchangeAddr, venue: { exchange: exchangeAddr },
        tokens: { yes: yesToken, no: noToken }, positionIds: posIds, conditionId, _merged: true });
    }
  } catch {}
  if (!Object.keys(market).length) return null;
  market.slug = market.slug || slug;
  marketCache[slug] = market;
  return market;
}

// ══════════════════════════════════════════════════════════════════
// ORDER PLACEMENT
// ══════════════════════════════════════════════════════════════════

const ORDER_TYPES = { Order: [
  { name:"salt",type:"uint256"},{name:"maker",type:"address"},{name:"signer",type:"address"},
  { name:"taker",type:"address"},{name:"tokenId",type:"uint256"},{name:"makerAmount",type:"uint256"},
  { name:"takerAmount",type:"uint256"},{name:"expiration",type:"uint256"},{name:"nonce",type:"uint256"},
  { name:"feeRateBps",type:"uint256"},{name:"side",type:"uint8"},{name:"signatureType",type:"uint8"},
]};

async function signOrder(order, exchangeAddr, privateKey) {
  const wallet = new ethers.Wallet(privateKey);
  const domain = { name:"Limitless CTF Exchange", version:"1", chainId: CHAIN_ID, verifyingContract: ethers.getAddress(exchangeAddr) };
  const values = {
    salt: BigInt(order.salt), maker: ethers.getAddress(order.maker), signer: ethers.getAddress(order.signer),
    taker: ethers.getAddress(order.taker), tokenId: BigInt(order.tokenId),
    makerAmount: BigInt(order.makerAmount), takerAmount: BigInt(order.takerAmount),
    expiration: BigInt(order.expiration), nonce: BigInt(order.nonce),
    feeRateBps: BigInt(order.feeRateBps), side: Number(order.side), signatureType: Number(order.signatureType),
  };
  return wallet.signTypedData(domain, ORDER_TYPES, values);
}

async function getOwnerIdAndFee(makerAddr, creds) {
  if (ownerIdCache !== null) return { ownerId: ownerIdCache, feeRateBps: feeRateCache };
  try {
    const apiPath = `/profiles/${makerAddr}`;
    const headers = buildHmacHeaders("GET", apiPath, "", creds);
    const res = await fetch(`${API_BASE}${apiPath}`, { headers, signal: AbortSignal.timeout(10000) });
    if (res.ok) {
      const data = await res.json();
      ownerIdCache = data.id != null ? Number(data.id) : null;
      feeRateCache = data.rank?.feeRateBps != null ? Number(data.rank.feeRateBps) : 0;
      return { ownerId: ownerIdCache, feeRateBps: feeRateCache };
    }
  } catch (e) { console.error("getOwnerIdAndFee error:", e.message); }
  return { ownerId: null, feeRateBps: 0 };
}

async function placeLiveOrder(symbol, direction, positionSizeUsd, maxContractPrice, creds) {
  const { privateKey } = creds;
  if (!privateKey) return { success: false, error: "LIMITLESS_PRIVATE_KEY not set" };
  let makerAddr;
  try { makerAddr = new ethers.Wallet(privateKey).address; }
  catch (e) { return { success: false, error: `Invalid private key: ${e.message}` }; }

  delete slugCache[symbol];
  const slug = await discoverSlug(symbol);
  if (!slug) return { success: false, error: `No active 15-min market for ${symbol}` };

  delete marketCache[slug];
  const market = await fetchMarket(slug);
  if (!market) return { success: false, error: `Cannot fetch market slug=${slug}` };

  const exchangeAddr = market.venue?.exchange || market.exchange;
  if (!exchangeAddr) return { success: false, error: "venue.exchange missing" };

  const tokens = market.tokens || {};
  const tokenId = direction === "UP" ? (tokens.yes || market.positionIds?.[0]) : (tokens.no || market.positionIds?.[1]);
  if (!tokenId) return { success: false, error: `Token ID missing for direction=${direction}` };

  const { ownerId, feeRateBps } = await getOwnerIdAndFee(makerAddr, creds);
  if (ownerId === null) return { success: false, error: `Could not resolve ownerId for ${makerAddr}` };

  const price       = Math.round(Math.min(maxContractPrice, 0.99) * 100) / 100;
  const size        = Math.round((positionSizeUsd / price) * 10000) / 10000;
  const makerAmount = Math.round(price * size * 1_000_000);
  const takerAmount = Math.round(size * 1_000_000);
  const salt        = BigInt(Date.now());

  const order = {
    salt: salt.toString(), maker: makerAddr, signer: makerAddr, taker: ZERO_ADDR,
    tokenId: String(tokenId), makerAmount, takerAmount,
    expiration: "0", nonce: 0, feeRateBps, side: 0, signatureType: 0,
  };

  let signature;
  try { signature = await signOrder(order, exchangeAddr, privateKey); }
  catch (e) { return { success: false, error: `EIP-712 signing failed: ${e.message}` }; }

  const payload = { order: { ...order, signature, signatureType: 0, price }, orderType: "GTC", marketSlug: slug, ownerId };
  const bodyStr = JSON.stringify(payload);
  let headers;
  try { headers = buildHmacHeaders("POST", "/orders", bodyStr, creds); }
  catch (e) { return { success: false, error: e.message }; }

  try {
    const res = await fetch(`${API_BASE}/orders`, { method:"POST", headers, body: bodyStr, signal: AbortSignal.timeout(15000) });
    const text = await res.text();
    if (!res.ok) {
      let detail; try { detail = JSON.parse(text); } catch { detail = text; }
      return { success: false, http_status: res.status, error: `HTTP ${res.status}`, api_response: detail };
    }
    const result  = JSON.parse(text);
    const orderId = result?.order?.id || result?.id || salt.toString();
    return {
      success: true, order_id: String(orderId), contracts: size,
      price_per_contract: price, total_spent: positionSizeUsd,
      slug, condition_id: market.conditionId || null,
      signal_direction: direction, maker: makerAddr,
    };
  } catch (e) { return { success: false, error: e.message }; }
}

function placeShadowOrder(symbol, direction, positionSizeUsd, maxContractPrice) {
  const price = Math.min(maxContractPrice, 0.99);
  return {
    success: true, order_id: `shadow_${Date.now()}`,
    contracts: Math.round((positionSizeUsd / price) * 10000) / 10000,
    price_per_contract: price, total_spent: positionSizeUsd,
    signal_direction: direction, shadow: true,
  };
}

// ══════════════════════════════════════════════════════════════════
// EXPRESS API ROUTES
// ══════════════════════════════════════════════════════════════════

// Keep-alive ping
app.get("/api/ping", (_req, res) => res.json({ ok: true, ts: Date.now() }));

// SSE — browser subscribes here to get live state pushes
app.get("/api/stream", (req, res) => {
  res.setHeader("Content-Type",  "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection",    "keep-alive");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.flushHeaders();

  // Send full current state on connect
  const snapshot = {
    prices:       state.prices,
    signals:      state.signals.slice(0, 100),
    activeSignal: state.activeSignal,
    stats:        state.stats,
    settings: {
      mode:             state.settings.mode,
      positionSize:     state.settings.positionSize,
      maxContractPrice: state.settings.maxContractPrice,
    },
    cooldownCandles:   state.cooldownCandles,
    lastFamily:        state.lastFamily,
  };
  res.write(`event: snapshot\ndata: ${JSON.stringify(snapshot)}\n\n`);

  // Heartbeat
  const heartbeat = setInterval(() => {
    try { res.write(": heartbeat\n\n"); } catch { clearInterval(heartbeat); }
  }, 15000);

  sseClients.add(res);
  req.on("close", () => { sseClients.delete(res); clearInterval(heartbeat); });
});

// Update settings from dashboard
app.post("/api/settings", (req, res) => {
  const { mode, positionSize, maxContractPrice, privateKey, tokenId, tokenSecret } = req.body || {};
  if (mode)             state.settings.mode             = mode;
  if (positionSize)     state.settings.positionSize     = Number(positionSize);
  if (maxContractPrice) state.settings.maxContractPrice = Number(maxContractPrice);
  if (privateKey)       state.settings.privateKey       = privateKey;
  if (tokenId)          state.settings.tokenId          = tokenId;
  if (tokenSecret)      state.settings.tokenSecret      = tokenSecret;
  // Reset owner cache if creds changed
  ownerIdCache = null; feeRateCache = null;
  res.json({ ok: true, settings: { mode: state.settings.mode, positionSize: state.settings.positionSize, maxContractPrice: state.settings.maxContractPrice } });
});

// Manual order execution (from dashboard button if needed)
app.post("/api/limitless/execute", async (req, res) => {
  try {
    const { symbol="BTC-USDT", direction="UP", mode="shadow", positionSize=10, maxContractPrice=0.50 } = req.body;
    const creds = resolveCreds(req.body);
    const result = mode === "live"
      ? await placeLiveOrder(symbol, direction, Number(positionSize), Number(maxContractPrice), creds)
      : placeShadowOrder(symbol, direction, Number(positionSize), Number(maxContractPrice));
    res.json(result);
  } catch (e) { res.status(500).json({ success: false, error: e.message }); }
});

// Claim winnings
app.post("/api/limitless/claim", async (req, res) => {
  try {
    const { marketSlug, conditionId } = req.body;
    const creds = resolveCreds(req.body);
    const cid = conditionId || (marketSlug ? (await fetchMarket(marketSlug))?.conditionId : null);
    if (!cid) { res.json({ success: false, error: "conditionId not found" }); return; }
    const apiPath = "/portfolio/redeem";
    const bodyStr = JSON.stringify({ conditionId: String(cid) });
    const headers = buildHmacHeaders("POST", apiPath, bodyStr, creds);
    const r = await fetch(`${API_BASE}${apiPath}`, { method:"POST", headers, body: bodyStr, signal: AbortSignal.timeout(15000) });
    const text = await r.text();
    if (!r.ok) { res.json({ success: false, error: `HTTP ${r.status}` }); return; }
    res.json({ success: true, ...(JSON.parse(text) || {}) });
  } catch (e) { res.status(500).json({ success: false, error: e.message }); }
});

// Order status
app.post("/api/limitless/order-status", async (req, res) => {
  try {
    const { marketSlug, orderId } = req.body;
    if (String(orderId).startsWith("shadow_")) { res.json({ filled: false, status: "SHADOW" }); return; }
    const creds = resolveCreds(req.body);
    const apiPath = `/markets/${marketSlug}/user-orders`;
    const headers = buildHmacHeaders("GET", apiPath, "", creds);
    const r = await fetch(`${API_BASE}${apiPath}?statuses=MATCHED&statuses=LIVE&limit=100`, { headers, signal: AbortSignal.timeout(10000) });
    if (!r.ok) { res.json({ filled: false, status: "ERROR" }); return; }
    const data = await r.json() || {};
    const order = (data.orders||[]).find(o => String(o.id) === String(orderId));
    res.json(order ? { filled: order.status === "MATCHED", status: order.status } : { filled: false, status: "NOT_FOUND" });
  } catch (e) { res.status(500).json({ filled: false, error: e.message }); }
});

// Validate credentials
app.post("/api/limitless/validate", (req, res) => {
  try {
    const creds = resolveCreds(req.body);
    const signerAddr = getSignerAddress(creds.privateKey);
    res.json({
      hmac_auth_ready:    !!(creds.tokenId && creds.tokenSecret),
      signing_ready:      !!creds.privateKey,
      live_trading_ready: !!(creds.tokenId && creds.tokenSecret && creds.privateKey),
      signer_address:     signerAddr,
    });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// Get slug
app.get("/api/limitless/slug/:symbol", async (req, res) => {
  try { res.json({ symbol: req.params.symbol, slug: await discoverSlug(req.params.symbol) }); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

// Expose current engine state (for debugging / polling fallback)
app.get("/api/state", (_req, res) => {
  res.json({
    signals:      state.signals.slice(0, 100),
    activeSignal: state.activeSignal,
    stats:        state.stats,
    prices:       state.prices,
    lastFamily:   state.lastFamily,
    cooldown:     state.cooldownCandles,
    settings: { mode: state.settings.mode, positionSize: state.settings.positionSize, maxContractPrice: state.settings.maxContractPrice },
  });
});

// Serve React build
const CLIENT_DIST = path.join(__dirname, "..", "client", "dist");
app.use(express.static(CLIENT_DIST));
app.get("*", (_req, res) => res.sendFile(path.join(CLIENT_DIST, "index.html")));

// ══════════════════════════════════════════════════════════════════
// START ENGINE
// ══════════════════════════════════════════════════════════════════

app.listen(PORT, async () => {
  console.log(`✅ Limitless Oracle server running on port ${PORT}`);
  console.log(`   Mode: ${state.settings.mode} | Position: $${state.settings.positionSize}`);
  console.log(`   Live trading ready: ${!!(state.settings.tokenId && state.settings.privateKey)}`);

  // Initial data fetch
  await refreshMarketData();
  console.log(`[init] Market data loaded for ${Object.keys(state.prices).length} pairs`);

  // Run an initial evaluation
  setTimeout(evaluateAndFire, 5000);

  // Check candle boundary every second
  setInterval(candleTick, 1000);

  // Refresh market data every 15 seconds
  setInterval(refreshMarketData, 15000);

  // Daily stat reset
  scheduleDailyReset();
});
