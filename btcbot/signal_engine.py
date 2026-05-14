"""
Signal Engine — Single best signal per candle across 6 pairs
─────────────────────────────────────────────────────────────
Per-pair models trained on their own data.
Anti-spam: only ONE signal fires per 15-min candle (highest confidence).
Pairs: BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, BNB-USDT, DOGE-USDT

Thresholds from backtested walk-forward validation:
  BTC-USDT: 0.58  ETH-USDT: 0.58
  SOL-USDT: 0.61  XRP-USDT: 0.61
  BNB-USDT: 0.63  DOGE-USDT: 0.65
"""
import numpy as np
import pandas as pd
import requests
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
SYMBOLS  = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT"]

PAIR_CONFIG = {
    "BTC-USDT":  {"threshold": 0.58, "tier": "A"},
    "ETH-USDT":  {"threshold": 0.58, "tier": "A"},
    "SOL-USDT":  {"threshold": 0.61, "tier": "B"},
    "XRP-USDT":  {"threshold": 0.61, "tier": "B"},
    "BNB-USDT":  {"threshold": 0.63, "tier": "C"},
    "DOGE-USDT": {"threshold": 0.65, "tier": "C"},
}

_models:      dict = {}
_scalers:     dict = {}
_pair_stats:  dict = {}

FEATURE_COLS = [
    'ret_1','ret_3','ret_5','body','upper_wick','lower_wick','body_ratio',
    'rsi_7','rsi_14','rsi_21','rsi_diff','rsi_slope',
    'macd_hist','macd_hist_diff','macd_cross',
    'bb_pct','bb_width','ema_cross','price_vs_ema21','price_vs_ema50',
    'ema8_slope','ema21_slope',
    'stoch_k','stoch_diff','stoch_cross',
    'atr_norm','atr_ratio','adx','di_diff','wr','cci',
    'vol_ratio','vol_trend','obv_slope',
    'near_high5','near_low5',
    'hour','dow','session_asia','session_ny',
]


# ── Indicators ────────────────────────────────────────────────────────────────

def _ema(s, p): return s.ewm(span=p, adjust=False).mean()

def _rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))

def _atr(h, l, c, p=14):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def _stoch(h, l, c, k=14, d=3):
    kp = 100*(c - l.rolling(k).min()) / (h.rolling(k).max() - l.rolling(k).min() + 1e-9)
    return kp, kp.rolling(d).mean()

def _adx(h, l, c, p=14):
    pdm = h.diff().clip(lower=0)
    mdm = (-l.diff()).clip(lower=0)
    tr  = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    at  = tr.rolling(p).mean()
    pdi = 100*pdm.rolling(p).mean()/(at+1e-9)
    mdi = 100*mdm.rolling(p).mean()/(at+1e-9)
    dx  = 100*(pdi-mdi).abs()/(pdi+mdi+1e-9)
    return dx.rolling(p).mean(), pdi, mdi

def _cci(h, l, c, p=20):
    tp = (h+l+c)/3
    sm = tp.rolling(p).mean()
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x-x.mean())))
    return (tp-sm)/(0.015*md+1e-9)


# ── Feature engineering ───────────────────────────────────────────────────────

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    vol = df['vol']

    df['ret_1'] = c.pct_change(1)
    df['ret_3'] = c.pct_change(3)
    df['ret_5'] = c.pct_change(5)
    df['body']        = (c-o)/(o+1e-9)
    df['upper_wick']  = (h-c.clip(lower=o))/(h-l+1e-9)
    df['lower_wick']  = (c.clip(upper=o)-l)/(h-l+1e-9)
    df['body_ratio']  = (c-o).abs()/(h-l+1e-9)
    df['rsi_7']   = _rsi(c,7);  df['rsi_14'] = _rsi(c,14); df['rsi_21'] = _rsi(c,21)
    df['rsi_diff']  = df['rsi_14'].diff()
    df['rsi_slope'] = df['rsi_14'] - df['rsi_14'].shift(3)
    m  = _ema(c,12)-_ema(c,26); ms = _ema(m,9); mh = m-ms
    df['macd_hist']      = mh
    df['macd_hist_diff'] = mh.diff()
    df['macd_cross']     = (m>ms).astype(int)
    e8=_ema(c,8); e21=_ema(c,21); e50=_ema(c,50)
    bm=c.rolling(20).mean(); bs=c.rolling(20).std()
    bu=bm+2*bs; bl=bm-2*bs
    df['bb_pct']         = (c-bl)/(bu-bl+1e-9)
    df['bb_width']       = (bu-bl)/(bm+1e-9)
    df['ema_cross']      = (e8>e21).astype(int)
    df['price_vs_ema21'] = (c-e21)/(e21+1e-9)
    df['price_vs_ema50'] = (c-e50)/(e50+1e-9)
    df['ema8_slope']     = e8.pct_change(3)
    df['ema21_slope']    = e21.pct_change(3)
    sk,sd = _stoch(h,l,c)
    df['stoch_k']    = sk
    df['stoch_diff'] = sk-sd
    df['stoch_cross']= (sk>sd).astype(int)
    at14 = _atr(h,l,c)
    df['atr_norm']  = at14/(c+1e-9)
    df['atr_ratio'] = at14/(at14.rolling(50).mean()+1e-9)
    adx_v,pdi,mdi_v = _adx(h,l,c)
    df['adx']    = adx_v
    df['di_diff']= pdi-mdi_v
    df['wr']  = -100*(h.rolling(14).max()-c)/(h.rolling(14).max()-l.rolling(14).min()+1e-9)
    df['cci'] = _cci(h,l,c)
    df['vol_ratio'] = vol/(vol.rolling(20).mean()+1e-9)
    df['vol_trend'] = vol.rolling(5).mean()/(vol.rolling(20).mean()+1e-9)
    obv = (np.sign(c.diff())*vol).cumsum()
    df['obv_slope']  = obv.pct_change(5)
    df['near_high5'] = c/(h.rolling(5).max()+1e-9)
    df['near_low5']  = c/(l.rolling(5).min()+1e-9)
    ts_col = 'timestamp' if 'timestamp' in df.columns else 'ts'
    df['hour'] = df[ts_col].dt.hour
    df['dow']  = df[ts_col].dt.dayofweek
    df['session_asia'] = ((df['hour']>=0)&(df['hour']<8)).astype(int)
    df['session_ny']   = ((df['hour']>=13)&(df['hour']<21)).astype(int)
    return df


# ── OKX data fetch ────────────────────────────────────────────────────────────

def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 500) -> pd.DataFrame:
    try:
        resp = requests.get(
            f"{OKX_BASE}/api/v5/market/candles",
            params={"instId": symbol, "bar": bar, "limit": str(limit)},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            logger.error(f"OKX {symbol}: {data.get('msg')}")
            return pd.DataFrame()
        df = pd.DataFrame(data["data"], columns=[
            "timestamp","open","high","low","close","vol","volCcy","volCcyQuote","confirm"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
        for col in ["open","high","low","close","vol"]:
            df[col] = df[col].astype(float)
        # Keep confirm as string ("0" = live/unconfirmed, "1" = fully closed)
        df["confirm"] = df["confirm"].astype(str)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        logger.error(f"OKX fetch {symbol}: {e}")
        return pd.DataFrame()


# ── Per-pair model training ───────────────────────────────────────────────────

def train_model(symbol: str, df: pd.DataFrame) -> bool:
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler

    df = build_features(df.copy())
    df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
    df_c = df.dropna(subset=FEATURE_COLS+['target']).copy()

    if len(df_c) < 150:
        logger.warning(f"[{symbol}] Need 150+ candles, got {len(df_c)}")
        return False

    X = df_c[FEATURE_COLS].values
    y = df_c['target'].values
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler()
    X_s = sc.fit_transform(X)

    rf = RandomForestClassifier(
        n_estimators=300, max_depth=8, min_samples_leaf=15,
        max_features='sqrt', random_state=42, n_jobs=-1
    )
    gb = GradientBoostingClassifier(
        n_estimators=150, max_depth=4, learning_rate=0.05,
        min_samples_leaf=15, random_state=42
    )
    rf.fit(X_s, y); gb.fit(X_s, y)
    _models[symbol]  = (rf, gb)
    _scalers[symbol] = sc
    logger.info(f"[{symbol}] Model trained on {len(df_c)} candles "
                f"threshold={PAIR_CONFIG.get(symbol,{}).get('threshold',0.58)}")
    return True


def retrain_all(limit: int = 500):
    logger.info("[ENGINE] Retraining all models...")
    for sym in SYMBOLS:
        df = fetch_okx_candles(sym, limit=limit)
        if not df.empty:
            train_model(sym, df)
    logger.info("[ENGINE] All models ready")


# ── Signal generation ─────────────────────────────────────────────────────────

def get_signal_for_symbol(symbol: str) -> dict | None:
    cfg       = PAIR_CONFIG.get(symbol, {})
    threshold = cfg.get("threshold", 0.58)

    df = fetch_okx_candles(symbol, limit=300)
    if df.empty or len(df) < 100:
        return None

    if symbol not in _models:
        ok = train_model(symbol, df)
        if not ok:
            return None

    df   = build_features(df.copy())
    df_c = df.dropna(subset=FEATURE_COLS).copy()
    if len(df_c) < 2:
        return None

    row    = df_c.iloc[-2]   # last CONFIRMED candle
    latest = df_c.iloc[-1]

    X    = row[FEATURE_COLS].values.reshape(1,-1)
    X_s  = _scalers[symbol].transform(X)
    rf, gb = _models[symbol]
    ens  = (rf.predict_proba(X_s)[0] + gb.predict_proba(X_s)[0]) / 2
    prob = float(ens[1])

    if prob >= threshold:
        direction  = "UP"
        confidence = prob
    elif prob <= (1.0 - threshold):
        direction  = "DOWN"
        confidence = 1.0 - prob
    else:
        return None

    vol_spike = float(row['vol_ratio']) > 1.5
    tier      = "T1" if vol_spike else "T2"

    # ── Exact 15-min candle boundary timing ──────────────────────────────────
    # Signal fires 2 minutes BEFORE the current candle closes (e.g. at :13).
    # We track the NEXT candle (the one that opens at the upcoming boundary)
    # so win/loss is measured over the period that starts after the signal drops.
    #
    # Example: signal fires at 00:13
    #   current candle:  00:00 → 00:15  (closing in 2 min — not tracked)
    #   tracked candle:  00:15 → 00:30  ← this is what we record as open/close
    ts = latest['timestamp']  # tz-aware UTC Timestamp

    # Floor to current 15-min boundary, then advance one period to get NEXT candle
    minutes      = ts.minute
    boundary     = (minutes // 15) * 15
    current_open = ts.replace(minute=boundary, second=0, microsecond=0)
    candle_open  = current_open + pd.Timedelta(minutes=15)   # next candle open
    candle_close = candle_open  + pd.Timedelta(minutes=15)   # next candle close

    return {
        'symbol':            symbol,
        'direction':         direction,
        'confidence':        confidence,
        'threshold':         threshold,
        'margin':            confidence - threshold,
        'tier':              tier,
        'vol_spike':         bool(vol_spike),
        'rsi_14':            float(row['rsi_14']),
        'macd_hist':         float(row['macd_hist']),
        'adx':               float(row['adx']),
        'vol_ratio':         float(row['vol_ratio']),
        'open_price':        float(latest['close']),  # preview only; resolver overwrites with tracked candle's actual open
        'candle_open_time':  candle_open.to_pydatetime().replace(tzinfo=None),
        'candle_close_time': candle_close.to_pydatetime().replace(tzinfo=None),
    }


def pick_best_signal(min_confidence: float = None) -> dict | None:
    """
    Evaluate all 6 pairs. Return ONLY the single best signal.
    Best = highest margin above its pair-specific threshold.
    T1 (volume spike) gets a +0.03 bonus.
    This guarantees exactly ONE signal per candle maximum.
    """
    candidates = []
    for sym in SYMBOLS:
        try:
            sig = get_signal_for_symbol(sym)
            if sig:
                candidates.append(sig)
                logger.info(f"[{sym}] candidate {sig['direction']} "
                            f"conf={sig['confidence']:.3f} margin={sig['margin']:.3f}")
        except Exception as e:
            logger.error(f"[{sym}] error: {e}")

    if not candidates:
        logger.info("[ENGINE] No qualifying signals this candle")
        return None

    def score(s):
        return s['margin'] + (0.03 if s['tier'] == 'T1' else 0.0)

    best = max(candidates, key=score)
    logger.info(f"[ENGINE] Best: {best['symbol']} {best['direction']} "
                f"conf={best['confidence']:.3f} tier={best['tier']}")
    return best


# ── Pair stats tracker ────────────────────────────────────────────────────────

def record_outcome(symbol: str, outcome: str):
    if symbol not in _pair_stats:
        _pair_stats[symbol] = {'wins': 0, 'losses': 0, 'signals': 0}
    _pair_stats[symbol]['signals'] += 1
    if outcome == 'WIN':   _pair_stats[symbol]['wins']   += 1
    elif outcome == 'LOSS': _pair_stats[symbol]['losses'] += 1


def get_pair_stats() -> dict:
    result = {}
    for sym in SYMBOLS:
        s     = _pair_stats.get(sym, {'wins':0,'losses':0,'signals':0})
        total = s['wins'] + s['losses']
        cfg   = PAIR_CONFIG.get(sym, {})
        result[sym] = {
            'wins':      s['wins'],
            'losses':    s['losses'],
            'signals':   s['signals'],
            'win_rate':  round(s['wins']/total*100, 1) if total > 0 else None,
            'threshold': cfg.get('threshold', 0.58),
            'tier':      cfg.get('tier', 'B'),
        }
    return result


def get_pair_config() -> dict:
    return PAIR_CONFIG.copy()
