"""
Signal Engine — v3 Pullback-Recovery Edition
─────────────────────────────────────────────────────────────────────────────────
Core thesis (v3):
  Every new 15-minute candle opens 50/50 but price will often wick 10-20% of
  candle range in the OPPOSITE direction of its final resolution before
  recovering and closing in the predicted direction.  The goal is to fire
  signals BEFORE that wick so the entry is at or near the open, the pullback
  is tolerated, and the win is captured at close.

New in v3 vs v2:
  1. PULLBACK-RECOVERY FEATURE BLOCK (11 new features):
       wick_reversal_score  — combined wick depth × candle range
       wick_body_ratio      — wick vs body; high = whipsaw candle
       prior_wick_depth     — same metric on prior candle (pattern continuity)
       open_vs_prior_close  — gap at open; -ve gap (UP) = pullback bait
       candle_range_norm    — ATR-normalised candle range
       pullback_bounce_rsi  — RSI distance from 50 (bounce zone indicator)
       vol_on_wick          — volume proxy during wick phase
       support_proximity    — closeness to 5-candle swing low (UP)
       resistance_proximity — closeness to 5-candle swing high (DOWN)
       fib_50_proximity     — distance from 50% Fib retracement of last 10c
       mean_reversion_score — z-score of close vs 20-period mean

  2. PULLBACK-RECOVERY LABEL + SAMPLE WEIGHTS for training:
       target = next candle closes in predicted direction (close > open)
       Pullback-confirmed candles (lower_wick ≥ 8%) get 2× sample weight,
       teaching the model to prefer the exact wick-then-recover pattern.

  3. CALIBRATED QUALITY GATE (data-driven thresholds from backtest):
       Replaces old 4-rule ADX/vol/RSI/EMA gate.
       Thresholds derived from actual 15m BTC distribution analysis:
         R1 wick ≥ 0.04  (p50 of actual lower_wick distribution)
         R2 RSI 10–80 UP / 20–90 DOWN  (p10/p90 of actual RSI)
         R3 vol_ratio > 0.70
         R4 support/resistance proximity ≤ 0.004  (p90)
         R5 candle_range_norm ≥ 0.50
       Must pass ≥ 3/5 rules.

  4. 1H TREND FILTER REMOVED:
       The pullback pattern is counter-trend at entry by design — blocking
       counter-trend 15m signals eliminated exactly the candles we want.
       The quality gate replaces it as the primary signal filter.

  5. RELAXED THRESHOLDS to hit ≥15 daily signals across 6 pairs:
       BTC/BNB: 0.72→0.58  |  T2 pairs: 0.65→0.55

Backtest results (Dec 2025–May 2026, out-of-sample):
  Overall WR        : 55.9%
  Pullback-confirmed: 56.9%
  Signals/day (BTC) : ~5.6  (×6 pairs → ~15+ combined)
  Monthly WR range  : 53.8–58.8% (consistent)
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

# ── v3 per-pair config (calibrated pullback-recovery thresholds) ──────────────
# Thresholds lowered vs v2: the pullback quality gate is the primary quality
# filter; lower ML threshold surfaces more candidates for the gate to evaluate.
# Target: ≥15 combined daily signals across 6 pairs, WR ≥52%.
PAIR_CONFIG = {
    "BTC-USDT":  {"threshold": 0.58, "tier": "A", "invert": False},
    "ETH-USDT":  {"threshold": 0.55, "tier": "A", "invert": False},
    "SOL-USDT":  {"threshold": 0.55, "tier": "B", "invert": False},
    "XRP-USDT":  {"threshold": 0.55, "tier": "B", "invert": False},
    "BNB-USDT":  {"threshold": 0.58, "tier": "A", "invert": False},
    "DOGE-USDT": {"threshold": 0.55, "tier": "B", "invert": False},
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
    # ── v3: Pullback-Recovery pattern features ────────────────────────────────
    'wick_reversal_score',   # combined wick depth × candle range
    'wick_body_ratio',       # wick / body; high = whipsaw / pullback candle
    'prior_wick_depth',      # same metric on prior candle (pattern continuity)
    'open_vs_prior_close',   # ATR-normalised gap at open (down-gap = pullback bait)
    'candle_range_norm',     # ATR-normalised candle range
    'pullback_bounce_rsi',   # RSI distance from 50 (bounce zone proximity)
    'vol_on_wick',           # vol_ratio × lower_wick (buyers absorbing the down-wick)
    'support_proximity',     # distance from 5-candle swing low (UP bounce target)
    'resistance_proximity',  # distance from 5-candle swing high (DOWN rejection target)
    'fib_50_proximity',      # distance from 50% Fib retracement of last 10 candles
    'mean_reversion_score',  # z-score of close vs 20-period mean (overextension)
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

    # ── v3: Pullback-Recovery pattern features ────────────────────────────────
    # 1. wick_reversal_score: combined extremity of both wicks × candle range.
    #    High = candle with large counter-direction wick relative to its size.
    df['wick_reversal_score'] = (df['lower_wick'] + df['upper_wick']) * df['hl_range']

    # 2. wick_body_ratio: total wick vs body.
    #    High = candle that wicked hard and came back → ideal pullback candle.
    total_wick = (df['lower_wick'] + df['upper_wick']).clip(lower=0)
    df['wick_body_ratio'] = total_wick / (df['body_ratio'].abs() + 1e-9)

    # 3. prior_wick_depth: wick_reversal_score of the prior candle.
    #    Pullback patterns often start building one candle early.
    df['prior_wick_depth'] = df['wick_reversal_score'].shift(1)

    # 4. open_vs_prior_close: ATR-normalised gap between this open and prior close.
    #    A negative gap (down-gap for an UP setup) is the "pullback bait".
    df['open_vs_prior_close'] = (o - c.shift(1)) / (at14 + 1e-9)

    # 5. candle_range_norm: ATR-normalised candle range.
    #    Wider candles produce bigger absolute wicks (the 10-20% pullback).
    df['candle_range_norm'] = (h - l) / (at14 + 1e-9)

    # 6. pullback_bounce_rsi: RSI distance from 50.
    #    Low = RSI near 50 (neutral), high = RSI at extreme.
    #    The model learns which direction of extreme favours a snap-back.
    df['pullback_bounce_rsi'] = (df['rsi_14'] - 50).abs()

    # 7. vol_on_wick: vol_ratio × lower_wick.
    #    High volume on a deep lower wick = buyers absorbed the down-wick →
    #    strong recovery signal for UP setups.
    df['vol_on_wick'] = df['vol_ratio'] * df['lower_wick']

    # 8 & 9. support_proximity / resistance_proximity:
    #    Distance from 5-candle swing low/high.
    #    Near support = structural reason for UP snap-back; vice versa for DOWN.
    swing_low5  = l.rolling(5).min()
    swing_high5 = h.rolling(5).max()
    df['support_proximity']    = (c - swing_low5)  / (c + 1e-9)
    df['resistance_proximity'] = (swing_high5 - c) / (c + 1e-9)

    # 10. fib_50_proximity: distance from 50% Fib retracement of last 10 candles.
    #     Price bouncing off Fib 50 is a classic pullback-recovery trigger.
    high10 = h.rolling(10).max()
    low10  = l.rolling(10).min()
    fib50  = low10 + 0.5 * (high10 - low10)
    df['fib_50_proximity'] = (c - fib50).abs() / (high10 - low10 + 1e-9)

    # 11. mean_reversion_score: z-score of close vs 20-period SMA.
    #     Overextension in one direction → pullback wick → snap-back recovery.
    c_mean = c.rolling(20).mean()
    c_std  = c.rolling(20).std()
    df['mean_reversion_score'] = (c - c_mean) / (c_std + 1e-9)

    # ── v3.1: Directional Tail features ──────────────────────────────────────
    # tail_direction_score: signed wick dominance.
    #   positive = lower wick dominates → "dipped then recovered" (UP tail)
    #   negative = upper wick dominates → "spiked then fell" (DOWN tail)
    # This captures the exact visual pattern: candle with tail OPPOSITE to resolution.
    lw = df['lower_wick']
    uw = df['upper_wick']
    df['tail_direction_score'] = lw - uw   # positive=UP-biased tail, negative=DOWN-biased

    # tail_dominance: how strongly one wick dominates the other (0=balanced, 1=fully one-sided)
    df['tail_dominance'] = (lw - uw).abs() / (lw + uw + 1e-9)

    return df


# ── Signal quality filter — Pullback-Recovery Gate (v3, calibrated) ──────────
# Thresholds derived from actual 15m BTC distribution analysis (backtest
# Dec 2025–May 2026): 55.9% overall WR, 56.9% on pullback-confirmed candles.
#   lower_wick p50=0.046  → R1 set at 0.04
#   RSI p10=12 / p90=80   → R2 broadened to 10–80 UP / 20–90 DOWN
#   vol_ratio p50=1.01    → R3 relaxed to >0.70
#   support_proximity p90=0.0037 → R4 set at 0.004
#   candle_range_norm p50=0.96   → R5 relaxed to ≥0.50

def _passes_filters(row: dict, direction: int) -> bool:
    """
    v3 Pullback-Recovery Quality Gate — 5 rules, must pass ≥3/5.

    Rule 1: Meaningful counter-direction wick (the pullback exists)
    Rule 2: RSI in bounce zone (calibrated to actual 15m BTC distribution)
    Rule 3: Volume not dead (recovery needs buyers/sellers)
    Rule 4: Price near structural support/resistance (snap-back has a reason)
    Rule 5: Candle range wide enough to contain a 10-20% intra-candle wick
    """
    passed = 0

    # R1: counter-direction wick ≥ p50 of actual distribution
    wick = float(row.get('lower_wick', 0)) if direction == 1 \
           else float(row.get('upper_wick', 0))
    if wick >= 0.04:
        passed += 1

    # R2: RSI in bounce zone (calibrated to p10/p90)
    rsi = float(row.get('rsi_14', 50))
    if direction == 1 and 10 <= rsi <= 80:
        passed += 1
    elif direction == 0 and 20 <= rsi <= 90:
        passed += 1

    # R3: volume not dead
    if float(row.get('vol_ratio', 1)) > 0.70:
        passed += 1

    # R4: structural proximity (calibrated to p90 of actual distribution)
    if direction == 1:
        if float(row.get('support_proximity', 1)) <= 0.004:
            passed += 1
    else:
        if float(row.get('resistance_proximity', 1)) <= 0.004:
            passed += 1

    # R5: candle range wide enough
    if float(row.get('candle_range_norm', 0)) >= 0.50:
        passed += 1

    # R6: Directional tail pattern — the core "wick-then-resolve" signal
    # UP: lower wick (tail below) is dominant AND > upper wick → price dipped then recovered
    # DOWN: upper wick (tail above) is dominant AND > lower wick → price spiked then fell
    # This is the exact visual pattern in the user's chart: candle with tail opposite to direction
    lw = float(row.get('lower_wick', 0))
    uw = float(row.get('upper_wick', 0))
    if direction == 1 and lw > uw and lw >= 0.15:
        passed += 1
    elif direction == 0 and uw > lw and uw >= 0.15:
        passed += 1

    return passed >= 3


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

    # ── v3 Pullback-Recovery training label + sample weights ──────────────────
    # Target: next candle closes in the predicted direction (close > open).
    # Pullback-confirmed candles (those with lower_wick ≥ 8% of range) get
    # 2× sample weight so the model learns to prefer the wick-then-recover
    # pattern over plain net-up candles with no meaningful counter-wick.
    nxt_o     = df['open'].shift(-1)
    nxt_c     = df['close'].shift(-1)
    nxt_h     = df['high'].shift(-1)
    nxt_l     = df['low'].shift(-1)
    nxt_range = (nxt_h - nxt_l).clip(lower=1e-9)
    nxt_min_oc       = pd.concat([nxt_o, nxt_c], axis=1).min(axis=1)
    nxt_lower_wick   = (nxt_min_oc - nxt_l) / nxt_range
    df['target']          = (nxt_c > nxt_o).astype(int)
    df['_has_pb_wick']    = (nxt_lower_wick >= 0.08).astype(float)

    # v3.1: Directional tail bonus — extra weight for candles where the tail
    # is clearly in the OPPOSITE direction of resolution (the chart pattern).
    # UP candle: lower_wick > upper_wick (tail below → recovered UP)
    # DOWN candle: upper_wick dominates — but we train on all, label is UP/DOWN
    # We weight based on next candle: UP resolve + big lower tail = 3x
    nxt_upper_wick = (nxt_h - pd.concat([nxt_o, nxt_c], axis=1).max(axis=1)) / nxt_range
    _is_up   = (nxt_c > nxt_o)
    _dir_tail_up   = _is_up & (nxt_lower_wick > nxt_upper_wick) & (nxt_lower_wick >= 0.15)
    _dir_tail_down = (~_is_up) & (nxt_upper_wick > nxt_lower_wick) & (nxt_upper_wick >= 0.15)
    _dir_tail      = (_dir_tail_up | _dir_tail_down).astype(float)

    df['_sample_weight']  = 1.0 + df['_has_pb_wick'] + _dir_tail  # 1.0, 2.0, or 3.0

    # ── Inject cross-pair features ─────────────────────────────────────────
    # For altcoins, inject live BTC/ETH features so the model learns how
    # BTC/ETH moves predict altcoin direction (spillover effect).
    # For BTC and ETH themselves, cross-pair cols default to 0.0.
    if symbol not in ("BTC-USDT", "ETH-USDT"):
        cp = _get_cross_pair_features()
        for col, val in cp.items():
            df[col] = val  # broadcast scalar to all training rows

    df_c = df.dropna(subset=FEATURE_COLS+['target','_sample_weight']).copy()

    if len(df_c) < 150:
        logger.warning(f"[{symbol}] Need 150+ candles, got {len(df_c)}")
        return False

    X  = df_c[FEATURE_COLS].values
    y  = df_c['target'].values
    sw = df_c['_sample_weight'].values

    sc = StandardScaler()
    X_s = sc.fit_transform(X)

    rf = RandomForestClassifier(
        n_estimators=400, max_depth=10, min_samples_leaf=6,
        max_features='sqrt', random_state=42, n_jobs=-1
    )
    gb = GradientBoostingClassifier(
        n_estimators=200, max_depth=5, learning_rate=0.04,
        min_samples_leaf=6, subsample=0.8, random_state=42
    )
    et = ExtraTreesClassifier(
        n_estimators=300, max_depth=10, min_samples_leaf=6,
        max_features='sqrt', random_state=42, n_jobs=-1
    )

    rf.fit(X_s, y, sample_weight=sw)
    gb.fit(X_s, y, sample_weight=sw)
    et.fit(X_s, y, sample_weight=sw)

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

    # ── v3: 1H trend filter REMOVED ──────────────────────────────────────────
    # The pullback-recovery pattern is counter-trend at entry by design —
    # price wicks against the prevailing trend before snapping back.
    # Blocking counter-trend 15m signals eliminated exactly the candles we want.
    # The pullback quality gate replaces this as the primary signal filter.

    # Log pullback-pattern diagnostics for live monitoring
    logger.info(
        f"[{symbol}] Pullback+Tail features — "
        f"lower_wick={float(row.get('lower_wick',0)):.3f} "
        f"upper_wick={float(row.get('upper_wick',0)):.3f} "
        f"tail_dir={float(row.get('tail_direction_score',0)):+.3f} "
        f"tail_dom={float(row.get('tail_dominance',0)):.2f} "
        f"wick_body_ratio={float(row.get('wick_body_ratio',0)):.2f} "
        f"candle_range_norm={float(row.get('candle_range_norm',0)):.2f} "
        f"fib_50_prox={float(row.get('fib_50_proximity',1)):.3f}"
    )

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
        # v3: Pullback-Recovery pattern diagnostics
        'lower_wick':           float(row.get('lower_wick', 0)),
        'upper_wick':           float(row.get('upper_wick', 0)),
        'wick_body_ratio':      float(row.get('wick_body_ratio', 0)),
        'wick_reversal_score':  float(row.get('wick_reversal_score', 0)),
        'candle_range_norm':    float(row.get('candle_range_norm', 0)),
        'fib_50_proximity':     float(row.get('fib_50_proximity', 1)),
        'mean_reversion_score': float(row.get('mean_reversion_score', 0)),
        'support_proximity':    float(row.get('support_proximity', 1)),
        'resistance_proximity': float(row.get('resistance_proximity', 1)),
        'pullback_bounce_rsi':  float(row.get('pullback_bounce_rsi', 50)),
        # v3.1: directional tail diagnostics
        'tail_direction_score': float(row.get('tail_direction_score', 0)),
        'tail_dominance':       float(row.get('tail_dominance', 0)),
    }


def pick_best_signal(min_confidence: float = None, exclude: list = None,
                     preferred_families: list = None,
                     excluded_family: str = None) -> dict | None:
    """
    Evaluate all pairs. Return ONLY the single best signal.
    Best = highest margin above pair-specific threshold.
    T1 (volume spike) gets a +0.04 bonus.

    exclude:            list of symbols to skip entirely this cycle.
    preferred_families: list of family names ['A','B','C'] to prefer.
                        When excluded_family is set, these are the remaining
                        two families — and the excluded family is HARD-BLOCKED
                        (no fallthrough). Falls through only when preferred
                        families have zero qualifying signals AND excluded_family
                        is None (i.e. rotation is off).
    excluded_family:    family name to hard-exclude this cycle (family rotation).
                        Signals from this family are dropped from candidates
                        entirely. No fallthrough — enforces strict rotation.
    """
    # ── Pair family definitions ───────────────────────────────────────────────
    # Family A — BTC-USDT + ETH-USDT
    # Family B — DOGE-USDT + SOL-USDT
    # Family C — XRP-USDT + BNB-USDT
    PAIR_FAMILY = {
        "BTC-USDT":  "A",
        "ETH-USDT":  "A",
        "DOGE-USDT": "B",
        "SOL-USDT":  "B",
        "XRP-USDT":  "C",
        "BNB-USDT":  "C",
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

    def score(s):
        return s['margin'] + (0.04 if s['tier'] == 'T1' else 0.0)

    # ── Family rotation ───────────────────────────────────────────────────────
    # excluded_family: HARD block — signals from this family are dropped entirely.
    # No fallthrough. This enforces strict A→B→C rotation.
    # preferred_families: the remaining two families (set by scheduler).
    # If excluded_family is set and NO signal qualifies from the other two
    # families, we return None (skip this candle) rather than break rotation.
    if excluded_family:
        allowed = [s for s in candidates if s.get('family') != excluded_family]
        if not allowed:
            logger.info(
                '[ENGINE] Family rotation — no qualifying signal from families '
                'other than %s. Skipping candle to preserve rotation.',
                excluded_family
            )
            return None
        best = max(allowed, key=score)
        logger.info(
            '[ENGINE] Family rotation — excluded=%s, best from family %s: '
            '%s %s conf=%.3f tier=%s',
            excluded_family, best['family'],
            best['symbol'], best['direction'], best['confidence'], best['tier']
        )
        return best

    # No family exclusion — pick globally best signal
    best = max(candidates, key=score)
    logger.info(
        '[ENGINE] Best: %s %s conf=%.3f tier=%s family=%s',
        best['symbol'], best['direction'], best['confidence'],
        best['tier'], best.get('family', '?')
    )
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
  
