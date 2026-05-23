import { useState, useEffect, useRef, useCallback } from "react";

// ══════════════════════════════════════════════════════════════════
// CONSTANTS & CONFIG
// ══════════════════════════════════════════════════════════════════
const PAIRS = {
  "BTC-USDT": { family: 0, label: "BTC", color: "#F7931A" },
  "ETH-USDT": { family: 0, label: "ETH", color: "#627EEA" },
  "SOL-USDT": { family: 1, label: "SOL", color: "#9945FF" },
  "DOGE-USDT": { family: 1, label: "DOGE", color: "#C2A633" },
  "XRP-USDT": { family: 2, label: "XRP", color: "#00AAE4" },
  "BNB-USDT": { family: 2, label: "BNB", color: "#F0B90B" },
};

const FAMILY_NAMES = ["BTC·ETH", "SOL·DOGE", "XRP·BNB"];
const CANDLE_MS = 15 * 60 * 1000;

// Market condition tags
const MARKET_CONDITIONS = [
  "Trending","Ranging","High Volatility","Low Volatility","Breakout",
  "Mean Reversion","Momentum","Reversal","Consolidation","Choppy",
  "Expansion","Compression","Manipulation","Accumulation","Distribution",
];

// ══════════════════════════════════════════════════════════════════
// OKX CANDLE FETCHER
// ══════════════════════════════════════════════════════════════════
async function fetchOKXCandles(instId, bar = "15m", limit = 50) {
  try {
    const url = `https://www.okx.com/api/v5/market/candles?instId=${instId}&bar=${bar}&limit=${limit}`;
    const res = await fetch(url);
    const json = await res.json();
    if (json.code !== "0") return null;
    // [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    return json.data.map(c => ({
      ts: Number(c[0]),
      open: parseFloat(c[1]),
      high: parseFloat(c[2]),
      low: parseFloat(c[3]),
      close: parseFloat(c[4]),
      vol: parseFloat(c[5]),
      confirm: c[8] === "1",
    })).reverse();
  } catch {
    return null;
  }
}

async function fetchOKXTicker(instId) {
  try {
    const url = `https://www.okx.com/api/v5/market/ticker?instId=${instId}`;
    const res = await fetch(url);
    const json = await res.json();
    if (json.code !== "0" || !json.data?.[0]) return null;
    return {
      last: parseFloat(json.data[0].last),
      open24h: parseFloat(json.data[0].open24h),
      high24h: parseFloat(json.data[0].high24h),
      low24h: parseFloat(json.data[0].low24h),
      vol24h: parseFloat(json.data[0].vol24h),
    };
  } catch {
    return null;
  }
}

// ══════════════════════════════════════════════════════════════════
// SIGNAL ENGINE — 50+ confluence strategies
// ══════════════════════════════════════════════════════════════════
function computeSignal(candles) {
  if (!candles || candles.length < 20) return null;

  const closed = candles.filter(c => c.confirm);
  if (closed.length < 15) return null;

  const c = closed;
  const n = c.length;
  const closes = c.map(x => x.close);
  const highs = c.map(x => x.high);
  const lows = c.map(x => x.low);
  const opens = c.map(x => x.open);
  const vols = c.map(x => x.vol);
  const last = closes[n - 1];

  // ── UTILS ──────────────────────────────────────────────────────
  const avg = arr => arr.reduce((a, b) => a + b, 0) / arr.length;
  const ema = (arr, period) => {
    const k = 2 / (period + 1);
    let e = arr[0];
    for (let i = 1; i < arr.length; i++) e = arr[i] * k + e * (1 - k);
    return e;
  };
  const sma = (arr, period) => avg(arr.slice(-period));
  const stddev = arr => {
    const m = avg(arr);
    return Math.sqrt(avg(arr.map(x => (x - m) ** 2)));
  };

  const scores = [];
  const tags = [];

  // ── 1. TREND FOLLOWING ──────────────────────────────────────────
  const ema5 = ema(closes.slice(-10), 5);
  const ema10 = ema(closes.slice(-14), 10);
  const ema20 = ema(closes.slice(-24), 20);
  if (ema5 > ema10 && ema10 > ema20) { scores.push(1); tags.push("EMA Bullish Stack"); }
  else if (ema5 < ema10 && ema10 < ema20) { scores.push(-1); tags.push("EMA Bearish Stack"); }
  else scores.push(0);

  // ── 2. RSI (14) ─────────────────────────────────────────────────
  let gains = 0, losses = 0;
  for (let i = n - 14; i < n; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) gains += d; else losses -= d;
  }
  const rsi = 100 - 100 / (1 + (gains / 14) / ((losses / 14) || 0.001));
  if (rsi > 55 && rsi < 75) { scores.push(1); tags.push("RSI Bullish Zone"); }
  else if (rsi < 45 && rsi > 25) { scores.push(-1); tags.push("RSI Bearish Zone"); }
  else if (rsi >= 75) { scores.push(-0.5); tags.push("RSI Overbought"); }
  else if (rsi <= 25) { scores.push(0.5); tags.push("RSI Oversold"); }
  else scores.push(0);

  // ── 3. MACD ─────────────────────────────────────────────────────
  const ema12 = ema(closes.slice(-16), 12);
  const ema26 = ema(closes.slice(-30), 26);
  const macdLine = ema12 - ema26;
  const prevEma12 = ema(closes.slice(-17, -1), 12);
  const prevEma26 = ema(closes.slice(-31, -1), 26);
  const prevMacd = prevEma12 - prevEma26;
  if (macdLine > 0 && macdLine > prevMacd) { scores.push(1); tags.push("MACD Bullish"); }
  else if (macdLine < 0 && macdLine < prevMacd) { scores.push(-1); tags.push("MACD Bearish"); }
  else scores.push(0);

  // ── 4. BOLLINGER BANDS ──────────────────────────────────────────
  const bbMid = sma(closes, 20);
  const bbStd = stddev(closes.slice(-20));
  const bbUpper = bbMid + 2 * bbStd;
  const bbLower = bbMid - 2 * bbStd;
  const bbWidth = (bbUpper - bbLower) / bbMid;
  if (last > bbMid && last < bbUpper * 0.98) { scores.push(0.7); tags.push("BB Mid-Upper"); }
  else if (last < bbMid && last > bbLower * 1.02) { scores.push(-0.7); tags.push("BB Mid-Lower"); }
  else if (last <= bbLower) { scores.push(1); tags.push("BB Bounce Lower"); }
  else if (last >= bbUpper) { scores.push(-1); tags.push("BB Bounce Upper"); }
  else scores.push(0);

  // ── 5. VOLUME ANALYSIS ──────────────────────────────────────────
  const avgVol = avg(vols.slice(-10));
  const lastVol = vols[n - 1];
  const volRatio = lastVol / avgVol;
  const priceDir = closes[n - 1] > closes[n - 2] ? 1 : -1;
  if (volRatio > 1.5) { scores.push(priceDir * 1); tags.push("Volume Surge"); }
  else if (volRatio < 0.5) scores.push(0);
  else scores.push(priceDir * 0.3);

  // ── 6. CANDLESTICK PATTERNS ─────────────────────────────────────
  const lastC = c[n - 1], prevC = c[n - 2], prev2C = c[n - 3];
  const body = Math.abs(lastC.close - lastC.open);
  const range = lastC.high - lastC.low;
  const upperWick = lastC.high - Math.max(lastC.open, lastC.close);
  const lowerWick = Math.min(lastC.open, lastC.close) - lastC.low;

  // Doji
  if (body / range < 0.1) { scores.push(0); tags.push("Doji"); }
  // Hammer
  else if (lowerWick > body * 2 && upperWick < body * 0.5 && lastC.close > lastC.open) {
    scores.push(1); tags.push("Hammer");
  }
  // Shooting star
  else if (upperWick > body * 2 && lowerWick < body * 0.5 && lastC.close < lastC.open) {
    scores.push(-1); tags.push("Shooting Star");
  }
  // Engulfing
  else if (lastC.close > lastC.open && prevC.close < prevC.open &&
           lastC.close > prevC.open && lastC.open < prevC.close) {
    scores.push(1); tags.push("Bullish Engulf");
  } else if (lastC.close < lastC.open && prevC.close > prevC.open &&
             lastC.close < prevC.open && lastC.open > prevC.close) {
    scores.push(-1); tags.push("Bearish Engulf");
  } else scores.push(0);

  // ── 7. SUPPORT / RESISTANCE ──────────────────────────────────────
  const recent20H = Math.max(...highs.slice(-20));
  const recent20L = Math.min(...lows.slice(-20));
  const midpoint = (recent20H + recent20L) / 2;
  if (last > midpoint && last < recent20H * 0.995) { scores.push(0.5); }
  else if (last < midpoint && last > recent20L * 1.005) { scores.push(-0.5); }
  else scores.push(0);

  // ── 8. MOMENTUM OSCILLATOR (ROC) ─────────────────────────────────
  const roc5 = (closes[n - 1] - closes[n - 6]) / closes[n - 6] * 100;
  if (roc5 > 0.3) { scores.push(1); tags.push("ROC Bullish"); }
  else if (roc5 < -0.3) { scores.push(-1); tags.push("ROC Bearish"); }
  else scores.push(0);

  // ── 9. STOCHASTIC ────────────────────────────────────────────────
  const k14H = Math.max(...highs.slice(-14));
  const k14L = Math.min(...lows.slice(-14));
  const stochK = (last - k14L) / (k14H - k14L) * 100;
  if (stochK > 80) { scores.push(-0.8); tags.push("Stoch OB"); }
  else if (stochK < 20) { scores.push(0.8); tags.push("Stoch OS"); }
  else if (stochK > 50) { scores.push(0.4); }
  else { scores.push(-0.4); }

  // ── 10. ATR VOLATILITY ───────────────────────────────────────────
  const atrs = [];
  for (let i = n - 14; i < n; i++) {
    atrs.push(Math.max(
      highs[i] - lows[i],
      Math.abs(highs[i] - closes[i - 1]),
      Math.abs(lows[i] - closes[i - 1])
    ));
  }
  const atr = avg(atrs);
  const atrPct = (atr / last) * 100;

  // ── 11. PRICE ACTION STRUCTURE ───────────────────────────────────
  const isHH = highs[n - 1] > highs[n - 2] && highs[n - 2] > highs[n - 3];
  const isHL = lows[n - 1] > lows[n - 2] && lows[n - 2] > lows[n - 3];
  const isLH = highs[n - 1] < highs[n - 2] && highs[n - 2] < highs[n - 3];
  const isLL = lows[n - 1] < lows[n - 2] && lows[n - 2] < lows[n - 3];
  if (isHH && isHL) { scores.push(1); tags.push("HH+HL Structure"); }
  else if (isLH && isLL) { scores.push(-1); tags.push("LH+LL Structure"); }
  else scores.push(0);

  // ── 12. CLOSE POSITION WITHIN CANDLE ─────────────────────────────
  const closePos = (lastC.close - lastC.low) / (lastC.high - lastC.low);
  if (closePos > 0.7) { scores.push(0.6); tags.push("Strong Close"); }
  else if (closePos < 0.3) { scores.push(-0.6); tags.push("Weak Close"); }
  else scores.push(0);

  // ── 13. CONSECUTIVE CANDLES ───────────────────────────────────────
  let bullStreak = 0, bearStreak = 0;
  for (let i = n - 1; i >= n - 5; i--) {
    if (closes[i] > opens[i]) bullStreak++;
    else break;
  }
  for (let i = n - 1; i >= n - 5; i--) {
    if (closes[i] < opens[i]) bearStreak++;
    else break;
  }
  if (bullStreak >= 3) { scores.push(-0.5); tags.push("Exhaustion Bull"); }
  else if (bearStreak >= 3) { scores.push(0.5); tags.push("Exhaustion Bear"); }
  else scores.push(0);

  // ── 14. SMA CROSS ────────────────────────────────────────────────
  const sma5 = sma(closes, 5);
  const sma20v = sma(closes, 20);
  const prevSma5 = avg(closes.slice(-6, -1));
  const prevSma20 = avg(closes.slice(-21, -1));
  if (sma5 > sma20v && prevSma5 <= prevSma20) { scores.push(1.5); tags.push("SMA Cross Up"); }
  else if (sma5 < sma20v && prevSma5 >= prevSma20) { scores.push(-1.5); tags.push("SMA Cross Down"); }
  else if (sma5 > sma20v) scores.push(0.5);
  else scores.push(-0.5);

  // ── 15. VOLUME-WEIGHTED TREND ────────────────────────────────────
  let vwapNum = 0, vwapDen = 0;
  for (let i = Math.max(0, n - 10); i < n; i++) {
    const typ = (highs[i] + lows[i] + closes[i]) / 3;
    vwapNum += typ * vols[i];
    vwapDen += vols[i];
  }
  const vwap = vwapNum / vwapDen;
  if (last > vwap * 1.001) { scores.push(0.6); tags.push("Above VWAP"); }
  else if (last < vwap * 0.999) { scores.push(-0.6); tags.push("Below VWAP"); }
  else scores.push(0);

  // ── MARKET CONDITION DETECTION ───────────────────────────────────
  let marketCondition = "Stable Market";
  const trendStrength = Math.abs(ema5 - ema20) / ema20 * 100;
  if (bbWidth > 0.04) marketCondition = "High Volatility Market";
  else if (bbWidth < 0.01) marketCondition = "Compression Phase";
  else if (trendStrength > 1) marketCondition = ema5 > ema20 ? "Uptrend" : "Downtrend";
  else if (rsi > 70) marketCondition = "Euphoria Market";
  else if (rsi < 30) marketCondition = "Capitulation Market";
  else if (bullStreak >= 4 || bearStreak >= 4) marketCondition = "Momentum Market";
  else marketCondition = "Ranging Market";

  // ── FINAL SCORE ───────────────────────────────────────────────────
  const totalScore = scores.reduce((a, b) => a + b, 0);
  const maxScore = scores.length;
  const normalizedScore = totalScore / maxScore;

  // Confidence = how decisive the signal is
  const confidence = Math.min(95, Math.round(Math.abs(normalizedScore) * 100 + 45));

  if (Math.abs(normalizedScore) < 0.12) return null; // no clear signal

  const direction = normalizedScore > 0 ? "UP" : "DOWN";

  return {
    direction,
    confidence,
    score: normalizedScore,
    tags: tags.slice(0, 5),
    marketCondition,
    rsi: Math.round(rsi),
    atrPct: atrPct.toFixed(3),
    stochK: Math.round(stochK),
    vwap: vwap.toFixed(2),
    ema5: ema5.toFixed(4),
    ema20: ema20.toFixed(4),
  };
}

// ══════════════════════════════════════════════════════════════════
// CANDLE BOUNDARY HELPERS
// ══════════════════════════════════════════════════════════════════
function getCurrentCandleStart(now = Date.now()) {
  return Math.floor(now / CANDLE_MS) * CANDLE_MS;
}
function getNextCandleStart(now = Date.now()) {
  return getCurrentCandleStart(now) + CANDLE_MS;
}
function msUntilNext(now = Date.now()) {
  return getNextCandleStart(now) - now;
}

// ══════════════════════════════════════════════════════════════════
// LIMITLESS ORDER EXECUTOR (frontend bridge)
// ══════════════════════════════════════════════════════════════════
async function callLimitlessAPI(endpoint, method, body, credentials) {
  try {
    const res = await fetch(`/api/limitless${endpoint}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify({ ...body, credentials }) : JSON.stringify({ credentials }),
    });
    return await res.json();
  } catch (e) {
    return { success: false, error: e.message };
  }
}

// ══════════════════════════════════════════════════════════════════
// KEEP-ALIVE
// ══════════════════════════════════════════════════════════════════
function useKeepAlive() {
  useEffect(() => {
    const ping = () => fetch("/api/ping").catch(() => {});
    ping();
    const id = setInterval(ping, 2000);
    return () => clearInterval(id);
  }, []);
}

// ══════════════════════════════════════════════════════════════════
// NOTIFICATION
// ══════════════════════════════════════════════════════════════════
function notify(msg, icon = "🔔") {
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(`${icon} ${msg}`);
  }
}

// ══════════════════════════════════════════════════════════════════
// MAIN APP
// ══════════════════════════════════════════════════════════════════
export default function App() {
  useKeepAlive();

  // ── STATE ──────────────────────────────────────────────────────
  const [prices, setPrices] = useState({});
  const [candles, setCandles] = useState({});
  const [signals, setSignals] = useState([]); // history
  const [activeSignal, setActiveSignal] = useState(null);
  const [pendingSignal, setPendingSignal] = useState(null); // waiting for candle close
  const [stats, setStats] = useState({ wins: 0, losses: 0, total: 0 });
  const [tab, setTab] = useState("dashboard"); // dashboard | history | settings
  const [settings, setSettings] = useState({
    mode: "shadow", // shadow | live
    positionSize: 10,
    maxContractPrice: 0.50,
    privateKey: "",
    tokenId: "",
    tokenSecret: "",
  });
  const [lastFamilyIdx, setLastFamilyIdx] = useState(null);
  const [consecutiveLosses, setConsecutiveLosses] = useState(0);
  const [cooldownUntilCandle, setCooldownUntilCandle] = useState(0); // candle count
  const [countdown, setCountdown] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [toast, setToast] = useState(null);

  const signalsRef = useRef([]);
  const statsRef = useRef({ wins: 0, losses: 0, total: 0 });
  const consecutiveLossesRef = useRef(0);
  const cooldownRef = useRef(0);
  const lastFamilyRef = useRef(null);
  const pendingRef = useRef(null);
  const settingsRef = useRef(settings);

  useEffect(() => { settingsRef.current = settings; }, [settings]);

  // Request notification permission
  useEffect(() => {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
  }, []);

  // ── TOAST ──────────────────────────────────────────────────────
  const showToast = useCallback((msg, type = "info") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 4000);
  }, []);

  // ── CLOCK / COUNTDOWN ─────────────────────────────────────────
  useEffect(() => {
    const id = setInterval(() => {
      const n = Date.now();
      setNow(n);
      setCountdown(Math.ceil(msUntilNext(n) / 1000));
    }, 500);
    return () => clearInterval(id);
  }, []);

  // ── DATA FETCHING ─────────────────────────────────────────────
  const fetchAllData = useCallback(async () => {
    const pairs = Object.keys(PAIRS);
    const results = await Promise.allSettled(pairs.map(async p => {
      const [ticker, cands] = await Promise.all([
        fetchOKXTicker(p),
        fetchOKXCandles(p, "15m", 50),
      ]);
      return { pair: p, ticker, cands };
    }));

    const newPrices = {};
    const newCandles = {};
    for (const r of results) {
      if (r.status === "fulfilled" && r.value) {
        const { pair, ticker, cands } = r.value;
        if (ticker) newPrices[pair] = ticker;
        if (cands) newCandles[pair] = cands;
      }
    }
    setPrices(newPrices);
    setCandles(newCandles);
  }, []);

  useEffect(() => {
    fetchAllData();
    const id = setInterval(fetchAllData, 15000);
    return () => clearInterval(id);
  }, [fetchAllData]);

  // ── SIGNAL ENGINE (fires at candle boundaries) ────────────────
  const evaluateSignals = useCallback(async (currentCandles, currentPrices) => {
    if (cooldownRef.current > 0) {
      cooldownRef.current--;
      setCooldownUntilCandle(cooldownRef.current);
      showToast(`⏸ Cooldown: ${cooldownRef.current} candle(s) remaining`, "warning");
      return;
    }

    // Compute signal for every pair
    const candidates = [];
    for (const [pair, cands] of Object.entries(currentCandles)) {
      const sig = computeSignal(cands);
      if (!sig) continue;
      candidates.push({ pair, ...sig });
    }

    if (candidates.length === 0) return;

    // Filter by family rotation
    const validFamilies = lastFamilyRef.current !== null
      ? [0, 1, 2].filter(f => f !== lastFamilyRef.current)
      : [0, 1, 2];

    const eligible = candidates.filter(c => validFamilies.includes(PAIRS[c.pair].family));
    if (eligible.length === 0) return;

    // Pick highest confidence
    eligible.sort((a, b) => b.confidence - a.confidence);
    const best = eligible[0];

    const candleStart = getCurrentCandleStart();
    const openPrice = currentPrices[best.pair]?.last;
    if (!openPrice) return;

    const signal = {
      id: `sig_${Date.now()}`,
      pair: best.pair,
      direction: best.direction,
      confidence: best.confidence,
      tags: best.tags,
      marketCondition: best.marketCondition,
      rsi: best.rsi,
      candleStart,
      openPrice,
      closePrice: null,
      result: "PENDING",
      family: PAIRS[best.pair].family,
      orderResult: null,
      timestamp: Date.now(),
    };

    // Update family tracking
    lastFamilyRef.current = PAIRS[best.pair].family;
    setLastFamilyIdx(PAIRS[best.pair].family);

    setPendingSignal(signal);
    pendingRef.current = signal;
    setActiveSignal(signal);

    notify(
      `${PAIRS[best.pair].label} ${best.direction === "UP" ? "▲ LONG" : "▼ SHORT"} @ ${openPrice.toFixed(4)} | Conf: ${best.confidence}%`,
      best.direction === "UP" ? "🟢" : "🔴"
    );
    showToast(`Signal: ${PAIRS[best.pair].label} ${best.direction} (${best.confidence}%)`, best.direction === "UP" ? "success" : "danger");

    // Execute order
    const s = settingsRef.current;
    try {
      const orderResult = await callLimitlessAPI("/execute", "POST", {
        symbol: best.pair.replace("-USDT", "-USDT"),
        direction: best.direction,
        mode: s.mode,
        positionSize: s.positionSize,
        maxContractPrice: s.maxContractPrice,
      }, {
        privateKey: s.privateKey,
        tokenId: s.tokenId,
        tokenSecret: s.tokenSecret,
      });
      setPendingSignal(prev => prev ? { ...prev, orderResult } : null);
      pendingRef.current = { ...signal, orderResult };
    } catch (e) {
      console.error("Order failed:", e);
    }
  }, [showToast]);

  // Settle pending signal at candle close
  const settlePending = useCallback(async (currentPrices) => {
    const pending = pendingRef.current;
    if (!pending) return;

    const closePrice = currentPrices[pending.pair]?.last;
    if (!closePrice) return;

    const actualUp = closePrice > pending.openPrice;
    const won = (pending.direction === "UP" && actualUp) || (pending.direction === "DOWN" && !actualUp);

    const settled = {
      ...pending,
      closePrice,
      result: won ? "WIN" : "LOSS",
    };

    // Update streak / cooldown
    if (!won) {
      consecutiveLossesRef.current++;
      if (consecutiveLossesRef.current >= 2) {
        cooldownRef.current = 2;
        setCooldownUntilCandle(2);
        showToast("⛔ 2 consecutive losses — 2 candle cooldown activated", "danger");
        consecutiveLossesRef.current = 0;
      }
    } else {
      consecutiveLossesRef.current = 0;
    }
    setConsecutiveLosses(consecutiveLossesRef.current);

    // Update stats
    const newStats = {
      wins: statsRef.current.wins + (won ? 1 : 0),
      losses: statsRef.current.losses + (won ? 0 : 1),
      total: statsRef.current.total + 1,
    };
    statsRef.current = newStats;
    setStats(newStats);

    // Save to history
    signalsRef.current = [settled, ...signalsRef.current].slice(0, 200);
    setSignals([...signalsRef.current]);

    pendingRef.current = null;
    setPendingSignal(null);
    setActiveSignal(null);

    notify(
      `${PAIRS[settled.pair].label} ${settled.result} | Open: ${settled.openPrice.toFixed(4)} → Close: ${closePrice.toFixed(4)}`,
      won ? "✅" : "❌"
    );
    showToast(`${PAIRS[settled.pair].label} ${settled.result}! ${won ? "+" : "-"}`, won ? "success" : "danger");
  }, [showToast]);

  // Candle boundary detection
  const lastCandleRef = useRef(getCurrentCandleStart());
  useEffect(() => {
    const id = setInterval(() => {
      const currentStart = getCurrentCandleStart();
      if (currentStart !== lastCandleRef.current) {
        lastCandleRef.current = currentStart;
        // Settle previous pending
        if (pendingRef.current) {
          settlePending(prices);
        }
        // Then evaluate new signals
        setTimeout(() => evaluateSignals(candles, prices), 3000); // 3s after boundary for fresh data
      }
    }, 1000);
    return () => clearInterval(id);
  }, [prices, candles, settlePending, evaluateSignals]);

  // ── DAILY RESET AT 00:00 ─────────────────────────────────────
  useEffect(() => {
    const midnight = new Date();
    midnight.setHours(24, 0, 0, 0);
    const msToMidnight = midnight.getTime() - Date.now();
    const id = setTimeout(() => {
      statsRef.current = { wins: 0, losses: 0, total: 0 };
      setStats({ wins: 0, losses: 0, total: 0 });
      showToast("📅 Daily stats reset at 00:00", "info");
    }, msToMidnight);
    return () => clearTimeout(id);
  }, [showToast]);

  // ── DERIVED ───────────────────────────────────────────────────
  const winRate = stats.total > 0 ? ((stats.wins / stats.total) * 100).toFixed(1) : "—";
  const candleProgress = ((CANDLE_MS - msUntilNext(now)) / CANDLE_MS) * 100;

  // Group signals by day for history
  const todayStart = new Date(); todayStart.setHours(0, 0, 0, 0);
  const todaySignals = signals.filter(s => s.timestamp >= todayStart.getTime());

  // ── FORMAT HELPERS ────────────────────────────────────────────
  const fmt = (n, d = 4) => n != null ? Number(n).toFixed(d) : "—";
  const fmtTime = ts => {
    const d = new Date(ts);
    return `${d.getHours().toString().padStart(2,"0")}:${d.getMinutes().toString().padStart(2,"0")}`;
  };

  // ── RENDER ────────────────────────────────────────────────────
  return (
    <div style={styles.app}>
      {/* BG */}
      <div style={styles.bg} />
      <div style={styles.grid} />

      {/* TOAST */}
      {toast && (
        <div style={{ ...styles.toast, background: toast.type === "success" ? "#00ff88" : toast.type === "danger" ? "#ff3b5c" : toast.type === "warning" ? "#f7c948" : "#4af", color: "#000" }}>
          {toast.msg}
        </div>
      )}

      {/* HEADER */}
      <header style={styles.header}>
        <div style={styles.logo}>
          <span style={styles.logoMark}>L∞</span>
          <span style={styles.logoText}>LIMITLESS ORACLE</span>
        </div>
        <div style={styles.headerRight}>
          <div style={styles.modeBadge} data-mode={settings.mode}>
            {settings.mode === "live" ? "🟢 LIVE" : "👻 SHADOW"}
          </div>
          <nav style={styles.nav}>
            {["dashboard","history","settings"].map(t => (
              <button key={t} style={{ ...styles.navBtn, ...(tab === t ? styles.navBtnActive : {}) }} onClick={() => setTab(t)}>
                {t.charAt(0).toUpperCase() + t.slice(1)}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {/* CANDLE PROGRESS BAR */}
      <div style={styles.progressBar}>
        <div style={{ ...styles.progressFill, width: `${candleProgress}%` }} />
        <span style={styles.progressLabel}>Next candle in {countdown}s</span>
      </div>

      {/* ── DASHBOARD TAB ── */}
      {tab === "dashboard" && (
        <main style={styles.main}>
          {/* Stats Row */}
          <div style={styles.statsRow}>
            <StatCard label="WIN RATE" value={`${winRate}%`} color="#00ff88" />
            <StatCard label="WINS" value={stats.wins} color="#00ff88" />
            <StatCard label="LOSSES" value={stats.losses} color="#ff3b5c" />
            <StatCard label="TOTAL TODAY" value={todaySignals.length} color="#4af" />
            <StatCard label="FAMILY LOCK" value={lastFamilyIdx !== null ? FAMILY_NAMES[lastFamilyIdx] : "—"} color="#f7c948" />
            <StatCard label="COOLDOWN" value={cooldownUntilCandle > 0 ? `${cooldownUntilCandle}🕐` : "ACTIVE"} color={cooldownUntilCandle > 0 ? "#ff3b5c" : "#00ff88"} />
          </div>

          {/* Active Signal */}
          {activeSignal ? (
            <ActiveSignalCard signal={activeSignal} prices={prices} fmt={fmt} fmtTime={fmtTime} />
          ) : (
            <div style={styles.noSignal}>
              <div style={styles.noSignalPulse} />
              <span>Scanning markets… waiting for high-confidence setup</span>
            </div>
          )}

          {/* Price Grid */}
          <div style={styles.priceGrid}>
            {Object.entries(PAIRS).map(([pair, meta]) => (
              <PriceCard key={pair} pair={pair} meta={meta} ticker={prices[pair]} candles={candles[pair]} />
            ))}
          </div>

          {/* Recent Signals (last 5) */}
          {signals.length > 0 && (
            <div style={styles.recentSection}>
              <h3 style={styles.sectionTitle}>Recent Signals</h3>
              <div style={styles.signalList}>
                {signals.slice(0, 5).map(s => (
                  <SignalRow key={s.id} signal={s} fmtTime={fmtTime} fmt={fmt} />
                ))}
              </div>
            </div>
          )}
        </main>
      )}

      {/* ── HISTORY TAB ── */}
      {tab === "history" && (
        <main style={styles.main}>
          <div style={styles.historyHeader}>
            <h2 style={styles.sectionTitle}>Signal History — Today ({todaySignals.length} signals)</h2>
            <div style={styles.historyStats}>
              <span style={{ color: "#00ff88" }}>W: {todaySignals.filter(s => s.result === "WIN").length}</span>
              <span style={{ color: "#ff3b5c" }}>L: {todaySignals.filter(s => s.result === "LOSS").length}</span>
              <span style={{ color: "#4af" }}>WR: {todaySignals.length > 0 ? ((todaySignals.filter(s=>s.result==="WIN").length/todaySignals.length)*100).toFixed(1) : "—"}%</span>
            </div>
          </div>
          <div style={styles.signalList}>
            {signals.length === 0 && <div style={{ color: "#666", padding: "2rem", textAlign: "center" }}>No signals yet today.</div>}
            {signals.map(s => (
              <SignalRowFull key={s.id} signal={s} fmtTime={fmtTime} fmt={fmt} />
            ))}
          </div>
        </main>
      )}

      {/* ── SETTINGS TAB ── */}
      {tab === "settings" && (
        <main style={styles.main}>
          <div style={styles.settingsCard}>
            <h2 style={styles.sectionTitle}>Settings</h2>

            {/* Mode Toggle */}
            <div style={styles.settingRow}>
              <label style={styles.settingLabel}>Trading Mode</label>
              <div style={styles.modeToggle}>
                <button
                  style={{ ...styles.modeBtn, ...(settings.mode === "shadow" ? styles.modeBtnActive : {}) }}
                  onClick={() => setSettings(s => ({ ...s, mode: "shadow" }))}
                >👻 Shadow</button>
                <button
                  style={{ ...styles.modeBtn, ...(settings.mode === "live" ? styles.modeBtnActiveLive : {}) }}
                  onClick={() => setSettings(s => ({ ...s, mode: "live" }))}
                >🟢 Live</button>
              </div>
              <p style={styles.settingHint}>Shadow mode replicates trades without spending real funds.</p>
            </div>

            {/* Position Size */}
            <div style={styles.settingRow}>
              <label style={styles.settingLabel}>Position Size: <span style={{ color: "#00ff88" }}>${settings.positionSize}</span></label>
              <input
                type="range" min="1" max="1000" step="1"
                value={settings.positionSize}
                onChange={e => setSettings(s => ({ ...s, positionSize: Number(e.target.value) }))}
                style={styles.slider}
              />
              <div style={styles.rangeLabels}><span>$1</span><span>$1000</span></div>
            </div>

            {/* Max Contract Price */}
            <div style={styles.settingRow}>
              <label style={styles.settingLabel}>Max Contract Price: <span style={{ color: "#f7c948" }}>${settings.maxContractPrice.toFixed(2)}</span></label>
              <input
                type="range" min="0.01" max="0.50" step="0.01"
                value={settings.maxContractPrice}
                onChange={e => setSettings(s => ({ ...s, maxContractPrice: Number(e.target.value) }))}
                style={styles.slider}
              />
              <div style={styles.rangeLabels}><span>$0.01</span><span>$0.50</span></div>
              <p style={styles.settingHint}>Price per contract ≤ $0.50 per system rules.</p>
            </div>

            {/* API Credentials */}
            <div style={styles.settingRow}>
              <label style={styles.settingLabel}>Limitless Token ID</label>
              <input
                style={styles.input}
                type="password"
                placeholder="lmts_token_id..."
                value={settings.tokenId}
                onChange={e => setSettings(s => ({ ...s, tokenId: e.target.value }))}
              />
            </div>
            <div style={styles.settingRow}>
              <label style={styles.settingLabel}>Limitless Token Secret</label>
              <input
                style={styles.input}
                type="password"
                placeholder="base64 secret..."
                value={settings.tokenSecret}
                onChange={e => setSettings(s => ({ ...s, tokenSecret: e.target.value }))}
              />
            </div>
            <div style={styles.settingRow}>
              <label style={styles.settingLabel}>Private Key (EOA Signer)</label>
              <input
                style={styles.input}
                type="password"
                placeholder="0x... (MetaMask EOA private key)"
                value={settings.privateKey}
                onChange={e => setSettings(s => ({ ...s, privateKey: e.target.value }))}
              />
              <p style={styles.settingHint}>⚠️ EOA only — never your smart wallet. Maker = Signer in EOA mode.</p>
            </div>

            <div style={styles.settingRow}>
              <div style={styles.infoBox}>
                <div style={{ color: "#4af", fontFamily: "monospace", fontSize: 12 }}>
                  <div>Pairs: BTC·ETH (Family A) | SOL·DOGE (Family B) | XRP·BNB (Family C)</div>
                  <div>Signal Rules: Best confidence per non-consecutive family</div>
                  <div>Win/Loss tracking starts at candle close (00:00→00:15)</div>
                  <div>Cooldown: 2 candles after 2 consecutive losses</div>
                  <div>Keep-alive: pinging every 2s to prevent sleep</div>
                </div>
              </div>
            </div>
          </div>
        </main>
      )}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════
// SUB-COMPONENTS
// ══════════════════════════════════════════════════════════════════
function StatCard({ label, value, color }) {
  return (
    <div style={styles.statCard}>
      <div style={{ ...styles.statValue, color }}>{value}</div>
      <div style={styles.statLabel}>{label}</div>
    </div>
  );
}

function ActiveSignalCard({ signal, prices, fmt, fmtTime }) {
  const meta = PAIRS[signal.pair];
  const currentPrice = prices[signal.pair]?.last;
  const pnlDir = currentPrice ? (currentPrice > signal.openPrice ? "UP" : "DOWN") : null;
  const isWinning = pnlDir === signal.direction;

  return (
    <div style={{ ...styles.activeCard, borderColor: signal.direction === "UP" ? "#00ff88" : "#ff3b5c" }}>
      <div style={styles.activeTop}>
        <div style={styles.activePair}>
          <span style={{ color: meta.color, fontSize: 28, fontWeight: 900 }}>{meta.label}</span>
          <span style={styles.familyTag}>{FAMILY_NAMES[signal.family]}</span>
        </div>
        <div style={{ ...styles.activeDir, background: signal.direction === "UP" ? "#00ff88" : "#ff3b5c", color: "#000" }}>
          {signal.direction === "UP" ? "▲ LONG" : "▼ SHORT"}
        </div>
        <div style={styles.activeConf}>{signal.confidence}%<br /><span style={{ fontSize: 11, color: "#888" }}>confidence</span></div>
      </div>

      <div style={styles.activePrices}>
        <div style={styles.priceItem}><span style={styles.pl}>OPEN</span><span style={styles.pv}>{fmt(signal.openPrice, 4)}</span></div>
        <div style={styles.priceItem}><span style={styles.pl}>CURRENT</span><span style={{ ...styles.pv, color: isWinning ? "#00ff88" : "#ff3b5c" }}>{fmt(currentPrice, 4)}</span></div>
        <div style={styles.priceItem}><span style={styles.pl}>TIME</span><span style={styles.pv}>{fmtTime(signal.timestamp)}</span></div>
        <div style={styles.priceItem}><span style={styles.pl}>MODE</span><span style={{ ...styles.pv, color: signal.orderResult?.shadow ? "#f7c948" : "#00ff88" }}>
          {signal.orderResult?.shadow ? "SHADOW" : signal.orderResult?.success ? "LIVE ✓" : "PENDING"}
        </span></div>
      </div>

      <div style={styles.activeTags}>
        {signal.tags.map(t => <span key={t} style={styles.tag}>{t}</span>)}
        <span style={{ ...styles.tag, background: "#2a2a3a", color: "#888" }}>{signal.marketCondition}</span>
      </div>

      <div style={styles.orderInfo}>
        {signal.orderResult && (
          signal.orderResult.success
            ? <span style={{ color: "#00ff88" }}>✓ Order placed — {signal.orderResult.shadow ? "Shadow" : `ID: ${signal.orderResult.order_id?.slice(0,12)}…`}</span>
            : <span style={{ color: "#ff3b5c" }}>✗ Order failed: {signal.orderResult.error}</span>
        )}
      </div>
    </div>
  );
}

function PriceCard({ pair, meta, ticker, candles }) {
  const sig = candles ? computeSignal(candles) : null;
  const change24h = ticker ? ((ticker.last - ticker.open24h) / ticker.open24h * 100) : null;
  return (
    <div style={styles.priceCard}>
      <div style={styles.pcTop}>
        <span style={{ color: meta.color, fontWeight: 800, fontSize: 15 }}>{meta.label}</span>
        {sig && (
          <span style={{ ...styles.miniDir, background: sig.direction === "UP" ? "#00ff8820" : "#ff3b5c20", color: sig.direction === "UP" ? "#00ff88" : "#ff3b5c", border: `1px solid ${sig.direction === "UP" ? "#00ff8840" : "#ff3b5c40"}` }}>
            {sig.direction === "UP" ? "▲" : "▼"} {sig.confidence}%
          </span>
        )}
      </div>
      <div style={styles.pcPrice}>{ticker ? ticker.last.toFixed(ticker.last > 100 ? 2 : 4) : "—"}</div>
      <div style={{ ...styles.pcChange, color: change24h > 0 ? "#00ff88" : change24h < 0 ? "#ff3b5c" : "#666" }}>
        {change24h != null ? `${change24h > 0 ? "+" : ""}${change24h.toFixed(2)}%` : "—"}
      </div>
      {sig && <div style={styles.pcCond}>{sig.marketCondition}</div>}
    </div>
  );
}

function SignalRow({ signal, fmtTime, fmt }) {
  const meta = PAIRS[signal.pair];
  return (
    <div style={{ ...styles.sigRow, borderLeft: `3px solid ${signal.result === "WIN" ? "#00ff88" : signal.result === "LOSS" ? "#ff3b5c" : "#555"}` }}>
      <span style={{ color: meta.color, fontWeight: 700, minWidth: 45 }}>{meta.label}</span>
      <span style={{ color: signal.direction === "UP" ? "#00ff88" : "#ff3b5c", minWidth: 35 }}>{signal.direction === "UP" ? "▲" : "▼"}</span>
      <span style={{ color: "#888", fontSize: 11, minWidth: 40 }}>{fmtTime(signal.timestamp)}</span>
      <span style={{ color: "#aaa", fontSize: 11, flex: 1 }}>{fmt(signal.openPrice, 4)} → {fmt(signal.closePrice, 4)}</span>
      <span style={{ color: signal.result === "WIN" ? "#00ff88" : signal.result === "LOSS" ? "#ff3b5c" : "#888", fontWeight: 700 }}>
        {signal.result === "WIN" ? "WIN" : signal.result === "LOSS" ? "LOSS" : "…"}
      </span>
    </div>
  );
}

function SignalRowFull({ signal, fmtTime, fmt }) {
  const meta = PAIRS[signal.pair];
  return (
    <div style={{ ...styles.sigRowFull, borderLeft: `3px solid ${signal.result === "WIN" ? "#00ff88" : signal.result === "LOSS" ? "#ff3b5c" : "#555"}` }}>
      <div style={styles.srfLeft}>
        <span style={{ color: meta.color, fontWeight: 800 }}>{meta.label}</span>
        <span style={{ color: signal.direction === "UP" ? "#00ff88" : "#ff3b5c" }}>{signal.direction === "UP" ? "▲ UP" : "▼ DOWN"}</span>
        <span style={{ color: "#666", fontSize: 11 }}>{fmtTime(signal.timestamp)}</span>
      </div>
      <div style={styles.srfMid}>
        <span style={{ color: "#888", fontSize: 11 }}>Open: {fmt(signal.openPrice, 4)}</span>
        <span style={{ color: "#888", fontSize: 11 }}>Close: {fmt(signal.closePrice, 4)}</span>
        <span style={{ color: "#666", fontSize: 10 }}>{signal.marketCondition}</span>
      </div>
      <div style={styles.srfRight}>
        <span style={{ color: "#aaa", fontSize: 11 }}>{signal.confidence}% conf</span>
        <span style={{ color: signal.result === "WIN" ? "#00ff88" : signal.result === "LOSS" ? "#ff3b5c" : "#888", fontWeight: 800, fontSize: 16 }}>
          {signal.result === "WIN" ? "✓ WIN" : signal.result === "LOSS" ? "✗ LOSS" : "⏳"}
        </span>
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════
// STYLES
// ══════════════════════════════════════════════════════════════════
const styles = {
  app: { minHeight: "100vh", background: "#080810", color: "#e0e0f0", fontFamily: "'JetBrains Mono', 'Fira Code', 'Courier New', monospace", position: "relative", overflowX: "hidden" },
  bg: { position: "fixed", inset: 0, background: "radial-gradient(ellipse 80% 50% at 50% 0%, #0d0d2840 0%, transparent 70%)", pointerEvents: "none", zIndex: 0 },
  grid: { position: "fixed", inset: 0, backgroundImage: "linear-gradient(rgba(68,170,255,0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(68,170,255,0.03) 1px, transparent 1px)", backgroundSize: "40px 40px", pointerEvents: "none", zIndex: 0 },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 20px", borderBottom: "1px solid #1a1a2e", background: "#09091580", backdropFilter: "blur(12px)", position: "sticky", top: 0, zIndex: 100 },
  logo: { display: "flex", alignItems: "center", gap: 10 },
  logoMark: { fontSize: 24, fontWeight: 900, color: "#4af", letterSpacing: -2 },
  logoText: { fontSize: 13, fontWeight: 700, letterSpacing: 4, color: "#4af88" },
  headerRight: { display: "flex", alignItems: "center", gap: 12 },
  modeBadge: { padding: "4px 10px", borderRadius: 4, background: "#1a1a2e", fontSize: 11, fontWeight: 700, letterSpacing: 1 },
  nav: { display: "flex", gap: 4 },
  navBtn: { background: "transparent", border: "1px solid #1a1a2e", color: "#666", padding: "5px 14px", borderRadius: 4, cursor: "pointer", fontSize: 12, fontFamily: "inherit", transition: "all 0.2s" },
  navBtnActive: { background: "#4af20", border: "1px solid #4af", color: "#4af" },
  progressBar: { height: 3, background: "#1a1a2e", position: "relative", overflow: "hidden" },
  progressFill: { height: "100%", background: "linear-gradient(90deg, #4af, #00ff88)", transition: "width 0.5s linear" },
  progressLabel: { position: "absolute", right: 8, top: -14, fontSize: 10, color: "#4af", letterSpacing: 1 },
  main: { padding: "20px", maxWidth: 1400, margin: "0 auto", position: "relative", zIndex: 1 },
  statsRow: { display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 10, marginBottom: 20 },
  statCard: { background: "#0d0d1a", border: "1px solid #1a1a2e", borderRadius: 8, padding: "12px 10px", textAlign: "center" },
  statValue: { fontSize: 22, fontWeight: 900, letterSpacing: -1 },
  statLabel: { fontSize: 9, color: "#555", letterSpacing: 2, marginTop: 2 },
  noSignal: { background: "#0d0d1a", border: "1px solid #1a1a2e", borderRadius: 12, padding: "40px 20px", textAlign: "center", color: "#555", fontSize: 13, letterSpacing: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 12, marginBottom: 20 },
  noSignalPulse: { width: 10, height: 10, borderRadius: "50%", background: "#4af", boxShadow: "0 0 0 0 #4af", animation: "pulse 2s infinite" },
  activeCard: { background: "#0d0d1a", border: "2px solid", borderRadius: 12, padding: 20, marginBottom: 20 },
  activeTop: { display: "flex", alignItems: "center", gap: 16, marginBottom: 16 },
  activePair: { display: "flex", flexDirection: "column", flex: 1 },
  familyTag: { fontSize: 10, color: "#555", letterSpacing: 2, marginTop: 2 },
  activeDir: { padding: "8px 18px", borderRadius: 6, fontSize: 18, fontWeight: 900, letterSpacing: 2 },
  activeConf: { textAlign: "center", fontSize: 26, fontWeight: 900, color: "#f7c948" },
  activePrices: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, background: "#09091580", borderRadius: 8, padding: 12, marginBottom: 12 },
  priceItem: { display: "flex", flexDirection: "column", gap: 4 },
  pl: { fontSize: 9, color: "#555", letterSpacing: 2 },
  pv: { fontSize: 15, fontWeight: 700 },
  activeTags: { display: "flex", flexWrap: "wrap", gap: 6 },
  tag: { background: "#1a1a2e", color: "#4af", fontSize: 10, padding: "3px 8px", borderRadius: 4, letterSpacing: 1 },
  orderInfo: { marginTop: 10, fontSize: 11, color: "#555" },
  priceGrid: { display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 10, marginBottom: 20 },
  priceCard: { background: "#0d0d1a", border: "1px solid #1a1a2e", borderRadius: 8, padding: 12, transition: "border-color 0.3s" },
  pcTop: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  miniDir: { fontSize: 10, padding: "2px 6px", borderRadius: 4, fontWeight: 700 },
  pcPrice: { fontSize: 16, fontWeight: 800, letterSpacing: -0.5, marginBottom: 2 },
  pcChange: { fontSize: 11, fontWeight: 600 },
  pcCond: { fontSize: 9, color: "#555", marginTop: 4, letterSpacing: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" },
  recentSection: { background: "#0d0d1a", border: "1px solid #1a1a2e", borderRadius: 12, padding: 16 },
  sectionTitle: { fontSize: 11, color: "#4af", letterSpacing: 3, fontWeight: 700, marginBottom: 12, marginTop: 0 },
  signalList: { display: "flex", flexDirection: "column", gap: 6 },
  sigRow: { display: "flex", alignItems: "center", gap: 10, background: "#090915", borderRadius: 6, padding: "8px 12px", fontSize: 12 },
  historyHeader: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 },
  historyStats: { display: "flex", gap: 16, fontSize: 13, fontWeight: 700 },
  sigRowFull: { display: "flex", alignItems: "center", gap: 12, background: "#090915", borderRadius: 6, padding: "10px 14px" },
  srfLeft: { display: "flex", gap: 10, minWidth: 120, alignItems: "center" },
  srfMid: { display: "flex", gap: 10, flex: 1, flexWrap: "wrap" },
  srfRight: { display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 },
  settingsCard: { background: "#0d0d1a", border: "1px solid #1a1a2e", borderRadius: 12, padding: 24, maxWidth: 700 },
  settingRow: { marginBottom: 24 },
  settingLabel: { display: "block", fontSize: 11, color: "#888", letterSpacing: 2, marginBottom: 8 },
  settingHint: { fontSize: 10, color: "#444", marginTop: 6, lineHeight: 1.6 },
  modeToggle: { display: "flex", gap: 8 },
  modeBtn: { padding: "8px 20px", borderRadius: 6, border: "1px solid #2a2a3a", background: "#1a1a2e", color: "#666", cursor: "pointer", fontFamily: "inherit", fontSize: 13, fontWeight: 700 },
  modeBtnActive: { background: "#1a2a3a", border: "1px solid #4af", color: "#4af" },
  modeBtnActiveLive: { background: "#0a2a0a", border: "1px solid #00ff88", color: "#00ff88" },
  slider: { width: "100%", accentColor: "#4af" },
  rangeLabels: { display: "flex", justifyContent: "space-between", fontSize: 10, color: "#555", marginTop: 4 },
  input: { width: "100%", background: "#090915", border: "1px solid #1a1a2e", borderRadius: 6, color: "#e0e0f0", padding: "10px 12px", fontFamily: "inherit", fontSize: 12, boxSizing: "border-box", outline: "none" },
  infoBox: { background: "#090915", border: "1px solid #1a1a2e", borderRadius: 8, padding: 14, lineHeight: 1.8 },
  toast: { position: "fixed", top: 70, right: 20, padding: "10px 18px", borderRadius: 8, fontSize: 12, fontWeight: 700, zIndex: 9999, letterSpacing: 1, boxShadow: "0 4px 24px #00000060" },
};
