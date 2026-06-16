"""
signal_engine.py — ML Signal Engine
═══════════════════════════════════════════════════════════════════════════════
SYSTEM ROLE
───────────
This module is the brain of btcbot. It is imported by three consumers:

  1. main.py / wsgi.py  → calls retrain_all(limit=300) once at startup in a
                          daemon thread to warm up all 6 models before the
                          first scheduler tick arrives.

  2. scheduler.py       → calls:
       • pick_best_signal(...)  every :00/:15/:30/:45+1s  (generate job)
       • fetch_okx_candles(sym) every :00/:15/:30/:45+0s  (resolve job — gets
                                                           exact OKX OHLC for
                                                           WIN/LOSS verdict)
       • record_outcome(sym, outcome)  after each resolve  (per-pair tracker)
       • retrain_all(limit=960)  every 4 hours             (retrain job)
       • _models dict / SYMBOLS  (readiness guard in _models_ready())

  3. app.py             → calls:
       • get_pair_stats()    for /api/stats/pairs route
       • get_pair_config()   for /api/stats/pairs route
       • SYMBOLS             for /api/prices live-price fetcher

SIGNAL FLOW (per 15-minute candle boundary)
───────────────────────────────────────────
  :XX:00  scheduler.resolve fires   → resolves previous PENDING signal
  :XX:01  scheduler.generate fires  → calls pick_best_signal()
                                        ↓
                                      fetch_okx_candles(sym, limit=120)
                                        for each SYMBOL
                                        ↓
                                      _build_features(df)   → 40+ indicators
                                        ↓
                                      _models[sym].predict_proba(X[-1])
                                        → bull_prob, bear_prob
                                        ↓
                                      apply PAIR_CONFIG thresholds + filters
                                        ↓
                                      rank candidates by confidence
                                      apply family / exclude / direction filters
                                        ↓
                                      return best signal dict  (or None)

PAIR_CONFIG  (one entry per symbol)
────────────────────────────────────
  threshold   float   minimum ML confidence to fire a signal
  tier        str     "T1" (vol spike ≥1.5×) or "T2" (standard)
  family      str     "A" | "B" | "C"  — used by family-rotation in scheduler
  invert      bool    when True the trade direction is flipped by scheduler.py
                      (signal_direction stored as raw ML direction; scheduler
                       reads sig['invert'] and flips before placing the order)
  weight      float   tie-break multiplier when two candidates have equal conf

PUBLIC API
──────────
  SYMBOLS             list[str]   the 6 trading pairs
  _models             dict        {sym: fitted Pipeline}  — inspected by
                                  scheduler._models_ready()
  fetch_okx_candles(sym, limit)   → pd.DataFrame  (OHLCV + timestamp)
  retrain_all(limit)              → None  (fits all 6 models)
  pick_best_signal(...)           → dict | None
  record_outcome(sym, outcome)    → None  (updates in-memory win tracker)
  get_pair_stats()                → dict  {sym: {wins, losses, win_rate}}
  get_pair_config()               → dict  {sym: PAIR_CONFIG entry}

DEBUGGING NOTES
───────────────
  • If _models_ready() returns False at generate time, the generate job skips
    the candle. This happens only during the first ~30s after cold start while
    retrain_all runs in _bg() thread (main.py).

  • fetch_okx_candles returns an EMPTY DataFrame on OKX error. The resolve job
    retries up to 5× (1s apart) before giving up on a symbol for that tick.

  • _build_features drops NaN rows before returning X. With limit=120 the
    first ~30 rows may be NaN-heavy (EMA200 needs 200 bars — use limit≥300
    for training, 120 is fine for live inference since models were trained on
    300+ bars and EMAs are already warm from prior candles).

  • record_outcome / get_pair_stats use the in-memory _pair_outcomes dict.
    This resets on process restart. The authoritative win/loss counts are in
    the Signal DB table (queried directly by app.py's /api/stats/pairs route).

  • DOGE-USDT, ETH-USDT: both are in Settings.no_execute_pairs by default
    (v3.4). They generate signals, resolve outcomes, and appear in stats/
    dashboard tracking but NEVER place a live order (scheduler.py
    _is_no_execute_pair guard). Re-activating either requires removing the
    symbol from no_execute_pairs in the Settings dashboard.
    DOGE: excluded from live execution in v3.4 (walk-forward WR 52.6% — below
          live-execution confidence threshold).
    ETH:  excluded from live execution in v3.4 (walk-forward WR 59.1% — model
          quality acceptable but daily WR variance too high for live orders).

  • XRP-USDT: LIVE — re-enabled for live order placement in v3.4.
    invert=False (v3.3 — inverse direction removed and kept off).
    Signals fire, resolve, and place real orders on Limitless + Polymarket.
    family=C, tier=T1, threshold=0.58, weight=1.1.

  • Family rotation lives entirely in scheduler.py. signal_engine only
    exposes sig['family'] and PAIR_CONFIG so the scheduler can enforce it.

  • The invert flag lives in PAIR_CONFIG and is forwarded through the signal
    dict as sig['invert']. The actual direction flip is done in scheduler.py
    just before execute_order() is called — signal_direction in the DB always
    stores the RAW ML direction, never the flipped one.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

OKX_BASE = "https://www.okx.com"
OKX_CANDLES_EP = f"{OKX_BASE}/api/v5/market/candles"

SYMBOLS = [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "BNB-USDT",
    "DOGE-USDT",
]

# ── Per-pair configuration ────────────────────────────────────────────────────
# threshold : minimum ML probability for the winning direction to fire a signal
# tier      : used for cooldown duration in scheduler (T1=1 candle, T2=2 candles)
# family    : A/B/C — family rotation in scheduler prevents back-to-back same family
# invert    : when True, scheduler flips UP→DOWN / DOWN→UP before order placement
#             XRP-USDT: invert=False (v3.3 — inverse signal direction removed)
# weight    : tie-break multiplier; higher = preferred when confidences are equal
# ─────────────────────────────────────────────────────────────────────────────
PAIR_CONFIG = {
    "BTC-USDT": {
        "threshold": 0.58,
        "tier":      "T2",
        "family":    "A",
        "invert":    False,
        "weight":    1.0,
    },
    "ETH-USDT": {
        "threshold": 0.58,
        "tier":      "T2",
        "family":    "A",
        "invert":    False,
        "weight":    1.0,
    },
    "SOL-USDT": {
        "threshold": 0.57,
        "tier":      "T2",
        "family":    "B",
        "invert":    False,
        "weight":    1.05,
    },
    "DOGE-USDT": {
        "threshold": 0.57,
        "tier":      "T2",
        "family":    "B",
        "invert":    False,
        "weight":    1.0,
    },
    # XRP-USDT:
    #   invert=False  — raw ML direction is traded as-is (v3.3: inverse removed)
    #   no_execute    — live orders suppressed via Settings.no_execute_pairs
    #                   (signal fires + resolves normally, order never placed)
    "XRP-USDT": {
        "threshold": 0.58,
        "tier":      "T1",
        "family":    "C",
        "invert":    False,   # ← v3.3: inverse direction FALSE
        "weight":    1.1,
    },
    "BNB-USDT": {
        "threshold": 0.57,
        "tier":      "T2",
        "family":    "C",
        "invert":    False,
        "weight":    1.05,
    },
}

# Volume-spike threshold for T1 tier upgrade
VOL_SPIKE_MULTIPLIER = 1.5

# ══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY STATE
# ══════════════════════════════════════════════════════════════════════════════

# Fitted model pipelines — one per symbol.
# scheduler._models_ready() checks len(_models) >= len(SYMBOLS).
_models: dict = {}

# Per-pair win/loss tracker (resets on restart; DB is the source of truth).
_pair_outcomes: dict = {sym: {"wins": 0, "losses": 0} for sym in SYMBOLS}


# ══════════════════════════════════════════════════════════════════════════════
# OKX DATA FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_okx_candles(symbol: str, limit: int = 120) -> pd.DataFrame:
    """
    Fetch 15-minute OHLCV candles from OKX for *symbol*.

    Returns a DataFrame with columns:
        timestamp (UTC-aware Timestamp) | open | high | low | close | volume

    Returns an EMPTY DataFrame on any error.
    The scheduler resolve job retries this up to 5× to let OKX publish
    the just-closed candle before giving up.

    OKX candle row format (data[0] = most recent):
        [ts_ms, open, high, low, close, vol_base, vol_quote, ...]
    Rows are returned newest-first; we reverse to chronological order.
    """
    try:
        resp = requests.get(
            OKX_CANDLES_EP,
            params={"instId": symbol, "bar": "15m", "limit": str(limit)},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            logger.warning("[SE] fetch_okx_candles: empty data for %s", symbol)
            return pd.DataFrame()

        rows = []
        for row in reversed(data):          # oldest → newest
            rows.append({
                "timestamp": pd.Timestamp(int(row[0]), unit="ms", tz="UTC"),
                "open":      float(row[1]),
                "high":      float(row[2]),
                "low":       float(row[3]),
                "close":     float(row[4]),
                "volume":    float(row[5]),
            })

        df = pd.DataFrame(rows)
        return df

    except Exception as exc:
        logger.error("[SE] fetch_okx_candles %s error: %s", symbol, exc)
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE ENGINEERING  (40+ indicators)
# ══════════════════════════════════════════════════════════════════════════════

def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all technical indicators on the OHLCV DataFrame and return
    a feature matrix (one row per candle, NaN rows dropped).

    Indicators computed
    ───────────────────
    Trend / Moving averages:
        EMA 8, 13, 21, 34, 55, 89, 200
        EMA ribbon bull/bear score
        Price position vs EMA21, EMA55, EMA200

    Momentum:
        RSI 14
        MACD (12,26,9): line, signal, histogram
        Stochastic (14,3): %K, %D
        Williams %R 14
        CCI 20
        ROC 3, 8, 21

    Volatility:
        ATR 14  (absolute + normalised as % of price)
        Bollinger Bands (20,2): upper, mid, lower, %B, width
        BB squeeze rank (rolling 50)

    Volume:
        Volume Z-score (20-period)
        Volume ratio vs 20-period MA
        Volume spike flag (≥1.5× MA)

    Trend strength:
        ADX 14, +DI, -DI
        ADX regime bins

    Candle structure:
        Body size, range, body%
        Heikin-Ashi close, HA trend

    Composite scores:
        bull_score (0–8)
        bear_score (0–8)

    Target (training only):
        label: 1 if next candle close > open, else 0
    """
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]
    o = df["open"]

    # ── EMAs ──────────────────────────────────────────────────────────────────
    for p in [8, 13, 21, 34, 55, 89, 200]:
        df[f"ema{p}"] = c.ewm(span=p, adjust=False).mean()

    df["ema_bull"] = (
        (df["ema8"] > df["ema21"]) & (df["ema21"] > df["ema55"])
    ).astype(int)
    df["ema_bear"] = (
        (df["ema8"] < df["ema21"]) & (df["ema21"] < df["ema55"])
    ).astype(int)
    df["above_ema21"]  = (c > df["ema21"]).astype(int)
    df["above_ema55"]  = (c > df["ema55"]).astype(int)
    df["above_ema200"] = (c > df["ema200"]).astype(int)

    # EMA distance features (normalised)
    df["dist_ema21"]  = (c - df["ema21"])  / (df["ema21"]  + 1e-9)
    df["dist_ema55"]  = (c - df["ema55"])  / (df["ema55"]  + 1e-9)
    df["dist_ema200"] = (c - df["ema200"]) / (df["ema200"] + 1e-9)

    # ── RSI 14 ────────────────────────────────────────────────────────────────
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / (loss + 1e-9))

    # ── MACD (12, 26, 9) ──────────────────────────────────────────────────────
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd"]   = ema12 - ema26
    df["macd_s"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_h"] = df["macd"] - df["macd_s"]
    df["macd_h_rising"] = (df["macd_h"] > df["macd_h"].shift(1)).astype(int)

    # ── Bollinger Bands (20, 2) ───────────────────────────────────────────────
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    df["bb_upper"] = bb_mid + 2 * bb_std
    df["bb_lower"] = bb_mid - 2 * bb_std
    df["bb_mid"]   = bb_mid
    df["bb_pct"]   = (c - bb_mid) / (2 * bb_std + 1e-9)
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / (bb_mid + 1e-9)
    # BB squeeze: current width vs rolling 50-period percentile rank
    df["bb_squeeze_rank"] = df["bb_width"].rolling(50).rank(pct=True)

    # ── ATR 14 ────────────────────────────────────────────────────────────────
    tr = pd.concat(
        [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    df["atr"]     = tr.ewm(span=14, adjust=False).mean()
    df["atr_pct"] = df["atr"] / (c + 1e-9)          # normalised ATR

    # ── ADX / +DI / -DI (14) ─────────────────────────────────────────────────
    plus_dm  = np.where(
        (h.diff() > l.diff().abs()) & (h.diff() > 0), h.diff(), 0
    )
    minus_dm = np.where(
        (l.diff().abs() > h.diff()) & (l.diff() < 0), l.diff().abs(), 0
    )
    plus_di  = (
        100
        * pd.Series(plus_dm,  index=df.index).ewm(span=14, adjust=False).mean()
        / (df["atr"] + 1e-9)
    )
    minus_di = (
        100
        * pd.Series(minus_dm, index=df.index).ewm(span=14, adjust=False).mean()
        / (df["atr"] + 1e-9)
    )
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    df["adx"]      = dx.ewm(span=14, adjust=False).mean()
    df["plus_di"]  = plus_di
    df["minus_di"] = minus_di
    df["di_diff"]  = plus_di - minus_di            # +DI − −DI directional bias

    # ── Stochastic (14, 3) ────────────────────────────────────────────────────
    lo14  = l.rolling(14).min()
    hi14  = h.rolling(14).max()
    df["stoch_k"] = 100 * (c - lo14) / (hi14 - lo14 + 1e-9)
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()
    df["stoch_cross_bull"] = (
        (df["stoch_k"] > df["stoch_d"]) &
        (df["stoch_k"].shift(1) <= df["stoch_d"].shift(1))
    ).astype(int)
    df["stoch_cross_bear"] = (
        (df["stoch_k"] < df["stoch_d"]) &
        (df["stoch_k"].shift(1) >= df["stoch_d"].shift(1))
    ).astype(int)

    # ── Williams %R 14 ───────────────────────────────────────────────────────
    df["willr"] = -100 * (hi14 - c) / (hi14 - lo14 + 1e-9)

    # ── CCI 20 ───────────────────────────────────────────────────────────────
    tp    = (h + l + c) / 3
    tp_ma = tp.rolling(20).mean()
    tp_md = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    df["cci"] = (tp - tp_ma) / (0.015 * tp_md + 1e-9)

    # ── Rate of Change ────────────────────────────────────────────────────────
    df["roc3"]  = c.pct_change(3)
    df["roc8"]  = c.pct_change(8)
    df["roc21"] = c.pct_change(21)

    # ── Volume ───────────────────────────────────────────────────────────────
    vol_ma       = v.rolling(20).mean()
    vol_std      = v.rolling(20).std()
    df["vol_z"]     = (v - vol_ma) / (vol_std + 1e-9)
    df["vol_ratio"] = v / (vol_ma + 1e-9)
    df["vol_spike"] = (df["vol_ratio"] >= VOL_SPIKE_MULTIPLIER).astype(int)

    # ── Candle structure ──────────────────────────────────────────────────────
    df["body"]     = (c - o).abs()
    df["range"]    = h - l
    df["body_pct"] = df["body"] / (df["range"] + 1e-9)
    df["is_bull_candle"] = (c > o).astype(int)

    # ── Heikin-Ashi ───────────────────────────────────────────────────────────
    df["ha_close"] = (o + h + l + c) / 4
    df["ha_trend"] = (df["ha_close"] > df["ha_close"].shift(2)).astype(int) * 2 - 1

    # ── Composite confluence scores (0–8) ─────────────────────────────────────
    df["bull_score"] = (
        (df["rsi"]     > 50).astype(int)
        + (df["macd"]  > df["macd_s"]).astype(int)
        + df["macd_h_rising"]
        + df["ema_bull"]
        + df["above_ema21"]
        + (df["stoch_k"] > df["stoch_d"]).astype(int)
        + (df["vol_z"]   > 0.5).astype(int)
        + (df["ha_trend"] > 0).astype(int)
    )
    df["bear_score"] = (
        (df["rsi"]     < 50).astype(int)
        + (df["macd"]  < df["macd_s"]).astype(int)
        + (df["macd_h"] < df["macd_h"].shift(1)).astype(int)
        + df["ema_bear"]
        + (1 - df["above_ema21"])
        + (df["stoch_k"] < df["stoch_d"]).astype(int)
        + (df["vol_z"]   > 0.5).astype(int)
        + (df["ha_trend"] < 0).astype(int)
    )

    # ── Feature column list (all non-OHLCV, non-timestamp) ───────────────────
    FEATURE_COLS = [
        # EMA
        "ema_bull", "ema_bear", "above_ema21", "above_ema55", "above_ema200",
        "dist_ema21", "dist_ema55", "dist_ema200",
        # Momentum
        "rsi", "macd", "macd_s", "macd_h", "macd_h_rising",
        "stoch_k", "stoch_d", "stoch_cross_bull", "stoch_cross_bear",
        "willr", "cci",
        "roc3", "roc8", "roc21",
        # Volatility
        "atr", "atr_pct",
        "bb_pct", "bb_width", "bb_squeeze_rank",
        # Trend
        "adx", "plus_di", "minus_di", "di_diff",
        # Volume
        "vol_z", "vol_ratio", "vol_spike",
        # Candle
        "body_pct", "is_bull_candle", "ha_trend",
        # Composite
        "bull_score", "bear_score",
    ]

    out = df[FEATURE_COLS].copy()
    out = out.dropna()
    return out


# ══════════════════════════════════════════════════════════════════════════════
# MODEL TRAINING
# ══════════════════════════════════════════════════════════════════════════════

def _make_pipeline() -> Pipeline:
    """
    Ensemble: RandomForest + GradientBoosting stacked via soft-voting average.
    Wrapped in a single sklearn Pipeline (scaler → ensemble wrapper).

    We use a lightweight custom ensemble rather than VotingClassifier so we
    can expose predict_proba on averaged probabilities without needing to
    duplicate the scaler for each sub-estimator.
    """
    return _EnsemblePipeline()


class _EnsemblePipeline:
    """
    Thin wrapper that behaves like an sklearn Pipeline:
      .fit(X, y)
      .predict_proba(X)  → shape (n, 2)  [prob_down, prob_up]

    Internally: StandardScaler → [RandomForest, GradientBoosting]
    Probabilities are averaged (equal weight).
    """

    def __init__(self):
        self.scaler = StandardScaler()
        self.rf = RandomForestClassifier(
            n_estimators=120,
            max_depth=8,
            min_samples_leaf=10,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        )
        self.gb = GradientBoostingClassifier(
            n_estimators=120,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            min_samples_leaf=10,
            random_state=42,
        )
        self._is_fitted = False

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_EnsemblePipeline":
        Xs = self.scaler.fit_transform(X)
        self.rf.fit(Xs, y)
        self.gb.fit(Xs, y)
        self._is_fitted = True
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if not self._is_fitted:
            raise RuntimeError("Model not fitted — call retrain_all() first")
        Xs = self.scaler.transform(X)
        p_rf = self.rf.predict_proba(Xs)
        p_gb = self.gb.predict_proba(Xs)
        # Average probabilities
        return (p_rf + p_gb) / 2.0


def retrain_all(limit: int = 300) -> None:
    """
    Fetch *limit* candles for every symbol, compute features, train the
    ensemble, and store the fitted pipeline in _models[sym].

    Called:
      • Once at startup (main.py _bg thread) with limit=300
      • Every 4 hours by scheduler.job_retrain() with limit=960

    With limit=300 the first ~30 rows are NaN-heavy (long EMAs). After
    dropna the training set is typically 270–280 samples — enough to fit
    the ensemble robustly. With limit=960 (~10 days) the model captures
    intraday regime shifts more accurately.

    Labels: 1 if next candle is a bull candle (close > open), else 0.
    We use NEXT candle's direction because the signal fires at the OPEN
    of the next candle and resolves at its CLOSE.
    """
    logger.info("[SE] retrain_all — fetching %d candles per symbol", limit)
    for sym in SYMBOLS:
        try:
            df = fetch_okx_candles(sym, limit=limit)
            if df.empty or len(df) < 60:
                logger.warning("[SE] retrain %s: insufficient data (%d rows)", sym, len(df))
                continue

            feats = _build_features(df)
            if feats.empty:
                logger.warning("[SE] retrain %s: no features after dropna", sym)
                continue

            # Align features with next-candle target
            # feats index matches df index after dropna; shift target by -1
            # so we predict the NEXT candle direction.
            # We must use the original df['close'] and df['open'] for labels.
            feat_idx = feats.index
            labels_raw = np.sign(df["close"] - df["open"]).replace(0, np.nan)
            # next-candle label: shift so row i predicts candle i+1
            labels_next = labels_raw.shift(-1)
            labels_aligned = labels_next.loc[feat_idx].dropna()

            # Keep only rows where both feature AND label are available
            common_idx = feats.index.intersection(labels_aligned.index)
            if len(common_idx) < 50:
                logger.warning(
                    "[SE] retrain %s: too few aligned samples (%d)", sym, len(common_idx)
                )
                continue

            X = feats.loc[common_idx].values
            y = (labels_aligned.loc[common_idx].values > 0).astype(int)   # 1=UP, 0=DOWN

            model = _make_pipeline()
            model.fit(X, y)
            _models[sym] = model

            # Class balance info
            up_pct = y.mean() * 100
            logger.info(
                "[SE] retrain %s ✓ — %d samples | UP=%.1f%% DOWN=%.1f%%",
                sym, len(y), up_pct, 100 - up_pct,
            )

        except Exception as exc:
            logger.error("[SE] retrain %s FAILED: %s", sym, exc, exc_info=True)

    logger.info("[SE] retrain_all complete — %d/%d models ready", len(_models), len(SYMBOLS))


# ══════════════════════════════════════════════════════════════════════════════
# CANDLE BOUNDARY UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _current_candle_open(now: datetime) -> datetime:
    """
    Return the UTC open time of the current 15-minute candle.
    E.g. 14:07 UTC → 14:00 UTC;  14:23 UTC → 14:15 UTC.
    Result is timezone-naive (matches DB storage convention).
    """
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    minute_floor = (now.minute // 15) * 15
    return now.replace(minute=minute_floor, second=0, microsecond=0)


def _candle_close(candle_open: datetime) -> datetime:
    """Return the close time (= open + 15 min) for a candle open datetime."""
    return candle_open + timedelta(minutes=15)


# ══════════════════════════════════════════════════════════════════════════════
# LIVE SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _score_symbol(sym: str) -> dict | None:
    """
    Fetch the latest candles for *sym*, run the ensemble, and return a
    candidate signal dict if the model confidence clears the pair threshold.

    Returns None if:
      • no trained model for sym
      • OKX fetch fails or returns < 10 rows
      • feature build fails
      • max(bull_prob, bear_prob) < pair threshold

    Returned dict keys:
      symbol, direction, confidence, family, invert, tier,
      rsi_14, macd_hist, adx, vol_ratio,
      open_price, candle_open_time, candle_close_time
    """
    model = _models.get(sym)
    if model is None:
        return None

    df = fetch_okx_candles(sym, limit=120)
    if df.empty or len(df) < 10:
        logger.debug("[SE] score %s: no/insufficient candle data", sym)
        return None

    feats = _build_features(df)
    if feats.empty:
        logger.debug("[SE] score %s: empty feature frame", sym)
        return None

    try:
        X_live = feats.iloc[[-1]].values          # last row only
        proba  = model.predict_proba(X_live)[0]   # [prob_down, prob_up]
    except Exception as exc:
        logger.warning("[SE] score %s predict error: %s", sym, exc)
        return None

    prob_up   = float(proba[1])
    prob_down = float(proba[0])
    confidence = max(prob_up, prob_down)
    direction  = "UP" if prob_up >= prob_down else "DOWN"

    cfg = PAIR_CONFIG[sym]
    if confidence < cfg["threshold"]:
        logger.debug(
            "[SE] score %s: conf=%.3f below threshold=%.3f — skip",
            sym, confidence, cfg["threshold"],
        )
        return None

    # ── Candle timing ─────────────────────────────────────────────────────────
    now          = datetime.now(timezone.utc).replace(tzinfo=None)
    candle_open  = _current_candle_open(now)
    candle_close = _candle_close(candle_open)

    # ── Open price: last candle's open from OKX data ──────────────────────────
    open_price = float(df["open"].iloc[-1])

    # ── Tier: T1 if vol spike present ────────────────────────────────────────
    last_vol_ratio = float(feats["vol_ratio"].iloc[-1])
    tier = "T1" if last_vol_ratio >= VOL_SPIKE_MULTIPLIER else cfg["tier"]

    # ── Extract indicator snapshot for DB storage ─────────────────────────────
    last_feat = feats.iloc[-1]
    rsi_14    = float(last_feat.get("rsi",       np.nan))
    macd_hist = float(last_feat.get("macd_h",    np.nan))
    adx_val   = float(last_feat.get("adx",       np.nan))
    vol_ratio = float(last_feat.get("vol_ratio", np.nan))

    return {
        "symbol":           sym,
        "direction":        direction,
        "confidence":       confidence,
        "family":           cfg["family"],
        "invert":           cfg["invert"],   # XRP: False (v3.3)
        "tier":             tier,
        "rsi_14":           rsi_14,
        "macd_hist":        macd_hist,
        "adx":              adx_val,
        "vol_ratio":        vol_ratio,
        "open_price":       open_price,
        "candle_open_time": candle_open,
        "candle_close_time": candle_close,
        # weight is used internally for ranking
        "_weight":          cfg["weight"],
    }


def pick_best_signal(
    min_confidence: float = 0.0,
    exclude: list | None = None,
    preferred_families: list | None = None,
    excluded_families: list | None = None,
    blocked_directions: dict | None = None,
) -> dict | None:
    """
    Score all 6 symbols and return the single best signal for this candle.

    Called by scheduler.job_generate_signal() every :00/:15/:30/:45+1s.

    Parameters
    ──────────
    min_confidence    : global floor (Settings.min_confidence); pair thresholds
                        in PAIR_CONFIG are the primary gates — this is an
                        additional global override (0.0 = disabled).
    exclude           : list of symbols to skip entirely this candle
                        (pair cooldown list from scheduler).
    preferred_families: families to prefer; candidates NOT in this list are
                        ranked lower (family rotation from scheduler).
    excluded_families : families to exclude entirely (family rotation).
    blocked_directions: {direction: floor} — if a direction's confidence is
                        below *floor* it is blocked (Rule 2 saturation filter).

    Ranking logic
    ─────────────
    1. Score all pairs → collect candidates above threshold
    2. Drop excluded symbols and excluded families
    3. Apply blocked_directions floor per-direction
    4. Apply global min_confidence floor
    5. Sort by: preferred_family first, then conf × weight descending
    6. Return the top candidate (or None)

    Returns a signal dict (see _score_symbol) or None.
    """
    exclude           = exclude           or []
    excluded_families = excluded_families or []
    blocked_directions = blocked_directions or {}

    candidates = []
    for sym in SYMBOLS:
        if sym in exclude:
            logger.debug("[SE] pick: skipping %s (pair cooldown)", sym)
            continue

        candidate = _score_symbol(sym)
        if candidate is None:
            continue

        fam = candidate["family"]
        if fam in excluded_families:
            logger.debug("[SE] pick: skipping %s family=%s (rotation)", sym, fam)
            continue

        # Global min_confidence override
        if min_confidence and candidate["confidence"] < min_confidence:
            logger.debug(
                "[SE] pick: %s conf=%.3f < global min_conf=%.3f — skip",
                sym, candidate["confidence"], min_confidence,
            )
            continue

        # Rule 2 directional saturation floor
        direction = candidate["direction"]
        dir_floor = blocked_directions.get(direction, 0.0)
        if dir_floor and candidate["confidence"] < dir_floor:
            logger.info(
                "[SE] pick: %s %s conf=%.3f blocked by Rule2 floor=%.2f",
                sym, direction, candidate["confidence"], dir_floor,
            )
            continue

        candidates.append(candidate)

    if not candidates:
        logger.info("[SE] pick_best_signal: no qualifying candidates this candle")
        return None

    # ── Sort: preferred family first, then weighted confidence ───────────────
    def _rank_key(c):
        in_preferred = 1 if (preferred_families and c["family"] in preferred_families) else 0
        score        = c["confidence"] * c["_weight"]
        return (in_preferred, score)

    candidates.sort(key=_rank_key, reverse=True)
    best = candidates[0]

    logger.info(
        "[SE] pick_best_signal → %s %s conf=%.3f tier=%s family=%s invert=%s",
        best["symbol"], best["direction"], best["confidence"],
        best["tier"], best["family"], best["invert"],
    )

    # Remove internal ranking key before returning to scheduler
    best.pop("_weight", None)
    return best


# ══════════════════════════════════════════════════════════════════════════════
# OUTCOME TRACKING  (in-memory; DB is source of truth)
# ══════════════════════════════════════════════════════════════════════════════

def record_outcome(symbol: str, outcome: str) -> None:
    """
    Update the in-memory per-pair win/loss counter.

    Called by scheduler.job_resolve_outcomes() after each resolution.
    These counters reset on process restart — the canonical counts live in
    the Signal DB table and are queried directly by app.py.
    """
    if symbol not in _pair_outcomes:
        _pair_outcomes[symbol] = {"wins": 0, "losses": 0}

    if outcome == "WIN":
        _pair_outcomes[symbol]["wins"] += 1
    elif outcome == "LOSS":
        _pair_outcomes[symbol]["losses"] += 1


def get_pair_stats() -> dict:
    """
    Return in-memory win/loss counts per symbol.

    Shape: {sym: {"wins": int, "losses": int, "win_rate": float|None}}

    Used by app.py /api/stats/pairs alongside DB counts.
    Note: these reset on restart; app.py queries the DB directly for
    authoritative counts and uses this only for session-level context.
    """
    result = {}
    for sym in SYMBOLS:
        counts = _pair_outcomes.get(sym, {"wins": 0, "losses": 0})
        wins   = counts["wins"]
        losses = counts["losses"]
        total  = wins + losses
        result[sym] = {
            "wins":     wins,
            "losses":   losses,
            "win_rate": round(wins / total * 100, 1) if total > 0 else None,
        }
    return result


def get_pair_config() -> dict:
    """
    Return the PAIR_CONFIG dict (a copy) for all symbols.

    Used by app.py /api/stats/pairs to expose threshold, tier, family,
    and invert settings to the dashboard.
    """
    return {sym: dict(cfg) for sym, cfg in PAIR_CONFIG.items()}
