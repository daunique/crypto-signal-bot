"""
Signal Engine - fetches OKX OHLCV data and generates ML signals
using the Ensemble RF+GB strategy (58.25% accuracy, ~16/day)
"""
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT"]

# Global model cache (trained once on startup)
_models = {}
_scalers = {}
_models_trained = False


def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 300) -> pd.DataFrame:
    """Fetch OHLCV candles from OKX spot market."""
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": symbol, "bar": bar, "limit": str(limit)}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            logger.error(f"OKX error for {symbol}: {data}")
            return pd.DataFrame()

        rows = data["data"]
        df = pd.DataFrame(rows, columns=[
            "timestamp", "open", "high", "low", "close",
            "vol", "volCcy", "volCcyQuote", "confirm"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
        for col in ["open", "high", "low", "close", "vol"]:
            df[col] = df[col].astype(float)
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"OKX fetch error for {symbol}: {e}")
        return pd.DataFrame()


# ─── Indicator helpers ────────────────────────────────────────────────────────

def _ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    lo = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (lo + 1e-9))

def _atr(h, l, c, p=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def _stoch(h, l, c, k=14, d=3):
    ll = l.rolling(k).min()
    hh = h.rolling(k).max()
    kp = 100 * (c - ll) / (hh - ll + 1e-9)
    return kp, kp.rolling(d).mean()

def _adx(h, l, c, p=14):
    pdm = h.diff().clip(lower=0)
    mdm = (-l.diff()).clip(lower=0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    at = tr.rolling(p).mean()
    pdi = 100 * pdm.rolling(p).mean() / (at + 1e-9)
    mdi = 100 * mdm.rolling(p).mean() / (at + 1e-9)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.rolling(p).mean(), pdi, mdi

def _wr(h, l, c, p=14):
    return -100 * (h.rolling(p).max() - c) / (h.rolling(p).max() - l.rolling(p).min() + 1e-9)

def _cci(h, l, c, p=20):
    tp = (h + l + c) / 3
    s = tp.rolling(p).mean()
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - x.mean())))
    return (tp - s) / (0.015 * md + 1e-9)


FEATURE_COLS = [
    'ret_1', 'ret_3', 'ret_5', 'body', 'upper_wick', 'lower_wick', 'body_ratio',
    'rsi_7', 'rsi_14', 'rsi_21', 'rsi_diff', 'rsi_slope',
    'macd_hist', 'macd_hist_diff', 'macd_cross',
    'bb_pct', 'bb_width', 'ema_cross', 'price_vs_ema21', 'price_vs_ema50',
    'ema8_slope', 'ema21_slope',
    'stoch_k', 'stoch_diff', 'stoch_cross',
    'atr_norm', 'atr_ratio', 'adx', 'di_diff', 'wr', 'cci',
    'vol_ratio', 'vol_trend', 'obv_slope',
    'near_high5', 'near_low5',
    'hour', 'dow', 'session_asia', 'session_ny'
]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build all 40 technical features from OHLCV dataframe."""
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    vol = df['vol']

    df['ret_1'] = c.pct_change(1)
    df['ret_3'] = c.pct_change(3)
    df['ret_5'] = c.pct_change(5)
    df['body'] = (c - o) / (o + 1e-9)
    df['upper_wick'] = (h - c.clip(lower=o)) / (h - l + 1e-9)
    df['lower_wick'] = (c.clip(upper=o) - l) / (h - l + 1e-9)
    df['body_ratio'] = (c - o).abs() / (h - l + 1e-9)

    df['rsi_7'] = _rsi(c, 7)
    df['rsi_14'] = _rsi(c, 14)
    df['rsi_21'] = _rsi(c, 21)
    df['rsi_diff'] = df['rsi_14'].diff()
    df['rsi_slope'] = df['rsi_14'] - df['rsi_14'].shift(3)

    m = _ema(c, 12) - _ema(c, 26)
    ms = _ema(m, 9)
    mh = m - ms
    df['macd_hist'] = mh
    df['macd_hist_diff'] = mh.diff()
    df['macd_cross'] = (m > ms).astype(int)

    e8 = _ema(c, 8)
    e21 = _ema(c, 21)
    e50 = _ema(c, 50)
    bm = c.rolling(20).mean()
    bs = c.rolling(20).std()
    bu = bm + 2 * bs
    bl = bm - 2 * bs
    df['bb_pct'] = (c - bl) / (bu - bl + 1e-9)
    df['bb_width'] = (bu - bl) / (bm + 1e-9)
    df['ema_cross'] = (e8 > e21).astype(int)
    df['price_vs_ema21'] = (c - e21) / (e21 + 1e-9)
    df['price_vs_ema50'] = (c - e50) / (e50 + 1e-9)
    df['ema8_slope'] = e8.pct_change(3)
    df['ema21_slope'] = e21.pct_change(3)

    sk, sd = _stoch(h, l, c)
    df['stoch_k'] = sk
    df['stoch_diff'] = sk - sd
    df['stoch_cross'] = (sk > sd).astype(int)

    at14 = _atr(h, l, c)
    df['atr_norm'] = at14 / (c + 1e-9)
    df['atr_ratio'] = at14 / (at14.rolling(50).mean() + 1e-9)

    adx_v, pdi, mdi_v = _adx(h, l, c)
    df['adx'] = adx_v
    df['di_diff'] = pdi - mdi_v
    df['wr'] = _wr(h, l, c)
    df['cci'] = _cci(h, l, c)

    df['vol_ratio'] = vol / (vol.rolling(20).mean() + 1e-9)
    df['vol_trend'] = vol.rolling(5).mean() / (vol.rolling(20).mean() + 1e-9)
    obv = (np.sign(c.diff()) * vol).cumsum()
    df['obv_slope'] = obv.pct_change(5)

    df['near_high5'] = c / (h.rolling(5).max() + 1e-9)
    df['near_low5'] = c / (l.rolling(5).min() + 1e-9)

    df['hour'] = df['timestamp'].dt.hour
    df['dow'] = df['timestamp'].dt.dayofweek
    df['session_asia'] = ((df['hour'] >= 0) & (df['hour'] < 8)).astype(int)
    df['session_ny'] = ((df['hour'] >= 13) & (df['hour'] < 21)).astype(int)

    return df


def train_models(df: pd.DataFrame, symbol: str):
    """Train RF + GB ensemble on historical data for a symbol."""
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler

    df = build_features(df.copy())
    df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
    df_c = df.dropna(subset=FEATURE_COLS + ['target']).copy()

    if len(df_c) < 100:
        logger.warning(f"Not enough data to train for {symbol}: {len(df_c)} rows")
        return False

    X = df_c[FEATURE_COLS].values
    y = df_c['target'].values

    sc = StandardScaler()
    X_s = sc.fit_transform(X)

    rf = RandomForestClassifier(n_estimators=200, max_depth=8,
                                 min_samples_leaf=15, max_features='sqrt', random_state=42)
    gb = GradientBoostingClassifier(n_estimators=100, max_depth=4,
                                     learning_rate=0.05, min_samples_leaf=15, random_state=42)
    rf.fit(X_s, y)
    gb.fit(X_s, y)

    _models[symbol] = (rf, gb)
    _scalers[symbol] = sc
    logger.info(f"Model trained for {symbol} on {len(df_c)} candles")
    return True


def get_signal_for_symbol(symbol: str, min_confidence: float = 0.58) -> dict | None:
    """
    Fetch latest candles, run ML, return signal dict or None.
    Returns None if no signal fires (confidence below threshold).
    """
    df = fetch_okx_candles(symbol, limit=300)
    if df.empty or len(df) < 100:
        return None

    # Train if not yet trained
    if symbol not in _models:
        success = train_models(df, symbol)
        if not success:
            return None

    df = build_features(df.copy())
    df_c = df.dropna(subset=FEATURE_COLS).copy()
    if df_c.empty:
        return None

    # Use latest complete candle (second-to-last; last may be incomplete)
    row = df_c.iloc[-2]
    latest_candle = df_c.iloc[-1]

    X = row[FEATURE_COLS].values.reshape(1, -1)
    sc = _scalers[symbol]
    X_s = sc.transform(X)

    rf, gb = _models[symbol]
    rf_prob = rf.predict_proba(X_s)[0]
    gb_prob = gb.predict_proba(X_s)[0]
    ens_prob = (rf_prob + gb_prob) / 2
    prob_up = ens_prob[1]

    # Only fire if confidence >= threshold
    if prob_up >= min_confidence:
        direction = "UP"
        confidence = prob_up
    elif prob_up <= (1 - min_confidence):
        direction = "DOWN"
        confidence = 1 - prob_up
    else:
        return None  # No signal

    # Determine tier
    vol_spike = row['vol_ratio'] > 1.5
    tier = "T1" if vol_spike else "T2"

    # Candle timing: next complete candle
    ts = latest_candle['timestamp']
    candle_open = ts
    candle_close = ts + pd.Timedelta(minutes=15)

    return {
        'symbol': symbol,
        'direction': direction,
        'confidence': float(confidence),
        'tier': tier,
        'vol_spike': bool(vol_spike),
        'rsi_14': float(row['rsi_14']),
        'macd_hist': float(row['macd_hist']),
        'adx': float(row['adx']),
        'vol_ratio': float(row['vol_ratio']),
        'open_price': float(latest_candle['close']),
        'candle_open_time': candle_open.to_pydatetime(),
        'candle_close_time': candle_close.to_pydatetime(),
        'current_price': float(latest_candle['close']),
    }


def pick_best_signal(min_confidence: float = 0.58) -> dict | None:
    """
    Run signal engine across all 6 symbols.
    Return ONLY the single best signal (highest confidence + tier).
    This prevents spam of multiple simultaneous signals.
    """
    candidates = []
    for symbol in SYMBOLS:
        sig = get_signal_for_symbol(symbol, min_confidence)
        if sig:
            candidates.append(sig)

    if not candidates:
        return None

    # Score: T1 gets +0.05 bonus, then sort by confidence
    def score(s):
        bonus = 0.05 if s['tier'] == 'T1' else 0.0
        return s['confidence'] + bonus

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def retrain_all(limit: int = 500):
    """Retrain models for all symbols with fresh data."""
    global _models_trained
    for symbol in SYMBOLS:
        df = fetch_okx_candles(symbol, limit=limit)
        if not df.empty:
            train_models(df, symbol)
    _models_trained = True
    logger.info("All models retrained")
