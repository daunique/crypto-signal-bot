import { useState, useEffect, useRef, useCallback } from "react";

const CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Barlow+Condensed:wght@400;600;700;800;900&family=Barlow:wght@400;500&display=swap');
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#07080a;--bg1:#0c0e11;--bg2:#111317;--bg3:#181b20;
    --border:#1e2228;--border2:#272c34;
    --gold:#c9a84c;--gold2:#e8c96a;--gdim:rgba(201,168,76,.14);
    --green:#3ddc84;--gdim2:rgba(61,220,132,.12);
    --red:#ff4757;--rdim:rgba(255,71,87,.12);
    --blue:#4a9eff;--bdim:rgba(74,158,255,.1);
    --purple:#9945FF;--pdim:rgba(153,69,255,.12);
    --text:#d4d8e0;--t2:#8892a0;--t3:#4a5260;
    --mono:'Space Mono',monospace;--head:'Barlow Condensed',sans-serif;--body:'Barlow',sans-serif;
    --r:6px;
  }
  html,body,#root{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--body);overflow-x:hidden}
  ::-webkit-scrollbar{width:3px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
  @keyframes ring{0%{box-shadow:0 0 0 0 rgba(201,168,76,.7)}100%{box-shadow:0 0 0 10px transparent}}
  @keyframes up{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes tape{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
  @keyframes glow{0%,100%{opacity:.4}50%{opacity:1}}
  .anim{animation:up .3s ease forwards}
  input[type=range]{-webkit-appearance:none;width:100%;height:2px;background:var(--border2);border-radius:1px;outline:none}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--gold);cursor:pointer;border:2px solid var(--bg);box-shadow:0 0 6px var(--gold)}
  input[type=password],input[type=text]{background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;padding:9px 11px;border-radius:4px;width:100%;outline:none;transition:border-color .15s}
  input:focus{border-color:var(--gold)}
  button{cursor:pointer;font-family:var(--head);border:none;outline:none}
  select{background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;padding:7px 10px;border-radius:4px;outline:none;width:100%}
  select:focus{border-color:var(--gold)}
`;

function injectCSS(){if(!document.getElementById("oc")){const s=document.createElement("style");s.id="oc";s.textContent=CSS;document.head.appendChild(s);}}

const PAIRS={
  "BTC-USDT":{fam:0,label:"BTC",color:"#c9a84c"},
  "ETH-USDT":{fam:0,label:"ETH",color:"#4a9eff"},
  "SOL-USDT":{fam:1,label:"SOL",color:"#9945FF"},
  "DOGE-USDT":{fam:1,label:"DOGE",color:"#C2A633"},
  "XRP-USDT":{fam:2,label:"XRP",color:"#00AAE4"},
  "BNB-USDT":{fam:2,label:"BNB",color:"#F0B90B"},
};
const FAMS=["BTC·ETH","SOL·DOGE","XRP·BNB"];
const C15=15*60*1000;
const fp=(n,pair)=>{if(n==null)return"—";return(pair?.includes("BTC")||pair?.includes("ETH"))?Number(n).toFixed(2):Number(n).toFixed(4);};
const ft=ts=>{const d=new Date(ts);return`${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;};
const msLeft=(now=Date.now())=>(Math.floor(now/C15)*C15+C15)-now;
const pct=(now=Date.now())=>((C15-msLeft(now))/C15)*100;
function useKeepAlive(){useEffect(()=>{const id=setInterval(()=>fetch("/api/ping").catch(()=>{}),2000);return()=>clearInterval(id);},[]);}

export default function App(){
  injectCSS();useKeepAlive();
  const [prices,setP]=useState({});
  const [sigs,setSigs]=useState([]);
  const [active,setActive]=useState(null);
  const [stats,setStats]=useState({wins:0,losses:0,total:0});
  const [demoStats,setDemoStats]=useState({wins:0,losses:0,total:0,pnl:0});
  const [cool,setCool]=useState(0);
  const [lastFam,setLastFam]=useState(null);
  const [tab,setTab]=useState("dashboard");
  const [toast,setToast]=useState(null);
  const [cd,setCd]=useState(0);
  const [prog,setProg]=useState(0);
  const [conn,setConn]=useState(false);
  const [settings,setSettings]=useState({mode:"shadow",positionSize:10,maxContractPrice:0.50,demoStake:10,privateKey:"",tokenId:"",tokenSecret:""});

  const showToast=useCallback((msg,type="info")=>{setToast({msg,type});setTimeout(()=>setToast(null),4000);},[]);

  useEffect(()=>{const id=setInterval(()=>{const n=Date.now();setCd(Math.ceil(msLeft(n)/1000));setProg(pct(n));},500);return()=>clearInterval(id);},[]);

  useEffect(()=>{
    let es,retry;
    function connect(){
      es=new EventSource("/api/stream");
      es.addEventListener("snapshot",e=>{
        const d=JSON.parse(e.data);
        setP(d.prices||{});setSigs(d.signals||[]);setActive(d.activeSignal||null);
        setStats(d.stats||{wins:0,losses:0,total:0});
        setDemoStats(d.demoStats||{wins:0,losses:0,total:0,pnl:0});
        setCool(d.cooldownCandles||0);setLastFam(d.lastFamily??null);
        if(d.settings)setSettings(prev=>({...prev,...d.settings}));
        setConn(true);
      });
      es.addEventListener("prices",e=>setP(JSON.parse(e.data)));
      es.addEventListener("signal_new",e=>{
        const s=JSON.parse(e.data);setActive(s);
        showToast(`${PAIRS[s.pair]?.label} ${s.direction} · ${s.confidence}%`,s.direction==="UP"?"success":"danger");
        if("Notification" in window&&Notification.permission==="granted")
          new Notification(`${PAIRS[s.pair]?.label} ${s.direction==="UP"?"▲":"▼"} ${s.confidence}%`);
      });
      es.addEventListener("signal_order",e=>{const{id,orderResult}=JSON.parse(e.data);setActive(prev=>prev?.id===id?{...prev,orderResult}:prev);});
      es.addEventListener("signal_settled",e=>{const s=JSON.parse(e.data);setSigs(prev=>[s,...prev].slice(0,200));setActive(null);showToast(`${PAIRS[s.pair]?.label} ${s.result}`,s.result==="WIN"?"success":"danger");});
      es.addEventListener("stats",e=>setStats(JSON.parse(e.data)));
      es.addEventListener("stats_reset",e=>setStats(JSON.parse(e.data)));
      es.addEventListener("cooldown",e=>setCool(JSON.parse(e.data).remaining||0));
      es.addEventListener("demo_stats",e=>setDemoStats(JSON.parse(e.data)));
      es.onerror=()=>{setConn(false);es.close();retry=setTimeout(connect,3000);};
    }
    connect();
    if("Notification" in window&&Notification.permission==="default")Notification.requestPermission();
    return()=>{es?.close();clearTimeout(retry);};
  },[showToast]);

  const saveSettings=useCallback(async ns=>{
    setSettings(ns);
    try{await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(ns)});}catch{}
  },[]);

  const todayStart=new Date();todayStart.setHours(0,0,0,0);
  const todaySigs=sigs.filter(s=>s.timestamp>=todayStart.getTime());
  const wr=stats.total>0?((stats.wins/stats.total)*100).toFixed(1):"—";

  const TABS=[
    {id:"dashboard",label:"HOME"},
    {id:"demo",label:"DEMO"},
    {id:"history",label:"HISTORY"},
    {id:"settings",label:"CONFIG"},
  ];

  return(
    <div style={{minHeight:"100vh",background:"var(--bg)"}}>
      <div style={{position:"fixed",inset:0,pointerEvents:"none",zIndex:0,backgroundImage:"repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.05) 2px,rgba(0,0,0,.05) 4px)"}}/>
      <div style={{position:"fixed",top:-200,left:"50%",transform:"translateX(-50%)",width:"min(900px,100vw)",height:400,borderRadius:"50%",pointerEvents:"none",zIndex:0,background:"radial-gradient(ellipse,rgba(201,168,76,.05) 0%,transparent 70%)"}}/>
      {toast&&<Toast msg={toast.msg} type={toast.type}/>}
      <Header tabs={TABS} tab={tab} setTab={setTab} mode={settings.mode} cd={cd} prog={prog} conn={conn}/>
      <Tape prices={prices}/>
      <div style={{maxWidth:1400,margin:"0 auto",padding:"0 12px 80px",position:"relative",zIndex:1}}>
        {tab==="dashboard"&&<Dashboard stats={stats} wr={wr} todaySigs={todaySigs} lastFam={lastFam} cool={cool} active={active} prices={prices} sigs={sigs}/>}
        {tab==="demo"&&<Demo demoStats={demoStats}/>}
        {tab==="history"&&<History/>}
        {tab==="settings"&&<Settings settings={settings} save={saveSettings}/>}
      </div>
    </div>
  );
}

/* ── HEADER ─────────────────────────────────────────── */
function Header({tabs,tab,setTab,mode,cd,prog,conn}){
  const mm=String(Math.floor(cd/60)).padStart(2,"0"),ss=String(cd%60).padStart(2,"0");
  return(
    <header style={{borderBottom:"1px solid var(--border)",background:"rgba(7,8,10,.97)",backdropFilter:"blur(16px)",position:"sticky",top:0,zIndex:100}}>
      <div style={{maxWidth:1400,margin:"0 auto",padding:"0 12px",height:52,display:"flex",alignItems:"center",gap:12}}>
        <div style={{display:"flex",alignItems:"center",gap:8,flex:"none"}}>
          <div style={{width:30,height:30,borderRadius:4,background:"var(--gold)",display:"flex",alignItems:"center",justifyContent:"center",fontFamily:"var(--mono)",fontSize:15,fontWeight:700,color:"#000"}}>∞</div>
          <span style={{fontFamily:"var(--head)",fontSize:14,fontWeight:900,letterSpacing:3,color:"var(--text)"}}>ORACLE</span>
        </div>
        <div style={{flex:1,display:"flex",alignItems:"center",gap:8,minWidth:0}}>
          <div style={{flex:1,height:2,background:"var(--border)",borderRadius:1,overflow:"hidden"}}>
            <div style={{height:"100%",width:`${prog}%`,background:"linear-gradient(90deg,var(--gold),var(--gold2))",transition:"width .5s linear",borderRadius:1}}/>
          </div>
          <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--gold)",flex:"none",minWidth:36}}>{mm}:{ss}</span>
        </div>
        <div style={{display:"flex",alignItems:"center",gap:5,padding:"4px 8px",border:`1px solid ${conn?mode==="live"?"rgba(61,220,132,.4)":"rgba(201,168,76,.3)":"rgba(255,71,87,.4)"}`,borderRadius:3,flex:"none"}}>
          <div style={{width:5,height:5,borderRadius:"50%",background:conn?mode==="live"?"var(--green)":"var(--gold)":"var(--red)",animation:conn?"glow 2s infinite":"none"}}/>
          <span style={{fontFamily:"var(--mono)",fontSize:9,fontWeight:700,letterSpacing:1.5,color:conn?mode==="live"?"var(--green)":"var(--gold)":"var(--red)"}}>
            {conn?(mode==="live"?"LIVE":"SHADOW"):"–"}
          </span>
        </div>
        <nav style={{display:"flex",gap:1,flex:"none"}}>
          {tabs.map(t=>(
            <button key={t.id} onClick={()=>setTab(t.id)} style={{
              padding:"5px 9px",borderRadius:3,
              background:tab===t.id?"var(--bg3)":"transparent",
              border:`1px solid ${tab===t.id?"var(--border2)":"transparent"}`,
              color:tab===t.id?"var(--gold)":"var(--t3)",
              fontFamily:"var(--head)",fontSize:10,fontWeight:700,letterSpacing:1.5,
              textTransform:"uppercase",transition:"all .15s",
            }}>{t.label}</button>
          ))}
        </nav>
      </div>
    </header>
  );
}

/* ── TAPE ───────────────────────────────────────────── */
function Tape({prices}){
  const items=Object.entries(PAIRS).map(([pair,m])=>{
    const t=prices[pair];const chg=t?((t.last-t.open24h)/t.open24h*100):null;
    return{pair,m,t,chg};
  });
  return(
    <div style={{borderBottom:"1px solid var(--border)",background:"var(--bg1)",overflow:"hidden",height:28,display:"flex",alignItems:"center"}}>
      <div style={{display:"flex",animation:"tape 30s linear infinite",whiteSpace:"nowrap"}}>
        {[...items,...items].map((item,i)=>(
          <div key={i} style={{display:"flex",alignItems:"center",gap:7,padding:"0 18px",borderRight:"1px solid var(--border)",height:28}}>
            <span style={{fontFamily:"var(--head)",fontSize:11,fontWeight:700,color:item.m.color}}>{item.m.label}</span>
            <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--text)"}}>{item.t?fp(item.t.last,item.pair):"—"}</span>
            {item.chg!=null&&<span style={{fontFamily:"var(--mono)",fontSize:9,color:item.chg>=0?"var(--green)":"var(--red)"}}>{item.chg>=0?"+":""}{item.chg.toFixed(2)}%</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── DASHBOARD ──────────────────────────────────────── */
function Dashboard({stats,wr,todaySigs,lastFam,cool,active,prices,sigs}){
  return(
    <div style={{paddingTop:14}}>
      <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:1,background:"var(--border)",borderRadius:"var(--r)",overflow:"hidden",marginBottom:12}}>
        {[
          {label:"WIN RATE",val:`${wr}%`,color:"var(--gold)"},
          {label:"WINS",val:stats.wins,color:"var(--green)"},
          {label:"LOSSES",val:stats.losses,color:"var(--red)"},
          {label:"TODAY",val:todaySigs.length,color:"var(--text)"},
          {label:"FAMILY",val:lastFam!==null?FAMS[lastFam]:"—",color:"var(--blue)"},
          {label:"ENGINE",val:cool>0?`COOL ${cool}`:"LIVE",color:cool>0?"var(--red)":"var(--green)"},
        ].map((s,i)=>(
          <div key={i} style={{background:"var(--bg1)",padding:"10px 12px"}}>
            <div style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)",letterSpacing:1.5,marginBottom:4}}>{s.label}</div>
            <div style={{fontFamily:"var(--head)",fontSize:20,fontWeight:800,color:s.color,lineHeight:1}}>{s.val}</div>
          </div>
        ))}
      </div>
      <div style={{display:"flex",flexDirection:"column",gap:12}}>
        {active?<ActiveCard signal={active} prices={prices}/>:<IdleCard/>}
        <div style={{display:"grid",gridTemplateColumns:"1fr",gap:12}}>
          <PriceGrid prices={prices}/>
          <LogPanel sigs={sigs}/>
        </div>
      </div>
    </div>
  );
}

/* ── ACTIVE SIGNAL CARD ─────────────────────────────── */
function ActiveCard({signal,prices}){
  const m=PAIRS[signal.pair];
  const cur=prices[signal.pair]?.last;
  const winning=cur!=null&&((signal.direction==="UP"&&cur>signal.openPrice)||(signal.direction==="DOWN"&&cur<signal.openPrice));
  const pnl=cur!=null?((cur-signal.openPrice)/signal.openPrice*100):null;
  const up=signal.direction==="UP";const gc=up?"var(--green)":"var(--red)";
  return(
    <div className="anim" style={{background:"var(--bg1)",border:`1px solid ${gc}`,borderRadius:"var(--r)",overflow:"hidden"}}>
      <div style={{height:3,background:`linear-gradient(90deg,${gc},transparent)`}}/>
      <div style={{padding:"14px 16px"}}>
        <div style={{display:"flex",alignItems:"flex-start",justifyContent:"space-between",marginBottom:14,gap:12}}>
          <div style={{minWidth:0}}>
            <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:4}}>
              <div style={{width:7,height:7,borderRadius:"50%",background:m.color,flexShrink:0,animation:"ring 1.5s infinite"}}/>
              <span style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)",letterSpacing:2}}>ACTIVE · {ft(signal.timestamp)}</span>
            </div>
            <div style={{fontFamily:"var(--head)",fontSize:"clamp(34px,8vw,52px)",fontWeight:900,color:m.color,lineHeight:1,letterSpacing:-1}}>{m.label}</div>
            <div style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)",marginTop:4}}>{FAMS[signal.family]} · {signal.marketCondition}</div>
          </div>
          <div style={{textAlign:"right",flexShrink:0}}>
            <div style={{display:"inline-flex",alignItems:"center",gap:8,padding:"10px 14px",borderRadius:4,background:up?"var(--gdim2)":"var(--rdim)",border:`1px solid ${gc}`,marginBottom:8}}>
              <span style={{fontSize:16}}>{up?"▲":"▼"}</span>
              <span style={{fontFamily:"var(--head)",fontSize:20,fontWeight:900,color:gc,letterSpacing:2}}>{up?"LONG":"SHORT"}</span>
            </div>
            <div style={{fontFamily:"var(--head)",fontSize:32,fontWeight:900,color:"var(--gold)",lineHeight:1}}>{signal.confidence}%</div>
            <div style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)",letterSpacing:2,marginTop:2}}>CONFIDENCE</div>
          </div>
        </div>
        <div style={{display:"grid",gridTemplateColumns:"repeat(2,1fr)",gap:1,background:"var(--border)",borderRadius:4,overflow:"hidden",marginBottom:10}}>
          {[
            {lbl:"OPEN",val:fp(signal.openPrice,signal.pair),c:"var(--text)"},
            {lbl:"CURRENT",val:fp(cur,signal.pair),c:cur==null?"var(--t3)":winning?"var(--green)":"var(--red)"},
            {lbl:"P&L",val:pnl!=null?`${pnl>=0?"+":""}${pnl.toFixed(3)}%`:"—",c:pnl==null?"var(--t3)":winning?"var(--green)":"var(--red)"},
            {lbl:"RSI",val:signal.rsi||"—",c:signal.rsi>70?"var(--red)":signal.rsi<30?"var(--green)":"var(--gold)"},
          ].map((item,i)=>(
            <div key={i} style={{background:"var(--bg)",padding:"8px 12px"}}>
              <div style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)",letterSpacing:1.5,marginBottom:3}}>{item.lbl}</div>
              <div style={{fontFamily:"var(--mono)",fontSize:13,fontWeight:700,color:item.c}}>{item.val}</div>
            </div>
          ))}
        </div>
        {signal.orderResult&&(
          <div style={{padding:"7px 10px",borderRadius:3,fontFamily:"var(--mono)",fontSize:9,
            background:signal.orderResult.success?"var(--gdim2)":"var(--rdim)",
            border:`1px solid ${signal.orderResult.success?"rgba(61,220,132,.3)":"rgba(255,71,87,.3)"}`,
            color:signal.orderResult.success?"var(--green)":"var(--red)"}}>
            {signal.orderResult.success?`✓ ${signal.orderResult.shadow?"Simulated":"Placed"} · ${signal.orderResult.contracts} contracts @ $${signal.orderResult.price_per_contract}`:`✗ ${signal.orderResult.error}`}
          </div>
        )}
      </div>
    </div>
  );
}

function IdleCard(){
  return(
    <div style={{background:"var(--bg1)",border:"1px solid var(--border)",borderRadius:"var(--r)",padding:"36px 20px",textAlign:"center"}}>
      <div style={{width:36,height:36,borderRadius:"50%",border:"2px solid var(--border2)",borderTopColor:"var(--gold)",margin:"0 auto 14px",animation:"spin 1s linear infinite"}}/>
      <div style={{fontFamily:"var(--head)",fontSize:14,fontWeight:700,letterSpacing:3,color:"var(--t3)",marginBottom:5}}>ENGINE RUNNING</div>
      <div style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)"}}>Signal fires at next candle open · persisted to Supabase</div>
    </div>
  );
}

function PriceGrid({prices}){
  return(
    <div style={{display:"grid",gridTemplateColumns:"repeat(2,1fr)",gap:1,background:"var(--border)",borderRadius:"var(--r)",overflow:"hidden"}}>
      {Object.entries(PAIRS).map(([pair,m])=>{
        const t=prices[pair];const chg=t?((t.last-t.open24h)/t.open24h*100):null;const up=chg==null?null:chg>=0;
        return(
          <div key={pair} style={{background:"var(--bg1)",padding:"10px 14px",position:"relative"}}>
            <div style={{position:"absolute",left:0,top:0,bottom:0,width:2,background:up==null?"var(--border)":up?"var(--green)":"var(--red)"}}/>
            <div style={{paddingLeft:8}}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:4}}>
                <span style={{fontFamily:"var(--head)",fontSize:15,fontWeight:800,color:m.color}}>{m.label}</span>
                {chg!=null&&<span style={{fontFamily:"var(--mono)",fontSize:9,color:up?"var(--green)":"var(--red)"}}>{up?"+":""}{chg.toFixed(2)}%</span>}
              </div>
              <div style={{fontFamily:"var(--mono)",fontSize:13,fontWeight:700,color:"var(--text)"}}>{t?fp(t.last,pair):"—"}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function LogPanel({sigs}){
  return(
    <div style={{background:"var(--bg1)",border:"1px solid var(--border)",borderRadius:"var(--r)",overflow:"hidden"}}>
      <div style={{padding:"9px 14px",borderBottom:"1px solid var(--border)",display:"flex",justifyContent:"space-between"}}>
        <span style={{fontFamily:"var(--mono)",fontSize:8,fontWeight:700,letterSpacing:2,color:"var(--t3)"}}>TODAY'S SIGNALS</span>
        <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)"}}>{sigs.length}</span>
      </div>
      <div style={{maxHeight:260,overflowY:"auto"}}>
        {sigs.length===0
          ?<div style={{padding:"28px",textAlign:"center",fontFamily:"var(--mono)",fontSize:10,color:"var(--t3)"}}>Waiting for first signal…</div>
          :sigs.slice(0,40).map((s,i)=><LogRow key={s.id} s={s} fresh={i===0}/>)
        }
      </div>
    </div>
  );
}

function LogRow({s,fresh}){
  const m=PAIRS[s.pair];const win=s.result==="WIN",loss=s.result==="LOSS";
  return(
    <div className={fresh?"anim":""} style={{display:"grid",gridTemplateColumns:"38px 14px 44px 1fr 36px",alignItems:"center",gap:8,padding:"7px 14px",borderBottom:"1px solid var(--border)",borderLeft:`2px solid ${win?"var(--green)":loss?"var(--red)":"var(--border)"}`,background:fresh?"rgba(201,168,76,.03)":"transparent"}}>
      <span style={{fontFamily:"var(--head)",fontSize:12,fontWeight:700,color:m.color}}>{m.label}</span>
      <span style={{fontFamily:"var(--mono)",fontSize:10,color:s.direction==="UP"?"var(--green)":"var(--red)"}}>{s.direction==="UP"?"▲":"▼"}</span>
      <span style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)"}}>{ft(s.timestamp)}</span>
      <span style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{fp(s.openPrice,s.pair)}→{fp(s.closePrice,s.pair)}</span>
      <span style={{fontFamily:"var(--mono)",fontSize:9,fontWeight:700,color:win?"var(--green)":loss?"var(--red)":"var(--t3)"}}>{win?"WIN":loss?"LOSS":"…"}</span>
    </div>
  );
}

/* ── DEMO TAB ───────────────────────────────────────── */
function Demo({demoStats}){
  const [trades,setTrades]=useState([]);
  const [loading,setLoading]=useState(true);
  const todayStart=new Date();todayStart.setHours(0,0,0,0);

  useEffect(()=>{
    fetch("/api/demo/trades").then(r=>r.json()).then(d=>{
      setTrades(d.trades||[]);setLoading(false);
    }).catch(()=>setLoading(false));
  },[]);

  // Listen for SSE demo updates
  useEffect(()=>{
    const es=new EventSource("/api/stream");
    es.addEventListener("demo_trade_new",e=>{const t=JSON.parse(e.data);setTrades(prev=>[t,...prev].slice(0,200));});
    es.addEventListener("demo_trade_settled",e=>{
      const t=JSON.parse(e.data);
      setTrades(prev=>prev.map(tr=>tr.id===t.id?t:tr));
    });
    return()=>es.close();
  },[]);

  const todayTrades=trades.filter(t=>t.timestamp>=todayStart.getTime());
  const allWins=todayTrades.filter(t=>t.result==="WIN").length;
  const allLosses=todayTrades.filter(t=>t.result==="LOSS").length;
  const totalPnl=todayTrades.reduce((s,t)=>s+(t.pnlUsd||0),0);
  const wr=todayTrades.filter(t=>t.result!=="PENDING").length>0?((allWins/(allWins+allLosses))*100).toFixed(1):"—";
  const pending=trades.find(t=>t.result==="PENDING");

  return(
    <div style={{paddingTop:14}}>
      {/* Header */}
      <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",marginBottom:12,gap:8}}>
        <div>
          <div style={{fontFamily:"var(--head)",fontSize:18,fontWeight:800,letterSpacing:2,color:"var(--purple)"}}>DEMO ACCOUNT</div>
          <div style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)",marginTop:2}}>Shadow trading · mirrors every live signal · no real funds</div>
        </div>
        <div style={{padding:"6px 12px",borderRadius:4,background:"var(--pdim)",border:"1px solid rgba(153,69,255,.3)",fontFamily:"var(--mono)",fontSize:9,color:"var(--purple)",letterSpacing:1}}>PAPER</div>
      </div>

      {/* Stats */}
      <div style={{display:"grid",gridTemplateColumns:"repeat(2,1fr)",gap:1,background:"var(--border)",borderRadius:"var(--r)",overflow:"hidden",marginBottom:12}}>
        {[
          {label:"WIN RATE",val:`${wr}%`,color:"var(--gold)"},
          {label:"TODAY P&L",val:`${totalPnl>=0?"+":""}$${totalPnl.toFixed(2)}`,color:totalPnl>=0?"var(--green)":"var(--red)"},
          {label:"WINS",val:allWins,color:"var(--green)"},
          {label:"LOSSES",val:allLosses,color:"var(--red)"},
        ].map((s,i)=>(
          <div key={i} style={{background:"var(--bg1)",padding:"12px 14px"}}>
            <div style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)",letterSpacing:1.5,marginBottom:4}}>{s.label}</div>
            <div style={{fontFamily:"var(--head)",fontSize:24,fontWeight:800,color:s.color,lineHeight:1}}>{s.val}</div>
          </div>
        ))}
      </div>

      {/* Active demo trade */}
      {pending&&(
        <div className="anim" style={{background:"var(--bg1)",border:"1px solid var(--purple)",borderRadius:"var(--r)",padding:"14px 16px",marginBottom:12}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:10}}>
            <div style={{width:7,height:7,borderRadius:"50%",background:"var(--purple)",animation:"ring 1.5s infinite"}}/>
            <span style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)",letterSpacing:2}}>ACTIVE DEMO TRADE</span>
          </div>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",gap:12}}>
            <div>
              <div style={{fontFamily:"var(--head)",fontSize:28,fontWeight:900,color:PAIRS[pending.pair]?.color||"var(--text)",lineHeight:1}}>{PAIRS[pending.pair]?.label}</div>
              <div style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)",marginTop:4}}>{pending.direction==="UP"?"▲ LONG":"▼ SHORT"} · ${pending.stakeUsd} stake · {pending.contracts} contracts</div>
            </div>
            <div style={{textAlign:"right"}}>
              <div style={{fontFamily:"var(--mono)",fontSize:11,color:"var(--t3)",marginBottom:2}}>OPEN PRICE</div>
              <div style={{fontFamily:"var(--mono)",fontSize:16,fontWeight:700,color:"var(--text)"}}>{fp(pending.openPrice,pending.pair)}</div>
            </div>
          </div>
        </div>
      )}

      {/* Trade history */}
      <div style={{border:"1px solid var(--border)",borderRadius:"var(--r)",overflow:"hidden"}}>
        <div style={{padding:"9px 14px",borderBottom:"1px solid var(--border)",background:"var(--bg2)",display:"flex",justifyContent:"space-between"}}>
          <span style={{fontFamily:"var(--mono)",fontSize:8,fontWeight:700,letterSpacing:2,color:"var(--t3)"}}>DEMO TRADE LOG</span>
          <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)"}}>{trades.length} trades</span>
        </div>
        {/* Table header */}
        <div style={{display:"grid",gridTemplateColumns:"40px 16px 44px 1fr 1fr 54px 60px",gap:8,padding:"7px 14px",background:"var(--bg2)",borderBottom:"1px solid var(--border)"}}>
          {["PAIR","","TIME","OPEN","CLOSE","P&L","RESULT"].map(h=>(
            <span key={h} style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)",letterSpacing:1}}>{h}</span>
          ))}
        </div>
        <div style={{maxHeight:"55vh",overflowY:"auto"}}>
          {loading&&<div style={{padding:"30px",textAlign:"center",fontFamily:"var(--mono)",fontSize:10,color:"var(--t3)"}}>Loading…</div>}
          {!loading&&trades.length===0&&<div style={{padding:"40px",textAlign:"center",fontFamily:"var(--mono)",fontSize:10,color:"var(--t3)"}}>No demo trades yet — waiting for first signal</div>}
          {trades.map((t,i)=>{
            const m=PAIRS[t.pair];const win=t.result==="WIN",loss=t.result==="LOSS";
            const pnl=t.pnlUsd;
            return(
              <div key={t.id} style={{display:"grid",gridTemplateColumns:"40px 16px 44px 1fr 1fr 54px 60px",gap:8,padding:"8px 14px",borderBottom:"1px solid var(--border)",alignItems:"center",background:i%2===0?"var(--bg1)":"var(--bg)",borderLeft:`2px solid ${win?"var(--green)":loss?"var(--red)":"var(--border)"}`}}>
                <span style={{fontFamily:"var(--head)",fontSize:12,fontWeight:700,color:m?.color||"var(--text)"}}>{m?.label||t.pair}</span>
                <span style={{fontFamily:"var(--mono)",fontSize:10,color:t.direction==="UP"?"var(--green)":"var(--red)"}}>{t.direction==="UP"?"▲":"▼"}</span>
                <span style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)"}}>{ft(t.timestamp)}</span>
                <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t2)"}}>{fp(t.openPrice,t.pair)}</span>
                <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t2)"}}>{fp(t.closePrice,t.pair)}</span>
                <span style={{fontFamily:"var(--mono)",fontSize:9,fontWeight:700,color:pnl==null?"var(--t3)":pnl>=0?"var(--green)":"var(--red)"}}>
                  {pnl!=null?`${pnl>=0?"+":""}$${Math.abs(pnl).toFixed(2)}`:"—"}
                </span>
                <span style={{fontFamily:"var(--head)",fontSize:12,fontWeight:800,color:win?"var(--green)":loss?"var(--red)":"var(--t3)"}}>
                  {win?"WIN":loss?"LOSS":"PEND"}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* ── HISTORY TAB ────────────────────────────────────── */
function History(){
  const [dates,setDates]=useState([]);
  const [selectedDate,setSelectedDate]=useState("");
  const [dayData,setDayData]=useState(null);
  const [loading,setLoading]=useState(false);
  const [allStats,setAllStats]=useState([]);

  useEffect(()=>{
    fetch("/api/history/dates").then(r=>r.json()).then(d=>{
      const ds=d.dates||[];setDates(ds);
      if(ds.length>0){setSelectedDate(ds[0]);}
    });
    fetch("/api/history/stats/all").then(r=>r.json()).then(d=>setAllStats(d.rows||[]));
  },[]);

  useEffect(()=>{
    if(!selectedDate)return;
    setLoading(true);setDayData(null);
    fetch(`/api/history/${selectedDate}`).then(r=>r.json()).then(d=>{setDayData(d);setLoading(false);});
  },[selectedDate]);

  const sigs=dayData?.signals||[];
  const dStats=dayData?.stats||{wins:0,losses:0,total:0};
  const wr=dStats.total>0?((dStats.wins/dStats.total)*100).toFixed(1):"—";

  return(
    <div style={{paddingTop:14}}>
      {/* Date selector */}
      <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:14,flexWrap:"wrap"}}>
        <div style={{fontFamily:"var(--head)",fontSize:14,fontWeight:800,letterSpacing:2,color:"var(--text)",flex:"none"}}>SIGNAL HISTORY</div>
        <div style={{flex:1,minWidth:180,maxWidth:260}}>
          <select value={selectedDate} onChange={e=>setSelectedDate(e.target.value)}>
            {dates.length===0&&<option value="">No history yet</option>}
            {dates.map(d=><option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        {dayData&&(
          <div style={{display:"flex",gap:10,fontFamily:"var(--mono)",fontSize:10,flex:"none"}}>
            <span style={{color:"var(--green)"}}>W:{dStats.wins}</span>
            <span style={{color:"var(--red)"}}>L:{dStats.losses}</span>
            <span style={{color:"var(--gold)"}}>{wr}%</span>
          </div>
        )}
      </div>

      {/* Weekly overview strip */}
      {allStats.length>0&&(
        <div style={{display:"flex",gap:1,marginBottom:12,overflowX:"auto",paddingBottom:4}}>
          {allStats.slice(0,14).map(row=>{
            const total=Number(row.total)||0;const wins=Number(row.wins)||0;
            const wr=total>0?((wins/total)*100).toFixed(0):null;
            const dateStr=typeof row.date==="string"?row.date:new Date(row.date).toISOString().slice(0,10);
            return(
              <button key={dateStr} onClick={()=>setSelectedDate(dateStr)} style={{
                flex:"none",minWidth:52,padding:"7px 6px",borderRadius:3,cursor:"pointer",
                background:selectedDate===dateStr?"var(--gdim)":"var(--bg1)",
                border:`1px solid ${selectedDate===dateStr?"var(--gold)":"var(--border)"}`,
                textAlign:"center",
              }}>
                <div style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)",marginBottom:2}}>{dateStr.slice(5)}</div>
                <div style={{fontFamily:"var(--head)",fontSize:13,fontWeight:800,color:wr===null?"var(--t3)":Number(wr)>=60?"var(--green)":Number(wr)>=40?"var(--gold)":"var(--red)"}}>
                  {wr!==null?`${wr}%`:"—"}
                </div>
                <div style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)"}}>{total}sig</div>
              </button>
            );
          })}
        </div>
      )}

      {loading&&<div style={{padding:"40px",textAlign:"center",fontFamily:"var(--mono)",fontSize:10,color:"var(--t3)"}}>Loading {selectedDate}…</div>}

      {!loading&&dayData&&(
        <div style={{border:"1px solid var(--border)",borderRadius:"var(--r)",overflow:"hidden"}}>
          <div style={{display:"grid",gridTemplateColumns:"40px 16px 44px 50px 1fr 1fr 60px",gap:8,padding:"8px 12px",borderBottom:"1px solid var(--border)",background:"var(--bg2)"}}>
            {["PAIR","","TIME","CONF","OPEN","CLOSE","RESULT"].map(h=>(
              <span key={h} style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)",letterSpacing:1}}>{h}</span>
            ))}
          </div>
          <div style={{maxHeight:"60vh",overflowY:"auto"}}>
            {sigs.length===0&&<div style={{padding:"50px",textAlign:"center",fontFamily:"var(--mono)",fontSize:10,color:"var(--t3)"}}>No signals on {selectedDate}</div>}
            {sigs.map((s,i)=>{
              const m=PAIRS[s.pair];const win=s.result==="WIN",loss=s.result==="LOSS";
              return(
                <div key={s.id} style={{display:"grid",gridTemplateColumns:"40px 16px 44px 50px 1fr 1fr 60px",gap:8,padding:"8px 12px",borderBottom:"1px solid var(--border)",alignItems:"center",background:i%2===0?"var(--bg1)":"var(--bg)",borderLeft:`2px solid ${win?"var(--green)":loss?"var(--red)":"var(--border)"}`}}>
                  <span style={{fontFamily:"var(--head)",fontSize:12,fontWeight:700,color:m?.color||"var(--text)"}}>{m?.label||s.pair}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:10,color:s.direction==="UP"?"var(--green)":"var(--red)"}}>{s.direction==="UP"?"▲":"▼"}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--t2)"}}>{ft(s.timestamp)}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--gold)"}}>{s.confidence}%</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t2)",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{fp(s.openPrice,s.pair)}</span>
                  <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t2)",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{fp(s.closePrice,s.pair)}</span>
                  <span style={{fontFamily:"var(--head)",fontSize:12,fontWeight:800,color:win?"var(--green)":loss?"var(--red)":"var(--t3)"}}>
                    {win?"WIN":loss?"LOSS":"PEND"}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── SETTINGS ───────────────────────────────────────── */
function Settings({settings,save}){
  const upd=patch=>save({...settings,...patch});
  const [val,setVal]=useState(null);const [validating,setValidating]=useState(false);
  const validate=async()=>{
    setValidating(true);
    try{const r=await fetch("/api/limitless/validate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({credentials:{privateKey:settings.privateKey,tokenId:settings.tokenId,tokenSecret:settings.tokenSecret}})});setVal(await r.json());}
    catch(e){setVal({error:e.message});}
    setValidating(false);
  };
  return(
    <div style={{paddingTop:14,maxWidth:560}}>
      <div style={{fontFamily:"var(--mono)",fontSize:8,fontWeight:700,letterSpacing:3,color:"var(--t3)",marginBottom:14}}>SYSTEM CONFIGURATION</div>

      <S title="TRADING MODE">
        <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,marginBottom:10}}>
          {["shadow","live"].map(m=>(
            <button key={m} onClick={()=>upd({mode:m})} style={{padding:"12px 8px",borderRadius:4,transition:"all .15s",background:settings.mode===m?m==="live"?"var(--gdim2)":"var(--gdim)":"var(--bg)",border:`1px solid ${settings.mode===m?m==="live"?"var(--green)":"var(--gold)":"var(--border2)"}`,color:settings.mode===m?m==="live"?"var(--green)":"var(--gold)":"var(--t3)",fontFamily:"var(--head)",fontSize:13,fontWeight:800,letterSpacing:2}}>
              {m==="shadow"?"👻 SHADOW":"🟢 LIVE"}
            </button>
          ))}
        </div>
        <p style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)",lineHeight:1.7}}>Shadow simulates orders. Demo account always runs alongside both modes.</p>
      </S>

      <S title="LIVE POSITION SIZE">
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",marginBottom:10}}>
          <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)"}}>Per live trade</span>
          <span style={{fontFamily:"var(--head)",fontSize:22,fontWeight:800,color:"var(--gold)"}}>${settings.positionSize}</span>
        </div>
        <input type="range" min="1" max="1000" step="1" value={settings.positionSize} onChange={e=>upd({positionSize:+e.target.value})}/>
        <div style={{display:"flex",justifyContent:"space-between",marginTop:5,fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)"}}><span>$1</span><span>$1,000</span></div>
      </S>

      <S title="DEMO STAKE SIZE">
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",marginBottom:10}}>
          <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)"}}>Per demo trade</span>
          <span style={{fontFamily:"var(--head)",fontSize:22,fontWeight:800,color:"var(--purple)"}}>${settings.demoStake}</span>
        </div>
        <input type="range" min="1" max="1000" step="1" value={settings.demoStake} onChange={e=>upd({demoStake:+e.target.value})}/>
        <div style={{display:"flex",justifyContent:"space-between",marginTop:5,fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)"}}><span>$1</span><span>$1,000</span></div>
      </S>

      <S title="MAX CONTRACT PRICE">
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",marginBottom:10}}>
          <span style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)"}}>Price ceiling</span>
          <span style={{fontFamily:"var(--head)",fontSize:22,fontWeight:800,color:"var(--gold)"}}>${settings.maxContractPrice.toFixed(2)}</span>
        </div>
        <input type="range" min="0.01" max="0.50" step="0.01" value={settings.maxContractPrice} onChange={e=>upd({maxContractPrice:+e.target.value})}/>
        <div style={{display:"flex",justifyContent:"space-between",marginTop:5,fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)"}}><span>$0.01</span><span>$0.50</span></div>
      </S>

      <S title="LIMITLESS CREDENTIALS">
        {[{lbl:"TOKEN ID",key:"tokenId",ph:"lmts_token_id…"},{lbl:"TOKEN SECRET",key:"tokenSecret",ph:"base64 secret…"},{lbl:"EOA PRIVATE KEY",key:"privateKey",ph:"0x…"}].map(f=>(
          <div key={f.key} style={{marginBottom:11}}>
            <div style={{fontFamily:"var(--mono)",fontSize:7,color:"var(--t3)",letterSpacing:2,marginBottom:5}}>{f.lbl}</div>
            <input type="password" placeholder={f.ph} value={settings[f.key]||""} onChange={e=>upd({[f.key]:e.target.value})}/>
          </div>
        ))}
        <button onClick={validate} disabled={validating} style={{width:"100%",padding:"10px",borderRadius:4,marginTop:4,background:"var(--gdim)",border:"1px solid var(--gold)",color:"var(--gold)",fontFamily:"var(--head)",fontSize:12,fontWeight:700,letterSpacing:2,opacity:validating?.5:1}}>
          {validating?"CHECKING…":"VALIDATE CREDENTIALS"}
        </button>
        {val&&<div style={{marginTop:10,padding:"10px 12px",borderRadius:4,fontFamily:"var(--mono)",fontSize:9,lineHeight:1.8,background:val.live_trading_ready?"var(--gdim2)":"var(--rdim)",border:`1px solid ${val.live_trading_ready?"rgba(61,220,132,.3)":"rgba(255,71,87,.3)"}`,color:val.live_trading_ready?"var(--green)":"var(--red)"}}>
          {val.live_trading_ready?`✓ Ready · Signer: ${val.signer_address?.slice(0,20)}…`:`✗ ${val.error||"Missing credentials"}`}
        </div>}
        <p style={{fontFamily:"var(--mono)",fontSize:8,color:"var(--t3)",marginTop:10,lineHeight:1.8}}>⚠ EOA only. Maker = Signer. No smart wallet.</p>
      </S>

      <S title="SUPABASE DATABASE">
        <div style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)",lineHeight:2.2}}>
          <div>Set <span style={{color:"var(--gold)"}}>DATABASE_URL</span> in Render → Environment</div>
          <div>Use Transaction Pooler URL (port 6543, IPv4)</div>
          <div>Supabase → Settings → Database → Connection string</div>
          <div style={{marginTop:8,color:"var(--t2)"}}>Tables auto-created on first boot: signals, daily_stats, demo_trades</div>
        </div>
      </S>

      <S title="ENGINE INFO">
        <div style={{fontFamily:"var(--mono)",fontSize:9,color:"var(--t3)",lineHeight:2.2}}>
          <div>Timing    · Signal at exact candle OPEN (xx:00/15/30/45)</div>
          <div>Settlement · Confirmed OKX close price at candle END</div>
          <div>Demo      · Mirrors every signal automatically</div>
          <div>Families  · BTC·ETH / SOL·DOGE / XRP·BNB (rotates)</div>
          <div>Cooldown  · 2 candles after 2 consecutive losses</div>
          <div>Keep-alive · /api/ping every 2s</div>
        </div>
      </S>
    </div>
  );
}

function S({title,children}){
  return(
    <div style={{marginBottom:10,border:"1px solid var(--border)",borderRadius:"var(--r)",overflow:"hidden"}}>
      <div style={{padding:"8px 14px",borderBottom:"1px solid var(--border)",background:"var(--bg1)"}}>
        <span style={{fontFamily:"var(--mono)",fontSize:7,fontWeight:700,letterSpacing:3,color:"var(--t3)"}}>{title}</span>
      </div>
      <div style={{padding:14,background:"var(--bg)"}}>{children}</div>
    </div>
  );
}

/* ── TOAST ──────────────────────────────────────────── */
function Toast({msg,type}){
  const c={success:{bg:"var(--gdim2)",b:"rgba(61,220,132,.3)",t:"var(--green)"},danger:{bg:"var(--rdim)",b:"rgba(255,71,87,.3)",t:"var(--red)"},warning:{bg:"var(--gdim)",b:"rgba(201,168,76,.3)",t:"var(--gold)"},info:{bg:"var(--bdim)",b:"rgba(74,158,255,.3)",t:"var(--blue)"}}[type]||{bg:"var(--bg2)",b:"var(--border2)",t:"var(--text)"};
  return(
    <div className="anim" style={{position:"fixed",bottom:16,right:12,zIndex:9999,maxWidth:"calc(100vw - 24px)",padding:"10px 14px",borderRadius:4,background:c.bg,border:`1px solid ${c.b}`,fontFamily:"var(--mono)",fontSize:10,color:c.t,letterSpacing:.5,boxShadow:"0 4px 24px rgba(0,0,0,.6)"}}>
      {msg}
    </div>
  );
}
