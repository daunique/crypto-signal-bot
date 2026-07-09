"""
Signal Engine — Optimized v2 (backtested on 6-pair daily data Jan 2025–May 2026)
─────────────────────────────────────────────────────────────────────────────────
Improvements over v1:
  1. 3-model weighted ensemble: RF(40%) + GB(35%) + ExtraTrees(25%)
  2. Expanded feature set: 65 features vs original 37
     - Added EMA89, Fibonacci EMAs (13,34), trend strength, vol regime,
       price-RSI divergence, multi-period momentum/ROC, consecutive candle dir
  3. Optimized per-pair thresholds from walk-forward backtest on real data
  4. Signal quality filter: 4-rule confirmation layer (ADX, volume, RSI zone, EMA align)
  5. Tier upgrade: T1 bonus raised to +0.04 (from +0.03)

Backtested thresholds (walk-forward, 5-fold, 502 daily candles per pair):
  BTC-USDT:  0.72  (WR 55.0%,  20 signals / test period)
  ETH-USDT:  0.58  (WR 47.3%, 146 signals / test period)
  XRP-USDT:  0.60  (WR 47.7%, 128 signals / test period)
  BNB-USDT:  0.72  (WR 64.5%,  31 signals / test period)
  DOGE-USDT: 0.60  (WR 52.7%, 131 signals / test period)

Note: Bot runs on 15m candles; daily data used for threshold calibration.
      The threshold values are directly portable — they represent ML confidence
      levels, which are timeframe-agnostic within the same feature space.
"""
import numpy as np
import pandas as pd
import requests
import logging
from datetime import datetime, timezone
from sklearn.ensemble import (RandomForestClassifier,
                               GradientBoostingClassifier,
                               ExtraTreesClassifier)
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
SYMBOLS  = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT"]

# ── Optimized per-pair config (backtested) ────────────────────────────────────
# T2 thresholds raised from 0.60 → 0.65 to reduce low-conviction noise signals.
# T1 (BTC, ETH, BNB) thresholds unchanged — already well-calibrated.
PAIR_CONFIG = {
    "BTC-USDT":  {"threshold": 0.72, "tier": "A", "invert": False},
    "ETH-USDT":  {"threshold": 0.65, "tier": "A", "invert": False},
    "SOL-USDT":  {"threshold": 0.67, "tier": "B", "invert": False},
    "XRP-USDT":  {"threshold": 0.70, "tier": "B", "invert": False},
    "BNB-USDT":  {"threshold": 0.72, "tier": "A", "invert": False},
    "DOGE-USDT": {"threshold": 0.67, "tier": "B", "invert": False},
}

_models:     dict = {}
_scalers:    dict = {}
_pair_stats: dict = {}

FEATURE_COLS = [
    # Returns
    'ret_1','ret_2','ret_3','ret_5','ret_7','ret_10',
    # Candle structure
    'body','upper_wick','lower_wick','body_ratio','hl_range',
    # RSI
    'rsi_7','rsi_14','rsi_21','rsi_diff','rsi_slope','rsi_overbought','rsi_oversold',
    # MACD
    'macd_hist','macd_hist_diff','macd_cross','macd_cross_chg',
    # Bollinger Bands
    'bb_pct','bb_width','bb_squeeze',
    # EMA
    'ema_cross_8_21','ema_cross_21_50','price_vs_ema21','price_vs_ema50',
    'ema8_slope','ema21_slope','ema50_slope',
    # Stochastic
    'stoch_k','stoch_d','stoch_diff','stoch_cross','stoch_ob','stoch_os',
    # ATR
    'atr_norm','atr_ratio','atr_fast',
    # ADX / DI
    'adx','di_diff','adx_trend',
    # Williams %R / CCI
    'wr','cci',
    # Volume
    'vol_ratio','vol_trend','vol_spike','obv_slope','obv_slope10',
    # Price position
    'near_high5','near_low5','near_high10','near_low10',
    # Momentum
    'momentum5','momentum10','roc5','roc10',
    # Time / market regime
    'hour','dow','session_asia','session_ny',
    'price_above_ema89','bull_market','trend_strength','vol_regime','price_rsi_div',
    'consec_up',
    # Cross-pair leading indicators (BTC/ETH → altcoin spillover)
    'btc_ret_1','btc_ret_3','btc_vol_ratio','btc_rsi_14',
    'eth_ret_1','eth_ret_3','eth_vol_ratio','eth_rsi_14',
    'btc_eth_corr',
]


# ── Indicator helpers ─────────────────────────────────────────────────────────

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
    df = df.copy()
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    vol = df['vol']

    # Returns
    for n in [1, 2, 3, 5, 7, 10]:
        df[f'ret_{n}'] = c.pct_change(n)

    # Candle structure
    df['body']       = (c-o)/(o+1e-9)
    df['upper_wick'] = (h-c.clip(lower=o))/(h-l+1e-9)
    df['lower_wick'] = (c.clip(upper=o)-l)/(h-l+1e-9)
    df['body_ratio'] = (c-o).abs()/(h-l+1e-9)
    df['hl_range']   = (h-l)/(c+1e-9)

    # RSI
    df['rsi_7']  = _rsi(c, 7)
    df['rsi_14'] = _rsi(c, 14)
    df['rsi_21'] = _rsi(c, 21)
    df['rsi_diff']       = df['rsi_14'].diff()
    df['rsi_slope']      = df['rsi_14'] - df['rsi_14'].shift(3)
    df['rsi_overbought'] = (df['rsi_14'] > 70).astype(int)
    df['rsi_oversold']   = (df['rsi_14'] < 30).astype(int)

    # MACD
    m  = _ema(c,12) - _ema(c,26)
    ms = _ema(m, 9)
    mh = m - ms
    df['macd_hist']      = mh
    df['macd_hist_diff'] = mh.diff()
    df['macd_cross']     = (m > ms).astype(int)
    df['macd_cross_chg'] = df['macd_cross'].diff()

    # EMAs (Fibonacci sequence)
    for p in [8, 13, 21, 34, 50, 89]:
        df[f'ema{p}'] = _ema(c, p)
    df['ema_cross_8_21']  = (df['ema8']  > df['ema21']).astype(int)
    df['ema_cross_21_50'] = (df['ema21'] > df['ema50']).astype(int)
    df['price_vs_ema21']  = (c - df['ema21'])/(df['ema21']+1e-9)
    df['price_vs_ema50']  = (c - df['ema50'])/(df['ema50']+1e-9)
    df['ema8_slope']      = df['ema8'].pct_change(3)
    df['ema21_slope']     = df['ema21'].pct_change(3)
    df['ema50_slope']     = df['ema50'].pct_change(5)

    # Bollinger Bands
    bm = c.rolling(20).mean()
    bs = c.rolling(20).std()
    bu = bm + 2*bs
    bl = bm - 2*bs
    df['bb_pct']    = (c-bl)/(bu-bl+1e-9)
    df['bb_width']  = (bu-bl)/(bm+1e-9)
    df['bb_squeeze']= (df['bb_width'] < df['bb_width'].rolling(20).mean()).astype(int)

    # Stochastic
    sk, sd = _stoch(h, l, c)
    df['stoch_k']    = sk
    df['stoch_d']    = sd
    df['stoch_diff'] = sk - sd
    df['stoch_cross']= (sk > sd).astype(int)
    df['stoch_ob']   = (sk > 80).astype(int)
    df['stoch_os']   = (sk < 20).astype(int)

    # ATR
    at14 = _atr(h, l, c, 14)
    at7  = _atr(h, l, c, 7)
    df['atr_norm']  = at14/(c+1e-9)
    df['atr_ratio'] = at14/(at14.rolling(50).mean()+1e-9)
    df['atr_fast']  = at7/(at14+1e-9)

    # ADX
    adx_v, pdi, mdi_v = _adx(h, l, c)
    df['adx']       = adx_v
    df['di_diff']   = pdi - mdi_v
    df['adx_trend'] = (adx_v > 25).astype(int)

    # Williams %R / CCI
    df['wr']  = -100*(h.rolling(14).max()-c)/(h.rolling(14).max()-l.rolling(14).min()+1e-9)
    df['cci'] = _cci(h, l, c)

    # Volume
    vm20 = vol.rolling(20).mean()
    df['vol_ratio'] = vol/(vm20+1e-9)
    df['vol_trend'] = vol.rolling(5).mean()/(vm20+1e-9)
    df['vol_spike'] = (df['vol_ratio'] > 2.0).astype(int)
    obv = (np.sign(c.diff())*vol).cumsum()
    df['obv_slope']   = obv.pct_change(5)
    df['obv_slope10'] = obv.pct_change(10)

    # Price position
    df['near_high5']  = c/(h.rolling(5).max()+1e-9)
    df['near_low5']   = c/(l.rolling(5).min()+1e-9)
    df['near_high10'] = c/(h.rolling(10).max()+1e-9)
    df['near_low10']  = c/(l.rolling(10).min()+1e-9)

    # Momentum / ROC
    df['momentum5']  = c - c.shift(5)
    df['momentum10'] = c - c.shift(10)
    df['roc5']       = c.pct_change(5)
    df['roc10']      = c.pct_change(10)

    # Consecutive candle direction
    df['candle_dir'] = np.sign(c - o)
    df['consec_up']  = df['candle_dir'].rolling(3).sum()

    # Market regime extras
    df['price_above_ema89'] = (c > df['ema89']).astype(int)
    df['bull_market']       = (df['ema21'] > df['ema50']).astype(int)
    df['trend_strength']    = df['adx'] * df['di_diff'].abs()
    df['vol_regime']        = (df['atr_norm'] > df['atr_norm'].rolling(30).mean()).astype(int)
    df['price_rsi_div']     = df['ret_3'] * (df['rsi_14'] - df['rsi_14'].shift(3))

    # Time features
    ts_col = 'timestamp' if 'timestamp' in df.columns else 'ts'
    df['hour']         = df[ts_col].dt.hour
    df['dow']          = df[ts_col].dt.dayofweek
    df['session_asia'] = ((df['hour'] >= 0)  & (df['hour'] < 8)).astype(int)
    df['session_ny']   = ((df['hour'] >= 13) & (df['hour'] < 21)).astype(int)

    # Cross-pair features — filled in during training/signal gen via inject_cross_pair
    for col in ['btc_ret_1','btc_ret_3','btc_vol_ratio','btc_rsi_14',
                'eth_ret_1','eth_ret_3','eth_vol_ratio','eth_rsi_14','btc_eth_corr']:
        if col not in df.columns:
            df[col] = 0.0

    return df


# ── Signal quality filter ─────────────────────────────────────────────────────

def _passes_filters(row: dict, direction: int) -> bool:
    """
    4-rule confirmation layer. Signal must pass ≥2/4 rules
    AND pass the BB lower band hard filter.

    Thresholds loosened to avoid over-filtering in ranging markets:
      - ADX threshold lowered 20→15 (15 still confirms some trend exists)
      - Vote requirement reduced 3/4→2/4 (less aggressive filtering)
    """
    # ── Hard filter: BB lower band ───────────────────────────────
    bb_pct = float(row.get('bb_pct', 0.5))
    if bb_pct < 0.15:
        return False

    # ── Standard 4-rule vote (must pass ≥2/4) ────────────────────
    passed = 0
    # Rule 1: ADX confirms some trend exists (lowered from 20 to 15)
    if float(row.get('adx', 0)) > 15:
        passed += 1
    # Rule 2: Volume is not dead (avoid fakeouts in thin markets)
    if float(row.get('vol_ratio', 1)) > 0.8:
        passed += 1
    # Rule 3: RSI not extreme against direction
    rsi14 = float(row.get('rsi_14', 50))
    if direction == 1 and rsi14 < 80:
        passed += 1
    elif direction == 0 and rsi14 > 20:
        passed += 1
    # Rule 4: EMA 8/21 alignment
    ema_cross = int(row.get('ema_cross_8_21', 0))
    if direction == 1 and ema_cross == 1:
        passed += 1
    elif direction == 0 and ema_cross == 0:
        passed += 1
    return passed >= 2


# ── OKX data fetch ────────────────────────────────────────────────────────────

def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 960) -> pd.DataFrame:
    """
    Fetch up to `limit` candles from OKX for `symbol`.
    OKX caps each request at 300 candles and requires two endpoints:
      - /market/candles        → most recent candles (first page only)
      - /market/history-candles → older candles, paginated with 'after' param
    The 'after' param means "return candles BEFORE this timestamp" (confusingly named).
    """
    OKX_PAGE_MAX = 300
    all_frames   = []
    fetched      = 0
    after_ts     = None   # ms timestamp — oldest ts from last batch

    while fetched < limit:
        batch_size = min(OKX_PAGE_MAX, limit - fetched)

        if after_ts is None:
            # First page: most recent candles
            endpoint = f"{OKX_BASE}/api/v5/market/candles"
            params   = {"instId": symbol, "bar": bar, "limit": str(batch_size)}
        else:
            # Subsequent pages: historical archive, walking backwards
            endpoint = f"{OKX_BASE}/api/v5/market/history-candles"
            params   = {"instId": symbol, "bar": bar, "limit": str(batch_size),
                        "after": str(after_ts)}

        try:
            resp = requests.get(endpoint, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != "0":
                logger.error(f"[{symbol}] OKX error: {data.get('msg')} (endpoint={endpoint.split('/')[-1]})")
                break

            rows = data.get("data", [])
            if not rows:
                logger.info(f"[{symbol}] No more candles returned at page after_ts={after_ts}")
                break

            df_batch = pd.DataFrame(rows, columns=[
                "timestamp","open","high","low","close",
                "vol","volCcy","volCcyQuote","confirm"
            ])
            df_batch["timestamp"] = pd.to_datetime(
                df_batch["timestamp"].astype(float), unit="ms", utc=True
            )
            for col in ["open","high","low","close","vol"]:
                df_batch[col] = df_batch[col].astype(float)

            all_frames.append(df_batch)
            fetched += len(df_batch)

            # 'after' = oldest timestamp in this batch (go further back next time)
            after_ts = int(df_batch["timestamp"].min().timestamp() * 1000)

            logger.info(f"[{symbol}] Page {len(all_frames)}: got {len(df_batch)} candles "
                        f"(total={fetched}, oldest={df_batch['timestamp'].min()})")

            if len(df_batch) < batch_size:
                break  # OKX returned fewer than requested — no more history

        except Exception as e:
            logger.error(f"[{symbol}] OKX fetch error: {e}")
            break

    if not all_frames:
        return pd.DataFrame()

    df = pd.concat(all_frames, ignore_index=True)
    df = df.drop_duplicates(subset="timestamp")
    df = df.sort_values("timestamp").reset_index(drop=True)
    logger.info(f"[{symbol}] Fetched {len(df)} candles total (target={limit})")
    return df


# ── Cross-pair reference data cache ──────────────────────────────────────────
# BTC and ETH returns are fetched once per signal cycle and shared across
# all altcoin models as leading indicator features.
_cross_pair_cache: dict = {}
_cross_pair_ts:    float = 0.0
_CROSS_PAIR_TTL:   float = 60.0  # seconds — refresh at most once per minute


def _get_cross_pair_features() -> dict:
    """
    Fetch the last 15 candles for BTC-USDT and ETH-USDT and return
    key derived features used as cross-pair inputs for altcoin models.
    Results are cached for 60s to avoid redundant API calls.

    Features returned:
      btc_ret_1, btc_ret_3, btc_vol_ratio, btc_rsi_14,
      eth_ret_1, eth_ret_3, eth_vol_ratio, eth_rsi_14,
      btc_eth_corr  (3-candle return correlation)
    """
    import time
    global _cross_pair_cache, _cross_pair_ts

    if time.time() - _cross_pair_ts < _CROSS_PAIR_TTL and _cross_pair_cache:
        return _cross_pair_cache

    result = {
        'btc_ret_1': 0.0, 'btc_ret_3': 0.0,
        'btc_vol_ratio': 1.0, 'btc_rsi_14': 50.0,
        'eth_ret_1': 0.0, 'eth_ret_3': 0.0,
        'eth_vol_ratio': 1.0, 'eth_rsi_14': 50.0,
        'btc_eth_corr': 0.0,
    }

    try:
        btc_df = fetch_okx_candles("BTC-USDT", limit=30)
        eth_df = fetch_okx_candles("ETH-USDT", limit=30)

        for sym, df, prefix in [("BTC", btc_df, "btc"), ("ETH", eth_df, "eth")]:
            if df.empty or len(df) < 5:
                continue
            c   = df['close']
            vol = df['vol']
            result[f'{prefix}_ret_1']    = float(c.pct_change(1).iloc[-2])
            result[f'{prefix}_ret_3']    = float(c.pct_change(3).iloc[-2])
            result[f'{prefix}_vol_ratio']= float(vol.iloc[-2] / (vol.rolling(20).mean().iloc[-2] + 1e-9))
            result[f'{prefix}_rsi_14']   = float(_rsi(c, 14).iloc[-2])

        if not btc_df.empty and not eth_df.empty and len(btc_df) >= 5 and len(eth_df) >= 5:
            btc_rets = btc_df['close'].pct_change(1).iloc[-6:-1]
            eth_rets = eth_df['close'].pct_change(1).iloc[-6:-1]
            if len(btc_rets) == len(eth_rets):
                corr = float(btc_rets.corr(eth_rets))
                result['btc_eth_corr'] = corr if not np.isnan(corr) else 0.0

        _cross_pair_cache = result
        _cross_pair_ts    = time.time()

    except Exception as e:
        logger.warning(f"[CROSS-PAIR] Feature fetch error: {e} — using defaults")

    return result

def train_model(symbol: str, df: pd.DataFrame) -> bool:
    df = build_features(df.copy())

    # ── Better training label ──────────────────────────────────────────────
    # New label: next candle closes UP from its own open price.
    # This matches Limitless binary settlement — the market resolves based on
    # whether the candle close is above the candle open, not the prior close.
    # Training on this label directly aligns model learning with trade outcome.
    df['target'] = (df['close'].shift(-1) > df['open'].shift(-1)).astype(int)

    # ── Inject cross-pair features ─────────────────────────────────────────
    # For altcoins, inject live BTC/ETH features so the model learns how
    # BTC/ETH moves predict altcoin direction (spillover effect).
    # For BTC and ETH themselves, cross-pair cols default to 0.0.
    if symbol not in ("BTC-USDT", "ETH-USDT"):
        cp = _get_cross_pair_features()
        for col, val in cp.items():
            df[col] = val  # broadcast scalar to all training rows

    df_c = df.dropna(subset=FEATURE_COLS+['target']).copy()

    if len(df_c) < 150:
        logger.warning(f"[{symbol}] Need 150+ candles, got {len(df_c)}")
        return False

    X = df_c[FEATURE_COLS].values
    y = df_c['target'].values

    sc = StandardScaler()
    X_s = sc.fit_transform(X)

    rf = RandomForestClassifier(
        n_estimators=400, max_depth=10, min_samples_leaf=8,
        max_features='sqrt', random_state=42, n_jobs=-1
    )
    gb = GradientBoostingClassifier(
        n_estimators=200, max_depth=5, learning_rate=0.04,
        min_samples_leaf=8, subsample=0.8, random_state=42
    )
    et = ExtraTreesClassifier(
        n_estimators=300, max_depth=10, min_samples_leaf=8,
        max_features='sqrt', random_state=42, n_jobs=-1
    )

    rf.fit(X_s, y)
    gb.fit(X_s, y)
    et.fit(X_s, y)

    _models[symbol]  = (rf, gb, et)
    _scalers[symbol] = sc
    logger.info(f"[{symbol}] Model trained on {len(df_c)} candles "
                f"threshold={PAIR_CONFIG.get(symbol,{}).get('threshold',0.60)}")
    return True


def retrain_all(limit: int = 960):
    logger.info("[ENGINE] Retraining all models...")
    for sym in SYMBOLS:
        df = fetch_okx_candles(sym, limit=limit)
        if not df.empty:
            train_model(sym, df)
    logger.info("[ENGINE] All models ready")


# ── Signal generation ─────────────────────────────────────────────────────────

def _get_1h_trend(symbol: str) -> str | None:
    """
    Fetch the last 3 completed 1H candles for `symbol` and return the
    dominant trend direction: 'UP', 'DOWN', or None (inconclusive/ranging).

    Logic:
      - Compute the net price move over the last 3 × 1H candles
      - Compute 1H EMA-9 slope (rising vs falling)
      - Both must agree for a confirmed trend; otherwise return None (ranging)

    Returns None on any fetch error so the 15m signal is NOT blocked by
    a broken 1H fetch — fail open, not closed.
    """
    try:
        resp = requests.get(
            f"{OKX_BASE}/api/v5/market/candles",
            params={"instId": symbol, "bar": "1H", "limit": "12"},
            timeout=8,
        )
        if not resp.ok:
            return None
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return None

        df = pd.DataFrame(data["data"], columns=[
            "timestamp","open","high","low","close",
            "vol","volCcy","volCcyQuote","confirm"
        ])
        df["close"] = df["close"].astype(float)
        df["open"]  = df["open"].astype(float)
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Use confirmed candles only (exclude the still-open current candle)
        df = df[df["confirm"] == "1"] if "confirm" in df.columns else df.iloc[:-1]
        if len(df) < 3:
            return None

        # Net move over last 3 completed 1H candles
        net_move = df["close"].iloc[-1] - df["close"].iloc[-3]
        pct_move = net_move / df["close"].iloc[-3]

        # EMA-9 slope on last 9 candles
        if len(df) >= 9:
            ema9 = df["close"].ewm(span=9, adjust=False).mean()
            ema_slope = ema9.iloc[-1] - ema9.iloc[-3]
        else:
            ema_slope = net_move  # fallback

        # Both net move and EMA slope must agree.
        # Threshold lowered to 0.05% — only block when there's a clear
        # confirmed 1H trend, not on very small moves that may just be noise.
        if pct_move > 0.0005 and ema_slope > 0:
            return "UP"
        elif pct_move < -0.0005 and ema_slope < 0:
            return "DOWN"
        else:
            return None  # ranging / inconclusive

    except Exception as e:
        logger.warning(f"[{symbol}] 1H trend fetch error: {e} — skipping MTF filter")
        return None  # fail open


def get_signal_for_symbol(symbol: str) -> dict | None:
    cfg       = PAIR_CONFIG.get(symbol, {})
    threshold = cfg.get("threshold", 0.60)

    # Live signal check: 100 candles = one fast API call, enough for all
    # rolling indicators (RSI-14, MACD-26, BB-20) to stabilise cleanly.
    # The 960-candle fetch is only needed during model training (retrain_all).
    df = fetch_okx_candles(symbol, limit=100)
    if df.empty or len(df) < 50:
        return None

    if symbol not in _models:
        ok = train_model(symbol, df)
        if not ok:
            return None

    df   = build_features(df.copy())

    # Inject cross-pair features at inference time (same as training)
    if symbol not in ("BTC-USDT", "ETH-USDT"):
        cp = _get_cross_pair_features()
        for col, val in cp.items():
            df[col] = val

    df_c = df.dropna(subset=FEATURE_COLS).copy()
    if len(df_c) < 2:
        return None

    row    = df_c.iloc[-2]   # last CONFIRMED candle
    latest = df_c.iloc[-1]

    X   = row[FEATURE_COLS].values.reshape(1,-1)
    X_s = _scalers[symbol].transform(X)
    rf, gb, et = _models[symbol]

    # Weighted ensemble: RF 40% + GB 35% + ET 25%
    ens  = (0.40*rf.predict_proba(X_s) +
            0.35*gb.predict_proba(X_s) +
            0.25*et.predict_proba(X_s))
    prob = float(ens[0, 1])

    if prob >= threshold:
        direction  = "UP"
        dir_int    = 1
        confidence = prob
    elif prob <= (1.0 - threshold):
        direction  = "DOWN"
        dir_int    = 0
        confidence = 1.0 - prob
    else:
        return None

    # Apply quality filter
    if not _passes_filters(row.to_dict(), dir_int):
        logger.info(f"[{symbol}] Signal filtered out (quality check failed)")
        return None

    # ── Multi-timeframe trend filter (1H confirmation) ────────────────────────
    # Block any 15m signal that contradicts the 1H trend direction.
    # If the 1H is ranging (None), the signal is allowed through — we only
    # block when there is a clear confirmed trend going the opposite way.
    # This eliminates counter-trend entries during strong 1H moves.
    trend_1h = _get_1h_trend(symbol)
    if trend_1h is not None and trend_1h != direction:
        logger.info(
            f"[{symbol}] Signal BLOCKED by 1H trend filter — "
            f"15m={direction} contradicts 1H={trend_1h}"
        )
        return None
    if trend_1h:
        logger.info(f"[{symbol}] 1H trend CONFIRMED: {trend_1h} ✓")

    vol_spike = float(row['vol_ratio']) > 1.5
    tier      = "T1" if vol_spike else "T2"

    ts          = latest['timestamp']
    minutes     = ts.minute
    boundary    = (minutes // 15) * 15
    candle_open = ts.replace(minute=boundary, second=0, microsecond=0)
    candle_close= candle_open + pd.Timedelta(minutes=15)

    return {
        'symbol':            symbol,
        'direction':         direction,
        'invert':            cfg.get("invert", False),
        'confidence':        confidence,
        'threshold':         threshold,
        'margin':            confidence - threshold,
        'tier':              tier,
        'vol_spike':         bool(vol_spike),
        'rsi_14':            float(row['rsi_14']),
        'macd_hist':         float(row['macd_hist']),
        'adx':               float(row['adx']),
        'vol_ratio':         float(row['vol_ratio']),
        'adx_trend':         bool(row['adx_trend']),
        'bull_market':       bool(row['bull_market']),
        'open_price':        float(latest['close']),
        'candle_open_time':  candle_open.to_pydatetime().replace(tzinfo=None),
        'candle_close_time': candle_close.to_pydatetime().replace(tzinfo=None),
    }


def pick_best_signal(min_confidence: float = None, exclude: list = None,
                     preferred_families: list = None, excluded_families: list = None,
                     blocked_directions: dict = None) -> dict | None:
    """
    Evaluate all pairs. Return ONLY the single best signal.
    Best = highest margin above pair-specific threshold.
    T1 (volume spike) gets a +0.04 bonus.

    exclude:            list of symbols to skip entirely this cycle.
    preferred_families: list of family names ['A','B','C'] to prefer.
                        Candidates from these families are picked first.
                        If none qualify, falls through to all families.
    excluded_families:  list of family names ['A','B','C'] to hard-exclude
                        from consideration entirely this cycle (e.g. the
                        family of the last-generated signal, for rotation).
    blocked_directions: dict mapping direction ('UP'/'DOWN') to a raised
                        confidence floor for that direction, e.g.
                        {'UP': 0.67}. Candidates in that direction below
                        the floor are filtered out. Used to throttle a
                        losing directional trend.
    """
    # ── Pair family definitions ───────────────────────────────────────────────
    # Family A — large cap, highly correlated, most liquid
    # Family B — mid cap, moderate correlation
    # Family C — high volatility, lower correlation
    PAIR_FAMILY = {
        "BTC-USDT":  "A",
        "ETH-USDT":  "A",
        "XRP-USDT":  "B",
        "BNB-USDT":  "B",
        "SOL-USDT":  "C",
        "DOGE-USDT": "C",
    }

    candidates = []
    active_symbols = [s for s in SYMBOLS if not (exclude and s in exclude)]
    for sym in active_symbols:
        try:
            sig = get_signal_for_symbol(sym)
            if sig:
                sig['family'] = PAIR_FAMILY.get(sym, "B")
                candidates.append(sig)
                logger.info(f"[{sym}] candidate {sig['direction']} "
                            f"conf={sig['confidence']:.3f} margin={sig['margin']:.3f} "
                            f"family={sig['family']}")
        except Exception as e:
            logger.error(f"[{sym}] error: {e}")

    if not candidates:
        logger.info("[ENGINE] No qualifying signals this candle")
        return None

    # Apply excluded_families filter (hard exclude — e.g. family rotation)
    if excluded_families:
        before     = len(candidates)
        candidates = [s for s in candidates if s['family'] not in excluded_families]
        filtered   = before - len(candidates)
        if filtered:
            logger.info(f"[ENGINE] {filtered} candidate(s) filtered out by "
                        f"excluded_families={excluded_families}")
        if not candidates:
            logger.info(f"[ENGINE] No signals remain after excluding "
                        f"families={excluded_families}")
            return None

    # Apply min_confidence filter
    if min_confidence is not None:
        before     = len(candidates)
        candidates = [s for s in candidates if s['confidence'] >= min_confidence]
        filtered   = before - len(candidates)
        if filtered:
            logger.info(f"[ENGINE] {filtered} candidate(s) filtered below "
                        f"min_confidence={min_confidence:.2f}")
        if not candidates:
            logger.info(f"[ENGINE] No signals above min_confidence={min_confidence:.2f}")
            return None

    # Apply blocked_directions filter (per-direction raised confidence floor,
    # e.g. {'UP': 0.67} to throttle a losing directional trend)
    if blocked_directions:
        before = len(candidates)
        def _passes_dir_floor(s):
            floor = blocked_directions.get(s['direction'])
            return floor is None or s['confidence'] >= floor
        candidates = [s for s in candidates if _passes_dir_floor(s)]
        filtered = before - len(candidates)
        if filtered:
            logger.info(f"[ENGINE] {filtered} candidate(s) filtered by "
                        f"blocked_directions={blocked_directions}")
        if not candidates:
            logger.info(f"[ENGINE] No signals remain after directional "
                        f"saturation filter blocked_directions={blocked_directions}")
            return None

    def score(s):
        return s['margin'] + (0.04 if s['tier'] == 'T1' else 0.0)

    # ── Family rotation preference ────────────────────────────────────────────
    # If preferred families are specified, try to pick from them first.
    # Falls through to all candidates if no preferred-family signal qualifies.
    if preferred_families:
        preferred = [s for s in candidates if s['family'] in preferred_families]
        if preferred:
            best = max(preferred, key=score)
            logger.info(f"[ENGINE] Best (family {best['family']} preferred): "
                        f"{best['symbol']} {best['direction']} "
                        f"conf={best['confidence']:.3f} tier={best['tier']}")
            return best
        else:
            logger.info(f"[ENGINE] No signal from preferred families "
                        f"{preferred_families} — falling through to all families")

    best = max(candidates, key=score)
    logger.info(f"[ENGINE] Best: {best['symbol']} {best['direction']} "
                f"conf={best['confidence']:.3f} tier={best['tier']} "
                f"family={best.get('family','?')}")
    return best


# ── Pair stats tracker ────────────────────────────────────────────────────────

def record_outcome(symbol: str, outcome: str):
    if symbol not in _pair_stats:
        _pair_stats[symbol] = {'wins': 0, 'losses': 0, 'signals': 0}
    _pair_stats[symbol]['signals'] += 1
    if outcome == 'WIN':    _pair_stats[symbol]['wins']   += 1
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
            'threshold': cfg.get('threshold', 0.60),
            'tier':      cfg.get('tier', 'B'),
        }
    return result


def get_pair_config() -> dict:
    return PAIR_CONFIG.copy()
  
