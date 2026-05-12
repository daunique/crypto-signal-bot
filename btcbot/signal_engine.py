"""
Signal Engine — AB Testing Architecture
────────────────────────────────────────
4 independent engines running in parallel, each trained exclusively
on its own pair's historical data. No cross-pair contamination.

Confirmed thresholds from walk-forward validation (5-fold):
  BTC-USDT: 0.58  →  ~55.1% acc  ~16.5 signals/day
  ETH-USDT: 0.59  →  ~56.8% acc  ~15.3 signals/day
  SOL-USDT: 0.59  →  ~57.0% acc  ~14.4 signals/day
  XRP-USDT: 0.59  →  ~56.1% acc  ~14.0 signals/day

Each pair card on the dashboard is fully independent.
No anti-spam filtering — all 4 pairs fire their own signals.
Telegram is disabled (AB test mode).
"""
import numpy as np
import pandas as pd
import requests
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"

# ── Confirmed per-pair thresholds from backtested data ────────────────────────
PAIR_CONFIG = {
    "BTC-USDT": {"threshold": 0.58, "expected_acc": 55.1, "expected_daily": 16.5, "tier": "A"},
    "ETH-USDT": {"threshold": 0.59, "expected_acc": 56.8, "expected_daily": 15.3, "tier": "A"},
    "SOL-USDT": {"threshold": 0.59, "expected_acc": 57.0, "expected_daily": 14.4, "tier": "B"},
    "XRP-USDT": {"threshold": 0.59, "expected_acc": 56.1, "expected_daily": 14.0, "tier": "B"},
}
SYMBOLS = list(PAIR_CONFIG.keys())

# Per-pair model cache
_models:  dict = {}   # symbol → (rf, gb)
_scalers: dict = {}   # symbol → StandardScaler
_trained: dict = {}   # symbol → bool

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
    df['rsi_7']   = _rsi(c,7);  df['rsi_14']  = _rsi(c,14); df['rsi_21']  = _rsi(c,21)
    df['rsi_diff']  = df['rsi_14'].diff()
    df['rsi_slope'] = df['rsi_14'] - df['rsi_14'].shift(3)

    m  = _ema(c,12) - _ema(c,26); ms = _ema(m,9); mh = m - ms
    df['macd_hist']      = mh
    df['macd_hist_diff'] = mh.diff()
    df['macd_cross']     = (m > ms).astype(int)

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


# ── OKX data ──────────────────────────────────────────────────────────────────

def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 500) -> pd.DataFrame:
    try:
        resp = requests.get(
            f"{OKX_BASE}/api/v5/market/candles",
            params={"instId": symbol, "bar": bar, "limit": str(limit)},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            logger.error(f"OKX error {symbol}: {data.get('msg')}")
            return pd.DataFrame()
        df = pd.DataFrame(data["data"], columns=[
            "timestamp","open","high","low","close","vol","volCcy","volCcyQuote","confirm"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
        for col in ["open","high","low","close","vol"]:
            df[col] = df[col].astype(float)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        logger.error(f"OKX fetch error {symbol}: {e}")
        return pd.DataFrame()


# ── Model training ────────────────────────────────────────────────────────────

def train_model(symbol: str, df: pd.DataFrame) -> bool:
    """Train dedicated RF+GB ensemble on this pair's own data only."""
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
    rf.fit(X_s, y)
    gb.fit(X_s, y)

    _models[symbol]  = (rf, gb)
    _scalers[symbol] = sc
    _trained[symbol] = True
    logger.info(f"[{symbol}] Model trained on {len(df_c)} candles "
                f"threshold={PAIR_CONFIG[symbol]['threshold']}")
    return True


def retrain_all(limit: int = 500):
    logger.info("[ENGINE] Retraining all per-pair models...")
    for sym in SYMBOLS:
        df = fetch_okx_candles(sym, limit=limit)
        if not df.empty:
            train_model(sym, df)
    logger.info("[ENGINE] All models ready")


# ── Per-pair signal — independent, no cross-pair logic ───────────────────────

def get_signal_for_symbol(symbol: str) -> dict | None:
    """
    Fully independent signal for one pair.
    Uses only that pair's model and threshold.
    Returns None if confidence below threshold.
    """
    cfg = PAIR_CONFIG.get(symbol)
    if not cfg:
        return None
    threshold = cfg['threshold']

    df = fetch_okx_candles(symbol, limit=300)
    if df.empty or len(df) < 100:
        return None

    if symbol not in _models:
        ok = train_model(symbol, df)
        if not ok:
            return None

    df = build_features(df.copy())
    df_c = df.dropna(subset=FEATURE_COLS).copy()
    if len(df_c) < 2:
        return None

    # Second-to-last row (last candle may still be forming)
    row    = df_c.iloc[-2]
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
    ts        = latest['timestamp']

    return {
        'symbol':            symbol,
        'direction':         direction,
        'confidence':        confidence,
        'threshold':         threshold,
        'tier':              tier,
        'vol_spike':         bool(vol_spike),
        'rsi_14':            float(row['rsi_14']),
        'macd_hist':         float(row['macd_hist']),
        'adx':               float(row['adx']),
        'vol_ratio':         float(row['vol_ratio']),
        'open_price':        float(latest['close']),
        'candle_open_time':  ts.to_pydatetime(),
        'candle_close_time': (ts + pd.Timedelta(minutes=15)).to_pydatetime(),
        'expected_acc':      cfg['expected_acc'],
    }


def get_all_signals() -> dict:
    """
    Run all 4 pair engines independently.
    Returns dict of symbol → signal (or None).
    AB test mode — no filtering, each pair runs its own logic.
    """
    results = {}
    for sym in SYMBOLS:
        try:
            results[sym] = get_signal_for_symbol(sym)
        except Exception as e:
            logger.error(f"[{sym}] Signal error: {e}")
            results[sym] = None
    return results


def get_pair_config() -> dict:
    return PAIR_CONFIG.copy()


# ── Live per-pair outcome tracker ─────────────────────────────────────────────
_pair_stats: dict = {}

def record_outcome(symbol: str, outcome: str):
    if symbol not in _pair_stats:
        _pair_stats[symbol] = {'wins':0,'losses':0,'signals':0}
    _pair_stats[symbol]['signals'] += 1
    if outcome == 'WIN':   _pair_stats[symbol]['wins']   += 1
    elif outcome == 'LOSS': _pair_stats[symbol]['losses'] += 1

def get_pair_stats() -> dict:
    result = {}
    for sym in SYMBOLS:
        s = _pair_stats.get(sym, {'wins':0,'losses':0,'signals':0})
        total = s['wins'] + s['losses']
        cfg   = PAIR_CONFIG.get(sym, {})
        result[sym] = {
            'wins':         s['wins'],
            'losses':       s['losses'],
            'signals':      s['signals'],
            'win_rate':     round(s['wins']/total*100,1) if total>0 else None,
            'threshold':    cfg.get('threshold', 0.59),
            'expected_acc': cfg.get('expected_acc', 56.0),
            'expected_daily': cfg.get('expected_daily', 14.0),
            'tier':         cfg.get('tier','B'),
        }
    return result
