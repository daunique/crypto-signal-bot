"""
Signal Engine — Per-pair ML models with calibrated confidence thresholds
────────────────────────────────────────────────────────────────────────
Each pair gets its own trained RF+GB ensemble.
Confidence thresholds are calibrated by pair quality tier:

  Tier A (BTC, ETH)   → 0.58  High liquidity, strong TA signal
  Tier B (BNB, SOL)   → 0.61  Moderate noise, needs higher certainty
  Tier C (XRP, DOGE)  → 0.65  News/sentiment driven, strictest filter

Anti-spam: only the single best signal per 15-min candle fires.
Best = highest (confidence - threshold_gap) score, T1 vol-spike bonus applied.
"""
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
SYMBOLS  = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT"]

# ── Per-pair confidence thresholds ────────────────────────────────────────────
# Calibrated from BTC backtest sensitivity analysis + pair liquidity/noise research
PAIR_THRESHOLDS = {
    "BTC-USDT":  0.58,   # A — Gold standard, model trained on BTC data
    "ETH-USDT":  0.58,   # A — 0.92 BTC correlation, high liquidity
    "BNB-USDT":  0.61,   # B — Exchange token, moderate noise
    "SOL-USDT":  0.61,   # B — High beta/volatility, needs higher certainty
    "XRP-USDT":  0.65,   # C — News-driven, low TA reliability
    "DOGE-USDT": 0.65,   # C — Sentiment-driven, weakest signal quality
}

# ── Per-pair model + scaler cache ─────────────────────────────────────────────
_models:    dict = {}   # symbol → (rf, gb)
_scalers:   dict = {}   # symbol → StandardScaler
_pair_stats: dict = {}  # symbol → live win/loss tracking

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


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _ema(s, p):  return s.ewm(span=p, adjust=False).mean()

def _rsi(s, p=14):
    d  = s.diff()
    g  = d.clip(lower=0).rolling(p).mean()
    lo = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (lo + 1e-9))

def _atr(h, l, c, p=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def _stoch(h, l, c, k=14, d=3):
    ll  = l.rolling(k).min()
    hh  = h.rolling(k).max()
    kp  = 100 * (c - ll) / (hh - ll + 1e-9)
    return kp, kp.rolling(d).mean()

def _adx(h, l, c, p=14):
    pdm = h.diff().clip(lower=0)
    mdm = (-l.diff()).clip(lower=0)
    tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    at  = tr.rolling(p).mean()
    pdi = 100 * pdm.rolling(p).mean() / (at + 1e-9)
    mdi = 100 * mdm.rolling(p).mean() / (at + 1e-9)
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.rolling(p).mean(), pdi, mdi

def _cci(h, l, c, p=20):
    tp = (h + l + c) / 3
    s  = tp.rolling(p).mean()
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - x.mean())))
    return (tp - s) / (0.015 * md + 1e-9)


# ── OKX data fetch ────────────────────────────────────────────────────────────

def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 500) -> pd.DataFrame:
    url    = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": symbol, "bar": bar, "limit": str(limit)}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            logger.error(f"OKX API error for {symbol}: {data.get('msg')}")
            return pd.DataFrame()
        rows = data["data"]
        df = pd.DataFrame(rows, columns=[
            "timestamp","open","high","low","close",
            "vol","volCcy","volCcyQuote","confirm"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
        for col in ["open","high","low","close","vol"]:
            df[col] = df[col].astype(float)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        logger.error(f"OKX fetch error for {symbol}: {e}")
        return pd.DataFrame()


# ── Feature engineering ───────────────────────────────────────────────────────

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    vol = df['vol']

    df['ret_1'] = c.pct_change(1)
    df['ret_3'] = c.pct_change(3)
    df['ret_5'] = c.pct_change(5)
    df['body']        = (c - o) / (o + 1e-9)
    df['upper_wick']  = (h - c.clip(lower=o)) / (h - l + 1e-9)
    df['lower_wick']  = (c.clip(upper=o) - l) / (h - l + 1e-9)
    df['body_ratio']  = (c - o).abs() / (h - l + 1e-9)

    df['rsi_7']   = _rsi(c, 7)
    df['rsi_14']  = _rsi(c, 14)
    df['rsi_21']  = _rsi(c, 21)
    df['rsi_diff']  = df['rsi_14'].diff()
    df['rsi_slope'] = df['rsi_14'] - df['rsi_14'].shift(3)

    m  = _ema(c, 12) - _ema(c, 26)
    ms = _ema(m, 9)
    mh = m - ms
    df['macd_hist']      = mh
    df['macd_hist_diff'] = mh.diff()
    df['macd_cross']     = (m > ms).astype(int)

    e8  = _ema(c, 8);  e21 = _ema(c, 21);  e50 = _ema(c, 50)
    bm  = c.rolling(20).mean()
    bs  = c.rolling(20).std()
    bu  = bm + 2 * bs;  bl = bm - 2 * bs
    df['bb_pct']        = (c - bl) / (bu - bl + 1e-9)
    df['bb_width']      = (bu - bl) / (bm + 1e-9)
    df['ema_cross']     = (e8 > e21).astype(int)
    df['price_vs_ema21'] = (c - e21) / (e21 + 1e-9)
    df['price_vs_ema50'] = (c - e50) / (e50 + 1e-9)
    df['ema8_slope']    = e8.pct_change(3)
    df['ema21_slope']   = e21.pct_change(3)

    sk, sd = _stoch(h, l, c)
    df['stoch_k']    = sk
    df['stoch_diff'] = sk - sd
    df['stoch_cross']= (sk > sd).astype(int)

    at14 = _atr(h, l, c)
    df['atr_norm']  = at14 / (c + 1e-9)
    df['atr_ratio'] = at14 / (at14.rolling(50).mean() + 1e-9)

    adx_v, pdi, mdi_v = _adx(h, l, c)
    df['adx']    = adx_v
    df['di_diff']= pdi - mdi_v

    df['wr']  = -100 * (h.rolling(14).max() - c) / (h.rolling(14).max() - l.rolling(14).min() + 1e-9)
    df['cci'] = _cci(h, l, c)

    df['vol_ratio'] = vol / (vol.rolling(20).mean() + 1e-9)
    df['vol_trend'] = vol.rolling(5).mean() / (vol.rolling(20).mean() + 1e-9)
    obv = (np.sign(c.diff()) * vol).cumsum()
    df['obv_slope'] = obv.pct_change(5)

    df['near_high5'] = c / (h.rolling(5).max() + 1e-9)
    df['near_low5']  = c / (l.rolling(5).min() + 1e-9)

    df['hour']         = df['timestamp'].dt.hour
    df['dow']          = df['timestamp'].dt.dayofweek
    df['session_asia'] = ((df['hour'] >= 0) & (df['hour'] < 8)).astype(int)
    df['session_ny']   = ((df['hour'] >= 13) & (df['hour'] < 21)).astype(int)

    return df


# ── Per-pair model training ───────────────────────────────────────────────────

def train_model(symbol: str, df: pd.DataFrame) -> bool:
    """Train dedicated RF+GB ensemble for a specific pair."""
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler

    df = build_features(df.copy())
    df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
    df_c = df.dropna(subset=FEATURE_COLS + ['target']).copy()

    if len(df_c) < 120:
        logger.warning(f"[{symbol}] Not enough data to train: {len(df_c)} rows (need 120+)")
        return False

    X = df_c[FEATURE_COLS].values
    y = df_c['target'].values

    sc = StandardScaler()
    X_s = sc.fit_transform(X)

    rf = RandomForestClassifier(
        n_estimators=200, max_depth=8, min_samples_leaf=15,
        max_features='sqrt', random_state=42, n_jobs=-1
    )
    gb = GradientBoostingClassifier(
        n_estimators=100, max_depth=4, learning_rate=0.05,
        min_samples_leaf=15, random_state=42
    )
    rf.fit(X_s, y)
    gb.fit(X_s, y)

    _models[symbol]  = (rf, gb)
    _scalers[symbol] = sc

    # Init live stats tracker
    if symbol not in _pair_stats:
        _pair_stats[symbol] = {'wins': 0, 'losses': 0, 'signals': 0}

    logger.info(f"[{symbol}] Model trained on {len(df_c)} candles "
                f"(threshold={PAIR_THRESHOLDS.get(symbol, 0.58):.2f})")
    return True


def retrain_all(limit: int = 500):
    """Fetch fresh data and retrain all 6 per-pair models."""
    logger.info("[ENGINE] Starting per-pair model training...")
    success = 0
    for symbol in SYMBOLS:
        df = fetch_okx_candles(symbol, limit=limit)
        if df.empty:
            logger.warning(f"[{symbol}] No data fetched — skipping")
            continue
        if train_model(symbol, df):
            success += 1
    logger.info(f"[ENGINE] Training complete: {success}/{len(SYMBOLS)} pairs ready")


# ── Per-pair signal generation ────────────────────────────────────────────────

def get_signal_for_symbol(symbol: str) -> dict | None:
    """
    Fetch latest candles for one pair, run its dedicated model,
    apply its calibrated threshold. Returns signal dict or None.
    """
    threshold = PAIR_THRESHOLDS.get(symbol, 0.58)

    df = fetch_okx_candles(symbol, limit=300)
    if df.empty or len(df) < 100:
        logger.warning(f"[{symbol}] Insufficient candles: {len(df)}")
        return None

    # Train if this pair has no model yet
    if symbol not in _models:
        ok = train_model(symbol, df)
        if not ok:
            return None

    df = build_features(df.copy())
    df_c = df.dropna(subset=FEATURE_COLS).copy()
    if len(df_c) < 2:
        return None

    # Use second-to-last row — last candle may still be forming
    row         = df_c.iloc[-2]
    latest      = df_c.iloc[-1]

    X = row[FEATURE_COLS].values.reshape(1, -1)
    sc = _scalers[symbol]
    X_s = sc.transform(X)

    rf, gb = _models[symbol]
    rf_prob  = rf.predict_proba(X_s)[0]
    gb_prob  = gb.predict_proba(X_s)[0]
    ens_prob = (rf_prob + gb_prob) / 2
    prob_up  = float(ens_prob[1])

    # Apply pair-specific threshold
    if prob_up >= threshold:
        direction  = "UP"
        confidence = prob_up
    elif prob_up <= (1.0 - threshold):
        direction  = "DOWN"
        confidence = 1.0 - prob_up
    else:
        return None   # Below this pair's quality bar

    # Tier: T1 if volume spike, else T2
    vol_spike = float(row['vol_ratio']) > 1.5
    tier      = "T1" if vol_spike else "T2"

    # Candle timing
    ts          = latest['timestamp']
    candle_open = ts
    candle_close= ts + pd.Timedelta(minutes=15)

    # Margin above threshold (used for cross-pair ranking)
    margin = confidence - threshold

    return {
        'symbol':           symbol,
        'direction':        direction,
        'confidence':       confidence,
        'threshold':        threshold,
        'margin':           margin,        # how far above threshold
        'tier':             tier,
        'vol_spike':        bool(vol_spike),
        'rsi_14':           float(row['rsi_14']),
        'macd_hist':        float(row['macd_hist']),
        'adx':              float(row['adx']),
        'vol_ratio':        float(row['vol_ratio']),
        'open_price':       float(latest['close']),
        'candle_open_time': candle_open.to_pydatetime(),
        'candle_close_time':candle_close.to_pydatetime(),
        'current_price':    float(latest['close']),
    }


# ── Best signal selector ──────────────────────────────────────────────────────

def pick_best_signal(min_confidence: float = None) -> dict | None:
    """
    Evaluate all 6 pairs, return only the single best signal.

    Ranking score = margin_above_threshold + T1_bonus
    This ensures we compare signals fairly across pairs with different
    thresholds — a 61% signal on ETH (58% threshold, margin=0.03) ranks
    higher than a 62% signal on DOGE (65% threshold, margin=-0.03 = skip).

    Anti-spam: only one signal fires per candle, no matter how many qualify.
    """
    candidates = []

    for symbol in SYMBOLS:
        try:
            sig = get_signal_for_symbol(symbol)
            if sig:
                candidates.append(sig)
                logger.info(
                    f"[{symbol}] SIGNAL {sig['direction']} "
                    f"conf={sig['confidence']:.3f} "
                    f"threshold={sig['threshold']:.2f} "
                    f"margin={sig['margin']:.3f} tier={sig['tier']}"
                )
            else:
                logger.debug(f"[{symbol}] No signal this candle")
        except Exception as e:
            logger.error(f"[{symbol}] Signal error: {e}")

    if not candidates:
        logger.info("[ENGINE] No qualifying signals across all pairs this candle")
        return None

    # Rank by margin above threshold + T1 bonus
    def rank_score(s):
        t1_bonus = 0.03 if s['tier'] == 'T1' else 0.0
        return s['margin'] + t1_bonus

    candidates.sort(key=rank_score, reverse=True)
    best = candidates[0]

    logger.info(
        f"[ENGINE] Best signal: {best['symbol']} {best['direction']} "
        f"conf={best['confidence']:.3f} margin={best['margin']:.3f} "
        f"tier={best['tier']} (from {len(candidates)} candidates)"
    )
    return best


# ── Live pair performance tracker ─────────────────────────────────────────────

def record_outcome(symbol: str, outcome: str):
    """Called by scheduler after candle resolves to track per-pair accuracy."""
    if symbol not in _pair_stats:
        _pair_stats[symbol] = {'wins': 0, 'losses': 0, 'signals': 0}
    _pair_stats[symbol]['signals'] += 1
    if outcome == 'WIN':
        _pair_stats[symbol]['wins'] += 1
    elif outcome == 'LOSS':
        _pair_stats[symbol]['losses'] += 1


def get_pair_stats() -> dict:
    """Return live win rate per pair for dashboard display."""
    result = {}
    for sym in SYMBOLS:
        s = _pair_stats.get(sym, {'wins': 0, 'losses': 0, 'signals': 0})
        total = s['wins'] + s['losses']
        result[sym] = {
            'wins':    s['wins'],
            'losses':  s['losses'],
            'signals': s['signals'],
            'win_rate': round(s['wins'] / total * 100, 1) if total > 0 else None,
            'threshold': PAIR_THRESHOLDS.get(sym, 0.58),
        }
    return result
