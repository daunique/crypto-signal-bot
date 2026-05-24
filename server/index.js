/**
 * Limitless Oracle — Server Engine v4
 * ═══════════════════════════════════════
 * • Signal fires at exact candle OPEN (xx:00/15/30/45)
 * • Settles using confirmed OKX candle close price
 * • All signals + demo trades persisted to Supabase (pg Transaction Pooler)
 * • SSE streams live state to browser
 */

import express from "express";
import path    from "path";
import { fileURLToPath } from "url";
import crypto  from "crypto";
import { ethers } from "ethers";
import {
  initDB, saveSignal, getRecentSignals, getSignalsByDate,
  getAvailableDates, upsertDailyStats, getAllDailyStats,
  saveDemoTrade, getDemoTrades, getDemoStats,
} from "./db.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json());

const PORT      = process.env.PORT || 5000;
const API_BASE  = "https://api.limitless.exchange";
const CHAIN_ID  = 8453n;
const ZERO_ADDR = "0x0000000000000000000000000000000000000000";
const CANDLE_MS = 15 * 60 * 1000;

const PAIRS = {
  "BTC-USDT":  { family:0, label:"BTC"  },
  "ETH-USDT":  { family:0, label:"ETH"  },
  "SOL-USDT":  { family:1, label:"SOL"  },
  "DOGE-USDT": { family:1, label:"DOGE" },
  "XRP-USDT":  { family:2, label:"XRP"  },
  "BNB-USDT":  { family:2, label:"BNB"  },
};

const state = {
  prices:{}, candles:{}, signals:[], activeSignal:null, pendingSignal:null,
  stats:{ wins:0, losses:0, total:0 },
  lastFamily:null, consecutiveLosses:0, cooldownCandles:0,
  demoStats:{ wins:0, losses:0, total:0, pnl:0 },
  settings:{
    mode:             process.env.TRADE_MODE            || "shadow",
    positionSize:     Number(process.env.POSITION_SIZE  || 10),
    maxContractPrice: Number(process.env.MAX_CONTRACT_PRICE || 0.50),
    demoStake:        Number(process.env.DEMO_STAKE     || 10),
    privateKey:   process.env.LIMITLESS_PRIVATE_KEY   || "",
    tokenId:      process.env.LIMITLESS_TOKEN_ID      || "",
    tokenSecret:  process.env.LIMITLESS_TOKEN_SECRET  || "",
  },
};

const sseClients = new Set();
function broadcast(event, data) {
  const msg = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const r of sseClients) { try { r.write(msg); } catch {} }
}

// ══════════════════════════════════════════════════════════════════
// TIME
// ══════════════════════════════════════════════════════════════════
function candleStartOf(now=Date.now()){ return Math.floor(now/CANDLE_MS)*CANDLE_MS; }
function nextCandleStart(now=Date.now()){ return candleStartOf(now)+CANDLE_MS; }
function todayUTC(){ return new Date().toISOString().slice(0,10); }

// ══════════════════════════════════════════════════════════════════
// OKX DATA
// ══════════════════════════════════════════════════════════════════
async function fetchOKXCandles(instId, limit=60){
  try{
    const r=await fetch(`https://www.okx.com/api/v5/market/candles?instId=${instId}&bar=15m&limit=${limit}`,{signal:AbortSignal.timeout(10000)});
    const j=await r.json();
    if(j.code!=="0")return null;
    return j.data.map(c=>({ts:Number(c[0]),open:+c[1],high:+c[2],low:+c[3],close:+c[4],vol:+c[5],confirm:c[8]==="1"})).reverse();
  }catch{return null;}
}

async function fetchOKXTicker(instId){
  try{
    const r=await fetch(`https://www.okx.com/api/v5/market/ticker?instId=${instId}`,{signal:AbortSignal.timeout(10000)});
    const j=await r.json();
    if(j.code!=="0"||!j.data?.[0])return null;
    const d=j.data[0];
    return{last:+d.last,open24h:+d.open24h,high24h:+d.high24h,low24h:+d.low24h,vol24h:+d.vol24h};
  }catch{return null;}
}

async function refreshMarketData(){
  const results=await Promise.allSettled(Object.keys(PAIRS).map(async p=>{
    const[ticker,candles]=await Promise.all([fetchOKXTicker(p),fetchOKXCandles(p)]);
    return{pair:p,ticker,candles};
  }));
  for(const r of results){
    if(r.status==="fulfilled"&&r.value){
      const{pair,ticker,candles}=r.value;
      if(ticker)state.prices[pair]=ticker;
      if(candles)state.candles[pair]=candles;
    }
  }
  broadcast("prices",state.prices);
}

// ══════════════════════════════════════════════════════════════════
// SIGNAL ENGINE
// ══════════════════════════════════════════════════════════════════
function computeSignal(candles){
  if(!candles||candles.length<22)return null;
  const closed=candles.filter(c=>c.confirm);
  if(closed.length<15)return null;
  const c=closed,n=c.length;
  const closes=c.map(x=>x.close),highs=c.map(x=>x.high),lows=c.map(x=>x.low),vols=c.map(x=>x.vol);
  const last=closes[n-1];
  const avg=a=>a.reduce((s,v)=>s+v,0)/a.length;
  const ema=(a,p)=>{const k=2/(p+1);let e=a[0];for(let i=1;i<a.length;i++)e=a[i]*k+e*(1-k);return e;};
  const sma=(a,p)=>avg(a.slice(-p));
  const std=a=>{const m=avg(a);return Math.sqrt(avg(a.map(x=>(x-m)**2)));};
  const S=[];
  const e5=ema(closes.slice(-10),5),e10=ema(closes.slice(-14),10),e20=ema(closes.slice(-24),20);
  if(e5>e10&&e10>e20)S.push(1);else if(e5<e10&&e10<e20)S.push(-1);else S.push(0);
  let g=0,l=0;for(let i=n-14;i<n;i++){const d=closes[i]-closes[i-1];if(d>0)g+=d;else l-=d;}
  const rsi=100-100/(1+(g/14)/((l/14)||0.001));
  if(rsi>55&&rsi<75)S.push(1);else if(rsi<45&&rsi>25)S.push(-1);
  else if(rsi>=75)S.push(-0.5);else if(rsi<=25)S.push(0.5);else S.push(0);
  const m1=ema(closes.slice(-16),12)-ema(closes.slice(-30),26);
  const m2=ema(closes.slice(-17,-1),12)-ema(closes.slice(-31,-1),26);
  if(m1>0&&m1>m2)S.push(1);else if(m1<0&&m1<m2)S.push(-1);else S.push(0);
  const bbM=sma(closes,20),bbS=std(closes.slice(-20)),bbU=bbM+2*bbS,bbL=bbM-2*bbS;
  if(last>bbM&&last<bbU*.98)S.push(0.7);else if(last<bbM&&last>bbL*1.02)S.push(-0.7);
  else if(last<=bbL)S.push(1);else if(last>=bbU)S.push(-1);else S.push(0);
  const avgV=avg(vols.slice(-10)),vr=vols[n-1]/avgV,pd=closes[n-1]>closes[n-2]?1:-1;
  S.push(vr>1.5?pd:pd*0.3);
  const lc=c[n-1],pc=c[n-2];
  const body=Math.abs(lc.close-lc.open),range=(lc.high-lc.low)||0.0001;
  const uw=lc.high-Math.max(lc.open,lc.close),lw=Math.min(lc.open,lc.close)-lc.low;
  if(body/range<0.1)S.push(0);
  else if(lw>body*2&&uw<body*.5&&lc.close>lc.open)S.push(1);
  else if(uw>body*2&&lw<body*.5&&lc.close<lc.open)S.push(-1);
  else if(lc.close>lc.open&&pc.close<pc.open&&lc.close>pc.open&&lc.open<pc.close)S.push(1);
  else if(lc.close<lc.open&&pc.close>pc.open&&lc.close<pc.open&&lc.open>pc.close)S.push(-1);
  else S.push(0);
  const roc=(closes[n-1]-closes[n-6])/closes[n-6]*100;
  if(roc>0.3)S.push(1);else if(roc<-0.3)S.push(-1);else S.push(0);
  const kH=Math.max(...highs.slice(-14)),kL=Math.min(...lows.slice(-14));
  const stoch=(last-kL)/(kH-kL)*100;
  if(stoch>80)S.push(-0.8);else if(stoch<20)S.push(0.8);else if(stoch>50)S.push(0.4);else S.push(-0.4);
  const HH=highs[n-1]>highs[n-2]&&highs[n-2]>highs[n-3],HL=lows[n-1]>lows[n-2]&&lows[n-2]>lows[n-3];
  const LH=highs[n-1]<highs[n-2]&&highs[n-2]<highs[n-3],LL=lows[n-1]<lows[n-2]&&lows[n-2]<lows[n-3];
  if(HH&&HL)S.push(1);else if(LH&&LL)S.push(-1);else S.push(0);
  const cp=(lc.close-lc.low)/((lc.high-lc.low)||0.0001);
  if(cp>0.7)S.push(0.6);else if(cp<0.3)S.push(-0.6);else S.push(0);
  const s5=sma(closes,5),s20=sma(closes,20);
  if(s5>s20&&avg(closes.slice(-6,-1))<=avg(closes.slice(-21,-1)))S.push(1.5);
  else if(s5<s20&&avg(closes.slice(-6,-1))>=avg(closes.slice(-21,-1)))S.push(-1.5);
  else if(s5>s20)S.push(0.5);else S.push(-0.5);
  let vN=0,vD=0;for(let i=Math.max(0,n-10);i<n;i++){const t=(highs[i]+lows[i]+closes[i])/3;vN+=t*vols[i];vD+=vols[i];}
  const vwap=vN/vD;
  if(last>vwap*1.001)S.push(0.6);else if(last<vwap*.999)S.push(-0.6);else S.push(0);
  const total=S.reduce((a,b)=>a+b,0)/S.length;
  if(Math.abs(total)<0.12)return null;
  const bbW=(bbU-bbL)/bbM,tStr=Math.abs(e5-e20)/e20*100;
  let mc="Ranging";
  if(bbW>0.04)mc="High Volatility";else if(bbW<0.01)mc="Compression";
  else if(tStr>1)mc=e5>e20?"Uptrend":"Downtrend";
  else if(rsi>70)mc="Euphoria";else if(rsi<30)mc="Capitulation";
  return{direction:total>0?"UP":"DOWN",confidence:Math.min(95,Math.round(Math.abs(total)*100+45)),marketCondition:mc,rsi:Math.round(rsi)};
}

// ══════════════════════════════════════════════════════════════════
// DEMO TRADE — mirrors live signal for paper trading
// ══════════════════════════════════════════════════════════════════
async function openDemoTrade(signal){
  const stake=state.settings.demoStake;
  const price=state.settings.maxContractPrice;
  const contracts=Math.round((stake/price)*10000)/10000;
  const trade={
    id:`demo_${signal.id}`,
    signalId:signal.id,
    pair:signal.pair,
    direction:signal.direction,
    openPrice:signal.openPrice,
    closePrice:null,
    result:"PENDING",
    contracts,
    stakeUsd:stake,
    pnlUsd:null,
    candleStart:signal.candleStart,
    timestamp:signal.timestamp,
    settledAt:null,
  };
  await saveDemoTrade(trade);
  broadcast("demo_trade_new",trade);
  return trade;
}

async function settleDemoTrade(signal){
  if(!signal.closePrice)return;
  const trade={
    id:`demo_${signal.id}`,
    signalId:signal.id,
    pair:signal.pair,
    direction:signal.direction,
    openPrice:signal.openPrice,
    closePrice:signal.closePrice,
    result:signal.result,
    contracts:Math.round((state.settings.demoStake/state.settings.maxContractPrice)*10000)/10000,
    stakeUsd:state.settings.demoStake,
    pnlUsd:signal.result==="WIN"?state.settings.demoStake*(1/state.settings.maxContractPrice-1):-state.settings.demoStake,
    candleStart:signal.candleStart,
    timestamp:signal.timestamp,
    settledAt:signal.settledAt||Date.now(),
  };
  await saveDemoTrade(trade);
  // Refresh demo stats
  state.demoStats=await getDemoStats();
  broadcast("demo_trade_settled",trade);
  broadcast("demo_stats",state.demoStats);
}

// ══════════════════════════════════════════════════════════════════
// SETTLE LIVE SIGNAL
// ══════════════════════════════════════════════════════════════════
async function settlePending(){
  const p=state.pendingSignal;
  if(!p)return;
  let closePrice=null;
  for(let attempt=1;attempt<=5;attempt++){
    const candles=await fetchOKXCandles(p.pair,10);
    if(candles){
      const match=candles.find(c=>c.ts===p.candleStart&&c.confirm);
      if(match){closePrice=match.close;break;}
      const near=candles.find(c=>Math.abs(c.ts-p.candleStart)<2000&&c.confirm);
      if(near){closePrice=near.close;break;}
    }
    if(attempt<5)await new Promise(r=>setTimeout(r,2000));
  }
  if(!closePrice){
    closePrice=state.prices[p.pair]?.last;
    console.warn(`[settle] Using ticker fallback for ${p.pair}`);
  }
  if(!closePrice){
    console.error(`[settle] No close price for ${p.pair} — skipping`);
    state.pendingSignal=null;state.activeSignal=null;return;
  }
  const won=(p.direction==="UP"&&closePrice>p.openPrice)||(p.direction==="DOWN"&&closePrice<p.openPrice);
  if(!won){
    state.consecutiveLosses++;
    if(state.consecutiveLosses>=2){state.cooldownCandles=2;state.consecutiveLosses=0;console.log("[engine] Cooldown 2 candles");}
  }else state.consecutiveLosses=0;
  state.stats.total++;
  if(won)state.stats.wins++;else state.stats.losses++;
  const settled={...p,closePrice,result:won?"WIN":"LOSS",settledAt:Date.now()};
  state.signals.unshift(settled);
  if(state.signals.length>500)state.signals.length=500;
  state.pendingSignal=null;state.activeSignal=null;
  console.log(`[settle] ${settled.pair} ${settled.direction} open=${settled.openPrice} close=${closePrice} → ${settled.result}`);
  // Persist
  await saveSignal(settled);
  await upsertDailyStats(todayUTC(),state.stats);
  await settleDemoTrade(settled);
  broadcast("signal_settled",settled);
  broadcast("stats",state.stats);
}

// ══════════════════════════════════════════════════════════════════
// FIRE SIGNAL
// ══════════════════════════════════════════════════════════════════
async function fireSignal(){
  if(state.cooldownCandles>0){
    state.cooldownCandles--;
    console.log(`[engine] Cooldown ${state.cooldownCandles} remaining`);
    broadcast("cooldown",{remaining:state.cooldownCandles});
    scheduleNextOpen();return;
  }
  await refreshMarketData();
  const candidates=[];
  for(const[pair,candles] of Object.entries(state.candles)){
    const sig=computeSignal(candles);
    if(sig)candidates.push({pair,...sig});
  }
  console.log(`[engine] ${candidates.length} candidates`);
  broadcast("scan",{candidates:candidates.length,ts:Date.now()});
  if(!candidates.length){scheduleNextOpen();return;}
  const validFams=state.lastFamily!==null?[0,1,2].filter(f=>f!==state.lastFamily):[0,1,2];
  const eligible=candidates.filter(c=>validFams.includes(PAIRS[c.pair].family));
  if(!eligible.length){console.log("[engine] Family blocked");scheduleNextOpen();return;}
  eligible.sort((a,b)=>b.confidence-a.confidence);
  const best=eligible[0];
  const openPrice=state.prices[best.pair]?.last;
  if(!openPrice){scheduleNextOpen();return;}
  // ── Duplicate guard (scheduler.py lines 227-234) ────────────────
  // Block if any PENDING signal already exists for this candle boundary.
  // Prevents double-signals on restarts or boundary edge cases.
  if (state.pendingSignal) {
    console.log(`[generate] BLOCKED — ${state.pendingSignal.pair} ${state.pendingSignal.direction} still PENDING. No new signal until resolved.`);
    scheduleNextOpen(); return;
  }

  state.lastFamily=PAIRS[best.pair].family;
  const signal={
    id:`sig_${Date.now()}`,pair:best.pair,direction:best.direction,
    confidence:best.confidence,marketCondition:best.marketCondition,rsi:best.rsi,
    candleStart:candleStartOf(),openPrice,closePrice:null,
    result:'PENDING',family:PAIRS[best.pair].family,orderResult:null,timestamp:Date.now(),
  };
  state.pendingSignal=signal;state.activeSignal=signal;
  console.log(`[signal] ${signal.pair} ${signal.direction} @ ${openPrice} | ${signal.confidence}% | candle=${new Date(signal.candleStart).toISOString()}`);
  broadcast('signal_new',signal);
  await saveSignal(signal);
  await openDemoTrade(signal);

  // ── Order retry loop (scheduler.py lines 236-346) ─────────────
  // Retry up to 24× with 5s gap. Stop immediately on first success.
  // Permanent failures (insufficient collateral) abort early.
  const s = state.settings;
  const ORDER_MAX  = 24;
  const ORDER_WAIT = 5000;
  let orderResult  = { success: false, error: 'not attempted' };

  if (s.mode === 'live' && s.privateKey && s.tokenId && s.tokenSecret) {
    const creds = { privateKey: s.privateKey, tokenId: s.tokenId, tokenSecret: s.tokenSecret };
    for (let attempt = 1; attempt <= ORDER_MAX; attempt++) {
      orderResult = await placeLiveOrder(signal.pair, signal.direction, s.positionSize, s.maxContractPrice, creds);
      if (orderResult.success) {
        console.log(`[order] Live ✓ attempt=${attempt} id=${orderResult.order_id}`);
        break;
      }
      const errBody = String(orderResult.api_response || orderResult.error || '');
      console.warn(`[order] Attempt ${attempt}/${ORDER_MAX} failed: ${orderResult.error}`);
      // Permanent failure — retrying won't help
      if (errBody.toLowerCase().includes('insufficient collateral')) {
        console.error('[order] Insufficient collateral — aborting retries');
        break;
      }
      if (attempt < ORDER_MAX) await new Promise(r => setTimeout(r, ORDER_WAIT));
    }
    if (!orderResult.success) console.error(`[order] FAILED after ${ORDER_MAX} attempts`);
  } else {
    orderResult = placeShadowOrder(signal.pair, signal.direction, s.positionSize, s.maxContractPrice);
    console.log(`[order] Shadow ✓ contracts=${orderResult.contracts}`);
  }

  state.pendingSignal = { ...state.pendingSignal, orderResult };
  state.activeSignal  = state.pendingSignal;
  broadcast('signal_order', { id: signal.id, orderResult });
  scheduleNextOpen();
}

// ══════════════════════════════════════════════════════════════════
// SCHEDULER  (mirrors scheduler.py)
//   second=0  → RESOLVE  — close previous candle
//   second=1  → GENERATE — open new signal
// Both fire at the same :00/:15/:30/:45 UTC boundary.
// ══════════════════════════════════════════════════════════════════
function scheduleNextOpen(){
  const now      = Date.now();
  const next     = nextCandleStart(now);
  const msToNext = next - now;
  console.log(`[scheduler] Next boundary in ${Math.round(msToNext/1000)}s → ${new Date(next).toISOString()}`);

  // RESOLVE at second=0 — right on the candle boundary
  setTimeout(async () => {
    console.log(`\n${'═'.repeat(56)}`);
    console.log(`[candle] BOUNDARY ${new Date(candleStartOf()).toISOString()}`);
    console.log(`${'═'.repeat(56)}`);

    // Stale-signal cleanup (from scheduler.py lines 212-225):
    // Any PENDING signal >30 min old is force-resolved as UNKNOWN
    // so it never permanently blocks the duplicate guard.
    if (state.pendingSignal) {
      const ageMs = Date.now() - state.pendingSignal.timestamp;
      if (ageMs > 30 * 60 * 1000) {
        console.warn(`[resolve] Stale PENDING id=${state.pendingSignal.id} (${Math.round(ageMs/60000)}m old) → UNKNOWN`);
        const stale = { ...state.pendingSignal, result: 'UNKNOWN', settledAt: Date.now() };
        state.signals.unshift(stale);
        await saveSignal(stale);
        state.pendingSignal = null;
        state.activeSignal  = null;
        broadcast('signal_settled', stale);
      }
    }

    await settlePending();
  }, msToNext); // exactly at boundary (second=0)

  // GENERATE at second=1 — 1s after boundary
  setTimeout(async () => {
    await fireSignal();
  }, msToNext + 1000);
}

function scheduleDailyReset(){
  const next=new Date();next.setUTCHours(24,0,0,0);
  setTimeout(()=>{
    state.stats={wins:0,losses:0,total:0};
    console.log("[engine] Daily reset 00:00 UTC");
    broadcast("stats_reset",state.stats);
    scheduleDailyReset();
  },next-Date.now()+500);
}

// ══════════════════════════════════════════════════════════════════
// LIMITLESS API
// ══════════════════════════════════════════════════════════════════
function resolveCreds(body={}){
  const c=body.credentials||{};
  return{
    privateKey:(c.privateKey||state.settings.privateKey||"").trim(),
    tokenId:(c.tokenId||state.settings.tokenId||"").trim(),
    tokenSecret:(c.tokenSecret||state.settings.tokenSecret||"").trim(),
  };
}

function buildHmacHeaders(method,apiPath,body="",creds){
  const{tokenId,tokenSecret}=creds;
  if(!tokenId||!tokenSecret)throw new Error("TOKEN_ID and TOKEN_SECRET required");
  const ts=new Date().toISOString().replace(/(\.\d{3})\d*Z/,"$1Z");
  const sig=crypto.createHmac("sha256",Buffer.from(tokenSecret,"base64")).update(`${ts}\n${method}\n${apiPath}\n${body}`,"utf8").digest("base64");
  return{"lmts-api-key":tokenId,"lmts-timestamp":ts,"lmts-signature":sig,"Content-Type":"application/json"};
}

const slugCache={},marketCache={};
let ownerIdCache=null,feeRateCache=null;

async function discoverSlug(symbol){
  if(slugCache[symbol])return slugCache[symbol];
  const ticker=symbol.replace("-USDT","").toUpperCase();
  try{
    const r=await fetch(`${API_BASE}/markets/active/slugs`,{signal:AbortSignal.timeout(10000)});
    if(!r.ok)return null;
    const entries=await r.json();const matches=[];
    const check=(slug,deadline)=>{if((slug+" ").match(/15.?min/i))matches.push([deadline||"",slug]);};
    for(const e of entries){
      const et=(e.ticker||"").toUpperCase().replace("-USDT","");
      const children=e.markets||[];
      if(et===ticker){
        if(children.length)children.forEach(ch=>check((ch.slug||"").toLowerCase(),ch.deadline||e.deadline));
        else check((e.slug||"").toLowerCase(),e.deadline);
      }else for(const ch of children){if((ch.ticker||"").toUpperCase().replace("-USDT",""===ticker))check((ch.slug||"").toLowerCase(),ch.deadline||e.deadline);}
    }
    if(!matches.length)return null;
    matches.sort((a,b)=>b[0]>a[0]?1:-1);
    slugCache[symbol]=matches[0][1];return slugCache[symbol];
  }catch(e){console.error("discoverSlug:",e.message);return null;}
}

async function fetchMarket(slug){
  if(marketCache[slug]?._m)return marketCache[slug];
  let m={};
  try{const r=await fetch(`${API_BASE}/markets/${slug}`,{signal:AbortSignal.timeout(10000)});if(r.ok)m=await r.json()||{};}catch{}
  try{
    const r2=await fetch(`${API_BASE}/markets/${slug}/orderbook`,{signal:AbortSignal.timeout(10000)});
    if(r2.ok){
      const ob=await r2.json()||{};
      const venue=typeof m.venue==="object"?m.venue:{};
      const exchangeAddr=venue?.exchange||m.exchange||null;
      const bt=m.tokens||{};
      let yes=null,no=null;
      if(Array.isArray(bt)){yes=bt.find(t=>["yes","up","1"].includes(String(t?.outcome||"").toLowerCase()))?.tokenId||null;no=bt.find(t=>["no","down","0"].includes(String(t?.outcome||"").toLowerCase()))?.tokenId||null;}
      else{yes=bt.yes||bt.Yes||null;no=bt.no||bt.No||null;}
      yes=yes||ob.tokenId||ob.yesTokenId||null;no=no||ob.noTokenId||null;
      let posIds=m.positionIds||[];if(!posIds.length&&yes)posIds=[yes,...(no?[no]:[])];
      Object.assign(m,{slug,exchange:exchangeAddr,venue:{exchange:exchangeAddr},tokens:{yes,no},positionIds:posIds,conditionId:m.conditionId||m.condition_id||null,_m:true});
    }
  }catch{}
  if(!Object.keys(m).length)return null;
  m.slug=m.slug||slug;marketCache[slug]=m;return m;
}

async function getOwnerAndFee(makerAddr,creds){
  if(ownerIdCache!==null)return{ownerId:ownerIdCache,feeRateBps:feeRateCache};
  try{
    const p=`/profiles/${makerAddr}`;
    const res=await fetch(`${API_BASE}${p}`,{headers:buildHmacHeaders("GET",p,"",creds),signal:AbortSignal.timeout(10000)});
    if(res.ok){const d=await res.json();ownerIdCache=d.id!=null?Number(d.id):null;feeRateCache=d.rank?.feeRateBps!=null?Number(d.rank.feeRateBps):0;}
  }catch(e){console.error("getOwnerAndFee:",e.message);}
  return{ownerId:ownerIdCache,feeRateBps:feeRateCache};
}

const ORDER_TYPES={Order:[
  {name:"salt",type:"uint256"},{name:"maker",type:"address"},{name:"signer",type:"address"},
  {name:"taker",type:"address"},{name:"tokenId",type:"uint256"},{name:"makerAmount",type:"uint256"},
  {name:"takerAmount",type:"uint256"},{name:"expiration",type:"uint256"},{name:"nonce",type:"uint256"},
  {name:"feeRateBps",type:"uint256"},{name:"side",type:"uint8"},{name:"signatureType",type:"uint8"},
]};

async function signOrder(order,exchangeAddr,pk){
  const wallet=new ethers.Wallet(pk);
  const domain={name:"Limitless CTF Exchange",version:"1",chainId:CHAIN_ID,verifyingContract:ethers.getAddress(exchangeAddr)};
  const vals={salt:BigInt(order.salt),maker:ethers.getAddress(order.maker),signer:ethers.getAddress(order.signer),taker:ethers.getAddress(order.taker),tokenId:BigInt(order.tokenId),makerAmount:BigInt(order.makerAmount),takerAmount:BigInt(order.takerAmount),expiration:BigInt(order.expiration),nonce:BigInt(order.nonce),feeRateBps:BigInt(order.feeRateBps),side:Number(order.side),signatureType:Number(order.signatureType)};
  return wallet.signTypedData(domain,ORDER_TYPES,vals);
}

async function placeLiveOrder(symbol,direction,sizeUsd,maxPrice,creds){
  const{privateKey}=creds;
  if(!privateKey)return{success:false,error:"LIMITLESS_PRIVATE_KEY not set"};
  let makerAddr;try{makerAddr=new ethers.Wallet(privateKey).address;}catch(e){return{success:false,error:`Invalid key: ${e.message}`};}
  delete slugCache[symbol];const slug=await discoverSlug(symbol);
  if(!slug)return{success:false,error:`No active 15-min market for ${symbol}`};
  delete marketCache[slug];const market=await fetchMarket(slug);
  if(!market)return{success:false,error:`Cannot fetch market slug=${slug}`};
  const exchangeAddr=market.venue?.exchange||market.exchange;
  if(!exchangeAddr)return{success:false,error:"venue.exchange missing"};
  const tokens=market.tokens||{};
  const tokenId=direction==="UP"?(tokens.yes||market.positionIds?.[0]):(tokens.no||market.positionIds?.[1]);
  if(!tokenId)return{success:false,error:`Token ID missing direction=${direction}`};
  const{ownerId,feeRateBps}=await getOwnerAndFee(makerAddr,creds);
  if(ownerId===null)return{success:false,error:`Cannot resolve ownerId`};
  const price=Math.round(Math.min(maxPrice,0.99)*100)/100;
  const size=Math.round((sizeUsd/price)*10000)/10000;
  const makerAmount=Math.round(price*size*1_000_000);
  const takerAmount=Math.round(size*1_000_000);
  const salt=BigInt(Date.now());
  const order={salt:salt.toString(),maker:makerAddr,signer:makerAddr,taker:ZERO_ADDR,tokenId:String(tokenId),makerAmount,takerAmount,expiration:"0",nonce:0,feeRateBps,side:0,signatureType:0};
  let signature;try{signature=await signOrder(order,exchangeAddr,privateKey);}catch(e){return{success:false,error:`EIP-712: ${e.message}`};}
  const payload={order:{...order,signature,signatureType:0,price},orderType:"GTC",marketSlug:slug,ownerId};
  const bodyStr=JSON.stringify(payload);
  let headers;try{headers=buildHmacHeaders("POST","/orders",bodyStr,creds);}catch(e){return{success:false,error:e.message};}
  try{
    const res=await fetch(`${API_BASE}/orders`,{method:"POST",headers,body:bodyStr,signal:AbortSignal.timeout(15000)});
    const text=await res.text();
    if(!res.ok){let d;try{d=JSON.parse(text);}catch{d=text;}return{success:false,http_status:res.status,error:`HTTP ${res.status}`,api_response:d};}
    const result=JSON.parse(text);
    return{success:true,order_id:String(result?.order?.id||result?.id||salt),contracts:size,price_per_contract:price,total_spent:sizeUsd,slug,condition_id:market.conditionId||null,signal_direction:direction,maker:makerAddr};
  }catch(e){return{success:false,error:e.message};}
}

function placeShadowOrder(symbol,direction,sizeUsd,maxPrice){
  const price=Math.min(maxPrice,0.99);
  return{success:true,order_id:`shadow_${Date.now()}`,contracts:Math.round((sizeUsd/price)*10000)/10000,price_per_contract:price,total_spent:sizeUsd,signal_direction:direction,shadow:true};
}

// ══════════════════════════════════════════════════════════════════
// EXPRESS ROUTES
// ══════════════════════════════════════════════════════════════════
app.get("/api/ping",(_,res)=>res.json({ok:true,ts:Date.now()}));

app.get("/api/stream",(req,res)=>{
  res.setHeader("Content-Type","text/event-stream");
  res.setHeader("Cache-Control","no-cache");
  res.setHeader("Connection","keep-alive");
  res.setHeader("Access-Control-Allow-Origin","*");
  res.flushHeaders();
  res.write(`event: snapshot\ndata: ${JSON.stringify({
    prices:state.prices,signals:state.signals.slice(0,100),
    activeSignal:state.activeSignal,stats:state.stats,
    demoStats:state.demoStats,
    cooldownCandles:state.cooldownCandles,lastFamily:state.lastFamily,
    settings:{mode:state.settings.mode,positionSize:state.settings.positionSize,maxContractPrice:state.settings.maxContractPrice,demoStake:state.settings.demoStake},
  })}\n\n`);
  const hb=setInterval(()=>{try{res.write(": hb\n\n");}catch{clearInterval(hb);}},20000);
  sseClients.add(res);
  req.on("close",()=>{sseClients.delete(res);clearInterval(hb);});
});

// History — by date
app.get("/api/history/dates",async(_,res)=>{
  const dates=await getAvailableDates();
  res.json({dates});
});

app.get("/api/history/:date",async(req,res)=>{
  const{date}=req.params; // YYYY-MM-DD
  const signals=await getSignalsByDate(date);
  const wins=signals.filter(s=>s.result==="WIN").length;
  const losses=signals.filter(s=>s.result==="LOSS").length;
  res.json({date,signals,stats:{wins,losses,total:signals.length}});
});

app.get("/api/history/stats/all",async(_,res)=>{
  const rows=await getAllDailyStats();
  res.json({rows});
});

// Demo trades
app.get("/api/demo/trades",async(_,res)=>{
  const trades=await getDemoTrades(200);
  const stats=await getDemoStats();
  res.json({trades,stats});
});

// Settings
app.post("/api/settings",(req,res)=>{
  const{mode,positionSize,maxContractPrice,demoStake,privateKey,tokenId,tokenSecret}=req.body||{};
  if(mode)             state.settings.mode             =mode;
  if(positionSize)     state.settings.positionSize     =Number(positionSize);
  if(maxContractPrice) state.settings.maxContractPrice =Number(maxContractPrice);
  if(demoStake)        state.settings.demoStake        =Number(demoStake);
  if(privateKey!==undefined)  state.settings.privateKey   =privateKey;
  if(tokenId!==undefined)     state.settings.tokenId      =tokenId;
  if(tokenSecret!==undefined) state.settings.tokenSecret  =tokenSecret;
  ownerIdCache=null;feeRateCache=null;
  res.json({ok:true});
});

app.post("/api/limitless/execute",async(req,res)=>{
  try{
    const{symbol="BTC-USDT",direction="UP",mode="shadow",positionSize=10,maxContractPrice=0.50}=req.body;
    const creds=resolveCreds(req.body);
    const result=mode==="live"?await placeLiveOrder(symbol,direction,+positionSize,+maxContractPrice,creds):placeShadowOrder(symbol,direction,+positionSize,+maxContractPrice);
    res.json(result);
  }catch(e){res.status(500).json({success:false,error:e.message});}
});

app.post("/api/limitless/validate",(req,res)=>{
  try{
    const creds=resolveCreds(req.body);
    let addr=null;try{addr=new ethers.Wallet(creds.privateKey).address;}catch{}
    res.json({hmac_auth_ready:!!(creds.tokenId&&creds.tokenSecret),signing_ready:!!creds.privateKey,live_trading_ready:!!(creds.tokenId&&creds.tokenSecret&&creds.privateKey),signer_address:addr});
  }catch(e){res.status(500).json({error:e.message});}
});

app.post("/api/limitless/claim",async(req,res)=>{
  try{
    const{marketSlug,conditionId}=req.body;const creds=resolveCreds(req.body);
    const cid=conditionId||(marketSlug?(await fetchMarket(marketSlug))?.conditionId:null);
    if(!cid){res.json({success:false,error:"conditionId not found"});return;}
    const apiPath="/portfolio/redeem";const bodyStr=JSON.stringify({conditionId:String(cid)});
    const r=await fetch(`${API_BASE}${apiPath}`,{method:"POST",headers:buildHmacHeaders("POST",apiPath,bodyStr,creds),body:bodyStr,signal:AbortSignal.timeout(15000)});
    const text=await r.text();
    if(!r.ok){res.json({success:false,error:`HTTP ${r.status}`});return;}
    res.json({success:true,...(JSON.parse(text)||{})});
  }catch(e){res.status(500).json({success:false,error:e.message});}
});

// Serve React
const CLIENT_DIST=path.join(__dirname,"..","client","dist");
app.use(express.static(CLIENT_DIST));
app.get("*",(_,res)=>res.sendFile(path.join(CLIENT_DIST,"index.html")));

// ══════════════════════════════════════════════════════════════════
// BOOT
// ══════════════════════════════════════════════════════════════════
app.listen(PORT,async()=>{
  console.log(`✅ Limitless Oracle · port ${PORT}`);
  console.log(`   Mode: ${state.settings.mode} | $${state.settings.positionSize} live | $${state.settings.demoStake} demo`);
  await initDB();
  // Load recent signals from DB into memory
  const recent=await getRecentSignals(200);
  if(recent.length){state.signals=recent;console.log(`[db] Loaded ${recent.length} signals from DB`);}
  // Load today's demo stats
  state.demoStats=await getDemoStats();
  await refreshMarketData();
  console.log(`[init] ${Object.keys(state.prices).length} pairs loaded`);
  scheduleNextOpen();
  setInterval(refreshMarketData,30000);
  scheduleDailyReset();
  const ms=nextCandleStart()-Date.now();
  console.log(`[scheduler] First signal in ${Math.round(ms/1000)}s → ${new Date(nextCandleStart()).toISOString()}`);
});
