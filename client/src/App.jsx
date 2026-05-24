import { useState, useEffect, useRef, useCallback } from "react";

/* ═══════════════════════════════════════════════════════════════
   GLOBAL STYLES
═══════════════════════════════════════════════════════════════ */
const GLOBAL_CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Barlow+Condensed:wght@300;500;700;800;900&family=Barlow:wght@300;400;500&display=swap');
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#07080a;--bg1:#0c0e11;--bg2:#111317;--bg3:#181b20;
    --border:#1e2228;--border2:#272c34;
    --gold:#c9a84c;--gold2:#e8c96a;--gold-dim:#c9a84c28;
    --green:#3ddc84;--green-dim:#3ddc8420;
    --red:#ff4757;--red-dim:#ff475720;
    --blue:#4a9eff;--blue-dim:#4a9eff18;
    --text:#d4d8e0;--text2:#8892a0;--text3:#4a5260;
    --mono:'Space Mono',monospace;
    --display:'Barlow Condensed',sans-serif;
    --body:'Barlow',sans-serif;
  }
  html,body,#root{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--body);overflow-x:hidden}
  ::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
  @keyframes pulse-ring{0%{box-shadow:0 0 0 0 rgba(201,168,76,.8)}100%{box-shadow:0 0 0 12px transparent}}
  @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes ticker{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
  @keyframes glow{0%,100%{opacity:.5}50%{opacity:1}}
  .fade-in{animation:fadeUp .35s ease forwards}
  input[type=range]{-webkit-appearance:none;width:100%;height:2px;background:var(--border2);border-radius:1px;outline:none}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--gold);cursor:pointer;border:2px solid var(--bg);box-shadow:0 0 8px var(--gold)}
  input[type=password],input[type=text]{background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;padding:10px 12px;border-radius:4px;width:100%;outline:none;transition:border-color .2s}
  input[type=password]:focus,input[type=text]:focus{border-color:var(--gold)}
  button{cursor:pointer;font-family:var(--display)}
`;

function injectStyles(){
  if(document.getElementById("os"))return;
  const s=document.createElement("style");s.id="os";s.textContent=GLOBAL_CSS;
  document.head.appendChild(s);
}

/* ═══════════════════════════════════════════════════════════════
   PAIR META
═══════════════════════════════════════════════════════════════ */
const PAIRS={
  "BTC-USDT":{family:0,label:"BTC",fcolor:"#c9a84c"},
  "ETH-USDT":{family:0,label:"ETH",fcolor:"#4a9eff"},
  "SOL-USDT":{family:1,label:"SOL",fcolor:"#9945FF"},
  "DOGE-USDT":{family:1,label:"DOGE",fcolor:"#C2A633"},
  "XRP-USDT":{family:2,label:"XRP",fcolor:"#00AAE4"},
  "BNB-USDT":{family:2,label:"BNB",fcolor:"#F0B90B"},
};
const FAMILY_NAMES=["BTC · ETH","SOL · DOGE","XRP · BNB"];
const CANDLE_MS=15*60*1000;

function msUntilNext(now=Date.now()){
  return (Math.floor(now/CANDLE_MS)*CANDLE_MS+CANDLE_MS)-now;
}
function fmtPrice(n,pair){
  if(n==null)return"—";
  return(pair?.includes("BTC")||pair?.includes("ETH"))?Number(n).toFixed(2):Number(n).toFixed(4);
}
function fmtT(ts){
  const d=new Date(ts);
  return`${d.getHours().toString().padStart(2,"0")}:${d.getMinutes().toString().padStart(2,"0")}`;
}

/* ═══════════════════════════════════════════════════════════════
   KEEP-ALIVE
═══════════════════════════════════════════════════════════════ */
function useKeepAlive(){
  useEffect(()=>{
    const id=setInterval(()=>fetch("/api/ping").catch(()=>{}),2000);
    return()=>clearInterval(id);
  },[]);
}

/* ═══════════════════════════════════════════════════════════════
   MAIN APP
═══════════════════════════════════════════════════════════════ */
export default function App(){
  injectStyles();
  useKeepAlive();

  // All state comes FROM the server via SSE
  const [prices,setPrices]=useState({});
  const [signals,setSignals]=useState([]);
  const [activeSignal,setActive]=useState(null);
  const [stats,setStats]=useState({wins:0,losses:0,total:0});
  const [cooldown,setCooldown]=useState(0);
  const [lastFamily,setLastFamily]=useState(null);
  const [connected,setConnected]=useState(false);
  const [tab,setTab]=useState("dashboard");
  const [toast,setToast]=useState(null);
  const [countdown,setCountdown]=useState(0);
  const [settings,setSettingsLocal]=useState({
    mode:"shadow",positionSize:10,maxContractPrice:0.50,
    privateKey:"",tokenId:"",tokenSecret:"",
  });

  const showToast=useCallback((msg,type="info")=>{
    setToast({msg,type});setTimeout(()=>setToast(null),4500);
  },[]);

  // Countdown clock
  useEffect(()=>{
    const id=setInterval(()=>setCountdown(Math.ceil(msUntilNext()/1000)),500);
    return()=>clearInterval(id);
  },[]);

  // SSE connection to server engine
  useEffect(()=>{
    let es, retryTimer;
    function connect(){
      es=new EventSource("/api/stream");
      es.addEventListener("snapshot",e=>{
        const d=JSON.parse(e.data);
        setPrices(d.prices||{});
        setSignals(d.signals||[]);
        setActive(d.activeSignal||null);
        setStats(d.stats||{wins:0,losses:0,total:0});
        setCooldown(d.cooldownCandles||0);
        setLastFamily(d.lastFamily);
        if(d.settings) setSettingsLocal(prev=>({...prev,...d.settings}));
        setConnected(true);
      });
      es.addEventListener("prices",e=>setPrices(JSON.parse(e.data)));
      es.addEventListener("signal_new",e=>{
        const sig=JSON.parse(e.data);
        setActive(sig);
        showToast(`${PAIRS[sig.pair]?.label} ${sig.direction} · ${sig.confidence}% conf`,sig.direction==="UP"?"success":"danger");
        if("Notification" in window&&Notification.permission==="granted")
          new Notification(`${PAIRS[sig.pair]?.label} ${sig.direction==="UP"?"▲":"▼"} ${sig.confidence}%`);
      });
      es.addEventListener("signal_order",e=>{
        const{id,orderResult}=JSON.parse(e.data);
        setActive(prev=>prev?.id===id?{...prev,orderResult}:prev);
      });
      es.addEventListener("signal_settled",e=>{
        const sig=JSON.parse(e.data);
        setSignals(prev=>[sig,...prev].slice(0,200));
        setActive(null);
        showToast(`${PAIRS[sig.pair]?.label} ${sig.result}`,sig.result==="WIN"?"success":"danger");
      });
      es.addEventListener("stats",e=>setStats(JSON.parse(e.data)));
      es.addEventListener("stats_reset",e=>setStats(JSON.parse(e.data)));
      es.addEventListener("cooldown",e=>setCooldown(JSON.parse(e.data).remaining||0));
      es.addEventListener("candle_open",()=>{});
      es.addEventListener("scan",()=>{});
      es.onerror=()=>{
        setConnected(false);es.close();
        retryTimer=setTimeout(connect,3000);
      };
    }
    connect();
    if("Notification" in window&&Notification.permission==="default") Notification.requestPermission();
    return()=>{es?.close();clearTimeout(retryTimer);};
  },[showToast]);

  const saveSettings=useCallback(async(newSettings)=>{
    setSettingsLocal(newSettings);
    try{
      await fetch("/api/settings",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify(newSettings),
      });
    }catch(e){console.error("settings save failed",e);}
  },[]);

  const prog=((CANDLE_MS-msUntilNext())/CANDLE_MS)*100;
  const wr=stats.total>0?((stats.wins/stats.total)*100).toFixed(1):"—";
  const todayStart=new Date();todayStart.setHours(0,0,0,0);
  const todaySignals=signals.filter(s=>s.timestamp>=todayStart.getTime());

  return(
    <div style={{minHeight:"100vh",background:"var(--bg)",position:"relative",overflow:"hidden"}}>
      <div style={{position:"fixed",inset:0,pointerEvents:"none",zIndex:0,
        backgroundImage:"repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.04) 2px,rgba(0,0,0,0.04) 4px)"}}/>
      <div style={{position:"fixed",top:-300,left:"50%",transform:"translateX(-50%)",
        width:900,height:500,borderRadius:"50%",pointerEvents:"none",zIndex:0,
        background:"radial-gradient(ellipse,rgba(201,168,76,0.05) 0%,transparent 70%)"}}/>
      {toast&&<Toast msg={toast.msg} type={toast.type}/>}
      <Header tab={tab} setTab={setTab} mode={settings.mode} countdown={countdown}
        prog={prog} connected={connected}/>
      <TickerTape prices={prices}/>
      <div style={{maxWidth:1440,margin:"0 auto",padding:"0 20px 60px",position:"relative",zIndex:1}}>
        {tab==="dashboard"&&<Dashboard stats={stats} wr={wr} todaySignals={todaySignals}
          lastFamily={lastFamily} cooldown={cooldown} activeSignal={activeSignal}
          prices={prices} signals={signals}/>}
        {tab==="history"&&<History signals={signals} todaySignals={todaySignals}/>}
        {tab==="settings"&&<Settings settings={settings} setSettings={saveSettings}/>}
      </div>
    </div>
  );
}

/* ═══ HEADER ════════════════════════════════════════════════════ */
function Header({tab,setTab,mode,countdown,prog,connected}){
  return(
    <header style={{borderBottom:"1px solid var(--border)",background:"rgba(7,8,10,0.96)",
      backdropFilter:"blur(20px)",position:"sticky",top:0,zIndex:100}}>
      <div style={{maxWidth:1440,margin:"0 auto",padding:"0 20px",
        display:"flex",alignItems:"center",gap:20,height:56}}>
        <div style={{display:"flex",alignItems:"center",gap:12,flex:"none"}}>
          <div style={{width:34,height:34,borderRadius:5,background:"var(--gold)",display:"flex",
            alignItems:"center",justifyContent:"center",fontSize:18,fontWeight:700,color:"#000",fontFamily:"var(--mono)"}}>∞</div>
          <div>
            <div style={{fontFamily:"var(--display)",fontSize:16,fontWeight:900,letterSpacing:4,color:"var(--text)",lineHeight:1}}>ORACLE</div>
            <div style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--text3)",letterSpacing:2,marginTop:2}}>LIMITLESS · 15M SIGNALS</div>
          </div>
        </div>
        <div style={{flex:1,display:"flex",alignItems:"center",gap:10}}>
          <div style={{flex:1,height:2,background:"var(--border)",borderRadius:1,overflow:"hidden"}}>
            <div style={{height:"100%",width:`${prog}%`,borderRadius:1,
              background:"linear-gradient(90deg,var(--gold),var(--gold2))",transition:"width .5s linear"}}/>
          </div>
          <span style={{fontFamily:"var(--mono)",fontSize:11,color:"var(--gold)",letterSpacing:1,flex:"none",minWidth:42}}>
            {String(Math.floor(countdown/60)).padStart(2,"0")}:{String(countdown%60).padStart(2,"0")}
          </span>
        </div>
        <div style={{display:"flex",alignItems:"center",gap:6,padding:"5px 12px",
          border:`1px solid ${connected?"#3ddc8440":mode==="live"?"#3ddc84":"var(--border2)"}`,borderRadius:3,flex:"none"}}>
          <div style={{width:6,height:6,borderRadius:"50%",flexShrink:0,
            background:connected?mode==="live"?"var(--green)":"var(--gold)":"var(--red)",
            animation:connected?"glow 2s infinite":"none"}}/>
          <span style={{fontFamily:"var(--mono)",fontSize:10,fontWeight:700,letterSpacing:2,
            color:connected?mode==="live"?"var(--green)":"var(--gold)":"var(--red)"}}>
            {connected?(mode==="live"?"LIVE":"SHADOW"):"RECONNECTING"}
          </span>
        </div>
        <nav style={{display:"flex",gap:2}}>
          {["dashboard","history","settings"].map(t=>(
            <button key={t} onClick={()=>setTab(t)} style={{
              padding:"6px 16px",borderRadius:4,textTransform:"uppercase",
              background:tab===t?"var(--bg3)":"transparent",
              border:`1px solid ${tab===t?"var(--border2)":"transparent"}`,
              color:tab===t?"var(--gold)":"var(--text3)",
              fontFamily:"var(--display)",fontSize:12,fontWeight:700,letterSpacing:2,transition:"all .15s",
            }}>{t}</button>
          ))}
        </nav>
      </div>
    </header>
  );
}

/* ═══ TICKER TAPE ═══════════════════════════════════════════════ */
function TickerTape({prices}){
  const items=Object.entries(PAIRS).map(([pair,meta])=>{
    const t=prices[pair];const chg=t?((t.last-t.open24h)/t.open24h*100):null;
    return{pair,meta,t,chg};
  });
  const doubled=[...items,...items];
  return(
    <div style={{borderBottom:"1px solid var(--border)",background:"var(--bg1)",overflow:"hidden",height:32,display:"flex",alignItems:"center"}}>
      <div style={{display:"flex",animation:"ticker 28s linear infinite",whiteSpace:"nowrap"}}>
        {doubled.map((item,i)=>(
          <div key={i} style={{display:"flex",alignItems:"center",gap:8,padding:"0 24px",borderRight:"1px solid var(--border)"}}>
            <span style={{fontFamily:"var(--display)",fontSize:12,fontWeight:700,letterSpacing:1,color:item.meta.fcolor}}>{item.meta.label}</span>
            <span style={{fontFamily:"var(--mono)",fontSize:11,color:"var(--text)"}}>
              {item.t?fmtPrice(item.t.last,item.pair):"—"}
            </span>
            {item.chg!=null&&(
              <span style={{fontFamily:"var(--mono)",fontSize:10,color:item.chg>=0?"var(--green)":"var(--red)"}}>
                {item.chg>=0?"+":""}{item.chg.toFixed(2)}%
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ═══ DASHBOARD ═════════════════════════════════════════════════ */
function Dashboard({stats,wr,todaySignals,lastFamily,cooldown,activeSignal,prices,signals}){
  return(
    <div style={{paddingTop:24}}>
      <div style={{display:"grid",gridTemplateColumns:"repeat(6,1fr)",gap:1,
        border:"1px solid var(--border)",borderRadius:6,overflow:"hidden",marginBottom:20}}>
        {[
          {label:"WIN RATE",value:`${wr}%`,color:"var(--gold)"},
          {label:"WINS",value:stats.wins,color:"var(--green)"},
          {label:"LOSSES",value:stats.losses,color:"var(--red)"},
          {label:"TOTAL TODAY",value:todaySignals.length,color:"var(--text)"},
          {label:"FAMILY LOCK",value:lastFamily!==null?FAMILY_NAMES[lastFamily]:"—",color:"var(--blue)"},
          {label:"STATUS",value:cooldown>0?`COOLDOWN ${cooldown}`:"SCANNING",color:cooldown>0?"var(--red)":"var(--green)"},
        ].map((s,i)=>(
          <div key={i} style={{background:"var(--bg1)",padding:"14px 16px",borderRight:i<5?"1px solid var(--border)":"none"}}>
            <div style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--text3)",letterSpacing:2,marginBottom:6}}>{s.label}</div>
            <div style={{fontFamily:"var(--display)",fontSize:26,fontWeight:800,color:s.color,lineHeight:1}}>{s.value}</div>
          </div>
        ))}
      </div>
      <div style={{display:"grid",gridTemplateColumns:"1fr 360px",gap:20,alignItems:"start"}}>
        <div style={{display:"flex",flexDirection:"column",gap:20}}>
          {activeSignal?<ActiveCard signal={activeSignal} prices={prices}/>:<ScanningCard/>}
          <PriceGrid prices={prices}/>
        </div>
        <div style={{background:"var(--bg1)",border:"1px solid var(--border)",borderRadius:6,
          overflow:"hidden",position:"sticky",top:76}}>
          <div style={{padding:"12px 16px",borderBottom:"1px solid var(--border)",
            display:"flex",justifyContent:"space-between",alignItems:"center"}}>
            <span style={{fontFamily:"var(--mono)",fontSize:9,fontWeight:700,letterSpacing:3,color:"var(--text3)"}}>SIGNAL LOG</span>
            <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text3)"}}>{signals.length}</span>
          </div>
          <div style={{maxHeight:"calc(100vh - 220px)",overflowY:"auto"}}>
            {signals.length===0
              ?<div style={{padding:"40px 20px",textAlign:"center",fontFamily:"var(--mono)",fontSize:11,color:"var(--text3)"}}>Waiting for first signal…</div>
              :signals.slice(0,60).map((s,i)=><LogRow key={s.id} signal={s} isNew={i===0}/>)
            }
          </div>
        </div>
      </div>
    </div>
  );
}

/* ═══ ACTIVE CARD ═══════════════════════════════════════════════ */
function ActiveCard({signal,prices}){
  const meta=PAIRS[signal.pair];
  const current=prices[signal.pair]?.last;
  const isWinning=current!=null&&((signal.direction==="UP"&&current>signal.openPrice)||(signal.direction==="DOWN"&&current<signal.openPrice));
  const pnlPct=current!=null?((current-signal.openPrice)/signal.openPrice*100):null;
  const isUp=signal.direction==="UP";
  return(
    <div className="fade-in" style={{background:"var(--bg1)",border:`1px solid ${isUp?"var(--green)":"var(--red)"}`,borderRadius:6,overflow:"hidden"}}>
      <div style={{height:3,background:isUp?"linear-gradient(90deg,var(--green),transparent)":"linear-gradient(90deg,var(--red),transparent)"}}/>
      <div style={{padding:20}}>
        <div style={{display:"flex",alignItems:"flex-start",justifyContent:"space-between",marginBottom:20}}>
          <div>
            <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:6}}>
              <div style={{width:8,height:8,borderRadius:"50%",background:meta.fcolor,animation:"pulse-ring 1.5s ease-out infinite"}}/>
              <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--text3)",letterSpacing:3}}>ACTIVE SIGNAL</span>
            </div>
            <div style={{fontFamily:"var(--display)",fontSize:52,fontWeight:900,color:meta.fcolor,lineHeight:1,letterSpacing:-2}}>{meta.label}</div>
            <div style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text3)",marginTop:6}}>
              {FAMILY_NAMES[signal.family]} · {signal.marketCondition} · {fmtT(signal.timestamp)}
            </div>
          </div>
          <div style={{textAlign:"right"}}>
            <div style={{display:"inline-flex",alignItems:"center",gap:10,padding:"14px 22px",borderRadius:4,marginBottom:10,
              background:isUp?"var(--green-dim)":"var(--red-dim)",border:`1px solid ${isUp?"var(--green)":"var(--red)"}`}}>
              <span style={{fontSize:24}}>{isUp?"▲":"▼"}</span>
              <span style={{fontFamily:"var(--display)",fontSize:28,fontWeight:900,color:isUp?"var(--green)":"var(--red)",letterSpacing:3}}>
                {isUp?"LONG":"SHORT"}
              </span>
            </div>
            <div style={{fontFamily:"var(--display)",fontSize:42,fontWeight:900,color:"var(--gold)",lineHeight:1}}>{signal.confidence}%</div>
            <div style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--text3)",letterSpacing:2,marginTop:2}}>CONFIDENCE</div>
          </div>
        </div>
        <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:1,background:"var(--border)",borderRadius:4,overflow:"hidden",marginBottom:14}}>
          {[
            {label:"OPEN",value:fmtPrice(signal.openPrice,signal.pair),color:"var(--text)"},
            {label:"CURRENT",value:fmtPrice(current,signal.pair),color:current==null?"var(--text3)":isWinning?"var(--green)":"var(--red)"},
            {label:"P&L",value:pnlPct!=null?`${pnlPct>=0?"+":""}${pnlPct.toFixed(3)}%`:"—",color:pnlPct==null?"var(--text3)":isWinning?"var(--green)":"var(--red)"},
            {label:"RSI",value:signal.rsi||"—",color:signal.rsi>70?"var(--red)":signal.rsi<30?"var(--green)":"var(--gold)"},
          ].map((item,i)=>(
            <div key={i} style={{background:"var(--bg)",padding:"10px 14px"}}>
              <div style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--text3)",letterSpacing:2,marginBottom:4}}>{item.label}</div>
              <div style={{fontFamily:"var(--mono)",fontSize:14,fontWeight:700,color:item.color}}>{item.value}</div>
            </div>
          ))}
        </div>
        {signal.orderResult&&(
          <div style={{padding:"8px 12px",borderRadius:3,fontFamily:"var(--mono)",fontSize:10,
            background:signal.orderResult.success?"var(--green-dim)":"var(--red-dim)",
            border:`1px solid ${signal.orderResult.success?"#3ddc8440":"#ff475740"}`,
            color:signal.orderResult.success?"var(--green)":"var(--red)"}}>
            {signal.orderResult.success
              ?`✓ Order ${signal.orderResult.shadow?"simulated":"placed"} · ${signal.orderResult.contracts} contracts @ $${signal.orderResult.price_per_contract}`
              :`✗ ${signal.orderResult.error}`}
          </div>
        )}
      </div>
    </div>
  );
}

function ScanningCard(){
  return(
    <div style={{background:"var(--bg1)",border:"1px solid var(--border)",borderRadius:6,padding:"60px 40px",textAlign:"center"}}>
      <div style={{width:44,height:44,borderRadius:"50%",border:"2px solid var(--border2)",
        borderTopColor:"var(--gold)",margin:"0 auto 20px",animation:"spin 1s linear infinite"}}/>
      <div style={{fontFamily:"var(--display)",fontSize:18,fontWeight:700,letterSpacing:4,color:"var(--text3)",marginBottom:8}}>SERVER ENGINE RUNNING</div>
      <div style={{fontFamily:"var(--mono)",fontSize:11,color:"var(--text3)"}}>Signal engine active on server · Waiting for next high-confidence setup</div>
    </div>
  );
}

/* ═══ PRICE GRID ════════════════════════════════════════════════ */
function PriceGrid({prices}){
  return(
    <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:1,background:"var(--border)",border:"1px solid var(--border)",borderRadius:6,overflow:"hidden"}}>
      {Object.entries(PAIRS).map(([pair,meta])=>{
        const t=prices[pair];const chg=t?((t.last-t.open24h)/t.open24h*100):null;
        return(
          <div key={pair} style={{background:"var(--bg1)",padding:16,position:"relative"}}>
            <div style={{position:"absolute",left:0,top:0,bottom:0,width:2,
              background:chg==null?"var(--border)":chg>=0?"var(--green)":"var(--red)"}}/>
            <div style={{paddingLeft:10}}>
              <div style={{fontFamily:"var(--display)",fontSize:20,fontWeight:800,color:meta.fcolor,letterSpacing:1,marginBottom:8}}>{meta.label}</div>
              <div style={{fontFamily:"var(--mono)",fontSize:17,fontWeight:700,color:"var(--text)",marginBottom:4}}>
                {t?fmtPrice(t.last,pair):"—"}
              </div>
              <span style={{fontFamily:"var(--mono)",fontSize:10,color:chg==null?"var(--text3)":chg>=0?"var(--green)":"var(--red)"}}>
                {chg!=null?`${chg>=0?"+":""}${chg.toFixed(2)}%`:"Loading…"}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ═══ LOG ROW ═══════════════════════════════════════════════════ */
function LogRow({signal,isNew}){
  const meta=PAIRS[signal.pair];
  const isWin=signal.result==="WIN",isLoss=signal.result==="LOSS";
  return(
    <div className={isNew?"fade-in":""} style={{display:"flex",alignItems:"center",gap:10,padding:"9px 16px",
      borderBottom:"1px solid var(--border)",
      borderLeft:`2px solid ${isWin?"var(--green)":isLoss?"var(--red)":"var(--border)"}`,
      background:isNew?"rgba(201,168,76,0.04)":"transparent"}}>
      <span style={{fontFamily:"var(--display)",fontSize:13,fontWeight:700,color:meta.fcolor,minWidth:38}}>{meta.label}</span>
      <span style={{fontFamily:"var(--mono)",fontSize:10,color:signal.direction==="UP"?"var(--green)":"var(--red)",minWidth:10}}>{signal.direction==="UP"?"▲":"▼"}</span>
      <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--text3)",minWidth:36}}>{fmtT(signal.timestamp)}</span>
      <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--text3)",flex:1,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>
        {fmtPrice(signal.openPrice,signal.pair)}→{fmtPrice(signal.closePrice,signal.pair)}
      </span>
      <span style={{fontFamily:"var(--mono)",fontSize:10,fontWeight:700,
        color:isWin?"var(--green)":isLoss?"var(--red)":"var(--text3)",minWidth:28}}>
        {isWin?"WIN":isLoss?"LOSS":"…"}
      </span>
    </div>
  );
}

/* ═══ HISTORY ═══════════════════════════════════════════════════ */
function History({signals,todaySignals}){
  const wins=todaySignals.filter(s=>s.result==="WIN").length;
  const losses=todaySignals.filter(s=>s.result==="LOSS").length;
  const wr=todaySignals.length>0?((wins/todaySignals.length)*100).toFixed(1):"—";
  return(
    <div style={{paddingTop:24}}>
      <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:1,
        border:"1px solid var(--border)",borderRadius:6,overflow:"hidden",marginBottom:20}}>
        {[
          {label:"SIGNALS TODAY",value:todaySignals.length,color:"var(--text)"},
          {label:"WINS",value:wins,color:"var(--green)"},
          {label:"LOSSES",value:losses,color:"var(--red)"},
          {label:"WIN RATE",value:`${wr}%`,color:"var(--gold)"},
        ].map((s,i)=>(
          <div key={i} style={{background:"var(--bg1)",padding:"16px 20px",borderRight:i<3?"1px solid var(--border)":"none"}}>
            <div style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--text3)",letterSpacing:2,marginBottom:6}}>{s.label}</div>
            <div style={{fontFamily:"var(--display)",fontSize:34,fontWeight:800,color:s.color,lineHeight:1}}>{s.value}</div>
          </div>
        ))}
      </div>
      <div style={{border:"1px solid var(--border)",borderRadius:6,overflow:"hidden"}}>
        <div style={{display:"grid",gridTemplateColumns:"60px 36px 56px 64px 110px 110px 90px 70px",
          padding:"10px 16px",borderBottom:"1px solid var(--border)",background:"var(--bg1)"}}>
          {["PAIR","DIR","TIME","CONF","OPEN","CLOSE","CONDITION","RESULT"].map(h=>(
            <span key={h} style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--text3)",letterSpacing:1}}>{h}</span>
          ))}
        </div>
        <div style={{maxHeight:"65vh",overflowY:"auto"}}>
          {signals.length===0
            ?<div style={{padding:"60px",textAlign:"center",fontFamily:"var(--mono)",fontSize:11,color:"var(--text3)"}}>No signal history yet</div>
            :signals.map((s,i)=>{
              const meta=PAIRS[s.pair];const isWin=s.result==="WIN",isLoss=s.result==="LOSS";
              return(
                <div key={s.id} style={{display:"grid",gridTemplateColumns:"60px 36px 56px 64px 110px 110px 90px 70px",
                  padding:"10px 16px",borderBottom:"1px solid var(--border)",alignItems:"center",
                  background:i%2===0?"var(--bg1)":"var(--bg)",
                  borderLeft:`2px solid ${isWin?"var(--green)":isLoss?"var(--red)":"var(--border)"}`}}>
                  <span style={{fontFamily:"var(--display)",fontSize:13,fontWeight:700,color:meta.fcolor}}>{meta.label}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:11,color:s.direction==="UP"?"var(--green)":"var(--red)"}}>{s.direction==="UP"?"▲":"▼"}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text2)"}}>{fmtT(s.timestamp)}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--gold)"}}>{s.confidence}%</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text2)"}}>{fmtPrice(s.openPrice,s.pair)}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text2)"}}>{fmtPrice(s.closePrice,s.pair)}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--text3)"}}>{s.marketCondition}</span>
                  <span style={{fontFamily:"var(--display)",fontSize:14,fontWeight:800,
                    color:isWin?"var(--green)":isLoss?"var(--red)":"var(--text3)"}}>
                    {isWin?"WIN":isLoss?"LOSS":"PEND"}
                  </span>
                </div>
              );
            })
          }
        </div>
      </div>
    </div>
  );
}

/* ═══ SETTINGS ══════════════════════════════════════════════════ */
function Settings({settings,setSettings}){
  const upd=k=>v=>setSettings({...settings,[k]:v});
  const updAll=patch=>setSettings({...settings,...patch});
  const [validated,setValidated]=useState(null);
  const [validating,setValidating]=useState(false);
  const validate=async()=>{
    setValidating(true);
    try{
      const r=await fetch("/api/limitless/validate",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({credentials:{privateKey:settings.privateKey,tokenId:settings.tokenId,tokenSecret:settings.tokenSecret}}),
      });
      setValidated(await r.json());
    }catch(e){setValidated({error:e.message});}
    setValidating(false);
  };
  return(
    <div style={{paddingTop:24,maxWidth:700}}>
      <div style={{fontFamily:"var(--mono)",fontSize:9,fontWeight:700,letterSpacing:4,color:"var(--text3)",marginBottom:20}}>SYSTEM SETTINGS</div>
      <Sect title="TRADING MODE">
        <div style={{display:"flex",gap:8,marginBottom:12}}>
          {["shadow","live"].map(m=>(
            <button key={m} onClick={()=>updAll({mode:m})} style={{flex:1,padding:"14px",borderRadius:4,transition:"all .2s",
              background:settings.mode===m?m==="live"?"var(--green-dim)":"var(--gold-dim)":"var(--bg)",
              border:`1px solid ${settings.mode===m?m==="live"?"var(--green)":"var(--gold)":"var(--border2)"}`,
              color:settings.mode===m?m==="live"?"var(--green)":"var(--gold)":"var(--text3)",
              fontFamily:"var(--display)",fontSize:14,fontWeight:800,letterSpacing:3}}>
              {m==="shadow"?"👻  SHADOW MODE":"🟢  LIVE TRADING"}
            </button>
          ))}
        </div>
        <p style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text3)",lineHeight:1.8}}>
          Shadow mode simulates all trades server-side with full P&amp;L tracking. Switch to Live once credentials are validated.
        </p>
      </Sect>
      <Sect title="POSITION SIZE">
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",marginBottom:12}}>
          <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text3)"}}>Per trade allocation</span>
          <span style={{fontFamily:"var(--display)",fontSize:28,fontWeight:800,color:"var(--gold)"}}>${settings.positionSize}</span>
        </div>
        <input type="range" min="1" max="1000" step="1" value={settings.positionSize} onChange={e=>updAll({positionSize:+e.target.value})}/>
        <div style={{display:"flex",justifyContent:"space-between",marginTop:6,fontFamily:"var(--mono)",fontSize:9,color:"var(--text3)"}}>
          <span>$1</span><span>$1,000</span>
        </div>
      </Sect>
      <Sect title="MAX CONTRACT PRICE">
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",marginBottom:12}}>
          <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text3)"}}>Price ceiling per contract</span>
          <span style={{fontFamily:"var(--display)",fontSize:28,fontWeight:800,color:"var(--gold)"}}>${settings.maxContractPrice.toFixed(2)}</span>
        </div>
        <input type="range" min="0.01" max="0.50" step="0.01" value={settings.maxContractPrice} onChange={e=>updAll({maxContractPrice:+e.target.value})}/>
        <div style={{display:"flex",justifyContent:"space-between",marginTop:6,fontFamily:"var(--mono)",fontSize:9,color:"var(--text3)"}}>
          <span>$0.01</span><span>$0.50 max</span>
        </div>
      </Sect>
      <Sect title="LIMITLESS API CREDENTIALS">
        {[
          {label:"TOKEN ID",key:"tokenId",ph:"lmts_token_id..."},
          {label:"TOKEN SECRET",key:"tokenSecret",ph:"base64 encoded secret..."},
          {label:"EOA PRIVATE KEY",key:"privateKey",ph:"0x... (wallet private key)"},
        ].map(f=>(
          <div key={f.key} style={{marginBottom:14}}>
            <div style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--text3)",letterSpacing:2,marginBottom:6}}>{f.label}</div>
            <input type="password" placeholder={f.ph} value={settings[f.key]||""} onChange={e=>updAll({[f.key]:e.target.value})}/>
          </div>
        ))}
        <button onClick={validate} disabled={validating} style={{width:"100%",padding:"12px",borderRadius:4,marginTop:4,transition:"all .2s",
          background:"var(--gold-dim)",border:"1px solid var(--gold)",color:"var(--gold)",
          fontFamily:"var(--display)",fontSize:13,fontWeight:700,letterSpacing:2,opacity:validating?.5:1}}>
          {validating?"VALIDATING…":"VALIDATE CREDENTIALS"}
        </button>
        {validated&&(
          <div style={{marginTop:12,padding:"12px 14px",borderRadius:4,fontFamily:"var(--mono)",fontSize:10,lineHeight:2,
            background:validated.live_trading_ready?"var(--green-dim)":"var(--red-dim)",
            border:`1px solid ${validated.live_trading_ready?"#3ddc8440":"#ff475740"}`,
            color:validated.live_trading_ready?"var(--green)":"var(--red)"}}>
            {validated.live_trading_ready
              ?`✓ Ready for live trading · Signer: ${validated.signer_address?.slice(0,20)}…`
              :`✗ ${validated.error||"Missing credentials"}`}
          </div>
        )}
        <p style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--text3)",marginTop:12,lineHeight:1.9}}>
          ⚠ EOA wallets only. Maker = Signer. Do not use a smart wallet address.
        </p>
      </Sect>
      <Sect title="SYSTEM INFO">
        <div style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text3)",lineHeight:2.4}}>
          <div>Engine  ·  Runs server-side 24/7 (no browser required)</div>
          <div>Data  ·  OKX Spot Market — 15m candles refreshed every 15s</div>
          <div>Pairs  ·  BTC·ETH / SOL·DOGE / XRP·BNB (family rotation)</div>
          <div>Models  ·  12 confluence strategies scored per candle</div>
          <div>Cooldown  ·  2 candles after 2 consecutive losses</div>
          <div>Stream  ·  Server-Sent Events push updates to browser</div>
          <div>Keep-alive  ·  /api/ping every 2s (free plan)</div>
        </div>
      </Sect>
    </div>
  );
}

function Sect({title,children}){
  return(
    <div style={{marginBottom:16,border:"1px solid var(--border)",borderRadius:6,overflow:"hidden"}}>
      <div style={{padding:"10px 16px",borderBottom:"1px solid var(--border)",background:"var(--bg1)"}}>
        <span style={{fontFamily:"var(--mono)",fontSize:8,fontWeight:700,letterSpacing:3,color:"var(--text3)"}}>{title}</span>
      </div>
      <div style={{padding:16,background:"var(--bg)"}}>{children}</div>
    </div>
  );
}

/* ═══ TOAST ═════════════════════════════════════════════════════ */
function Toast({msg,type}){
  const c={
    success:{bg:"var(--green-dim)",border:"#3ddc8440",text:"var(--green)"},
    danger: {bg:"var(--red-dim)", border:"#ff475740", text:"var(--red)"},
    warning:{bg:"var(--gold-dim)",border:"#c9a84c40", text:"var(--gold)"},
    info:   {bg:"var(--blue-dim)",border:"#4a9eff40", text:"var(--blue)"},
  }[type]||{bg:"var(--bg2)",border:"var(--border2)",text:"var(--text)"};
  return(
    <div className="fade-in" style={{position:"fixed",top:68,right:20,zIndex:9999,
      padding:"12px 18px",borderRadius:4,maxWidth:320,
      background:c.bg,border:`1px solid ${c.border}`,
      fontFamily:"var(--mono)",fontSize:11,color:c.text,
      letterSpacing:1,boxShadow:"0 8px 40px rgba(0,0,0,.7)"}}>
      {msg}
    </div>
  );
}
