"""
Signal Engine v5 — MR_extreme Pure (No ML)
═══════════════════════════════════════════════════════════════════════════════
Strategy: MR_extreme_rsi_bb — backtested #1 performer across 208 strategies.

  Core logic (IDENTICAL to what was backtested):
    LONG  when RSI14 < 25  AND  BB%B < 5%  AND  Volume > 1.1×
    SHORT when RSI14 > 75  AND  BB%B > 95% AND  Volume > 1.1×

  Confirmation backtest result (BTC + ETH + XRP, Jan–May 2026, OKX data,
  full scheduler logic applied — family rotation, cooldowns, dir-block):
    Overall WR:      65.7%
    Avg daily WR:    65.1%  (median 66.7%)
    Days ≥ 60% WR:   70% of trading days
    Max win streak:  8
    Max loss streak: 5
    Monthly range:   61.1% – 70.6%

  Per-pair:
    BTC:  68.8%  (n=93)
    ETH:  70.8%  (n=65)
    XRP:  61.8%  (n=157)

  Why no ML:
    The 208-strategy backtest proved that 15m crypto candle direction is
    mean-reverting. ML ensemble inference adds latency and fragility without
    improving WR beyond what clean indicator thresholds achieve. The
    MR_extreme strategy with precise thresholds outperformed ALL ML-gated
    variants. Removing ML eliminates training time, cold-start latency, and
    the risk of model drift degrading live performance.

  Active pairs: BTC-USDT, ETH-USDT, XRP-USDT
  Inactive pairs (SOL, DOGE, BNB): signals produced but not scheduled for
    live execution — kept for monitoring/research only.

Architecture:
  - No sklearn / RandomForest / GradientBoosting imports
  - No model training, no retrain_all (retrain_all kept as no-op for compat)
  - _models dict replaced with _engine_ready flag (scheduler compat shim)
  - All other scheduler-facing APIs preserved:
      pick_best_signal(), fetch_okx_candles(), record_outcome(),
      get_pair_stats(), get_pair_config(), get_direction_block_status(),
      reset_cluster_state(), SYMBOLS
"""

import numpy as np
import pandas as pd
import requests
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
SYMBOLS  = ["BTC-USDT", "ETH-USDT", "XRP-USDT",
            "SOL-USDT", "DOGE-USDT", "BNB-USDT"]

# Active pairs for live signal generation
ACTIVE_PAIRS = ["BTC-USDT", "ETH-USDT", "XRP-USDT"]

# ── Scheduler compatibility shim ─────────────────────────────────────────────
# scheduler.py calls `from signal_engine import _models, SYMBOLS` and checks
# `len(_models) >= len(SYMBOLS)` before allowing signals to fire.
# We populate _models with a sentinel so that check passes immediately on start.
_models: dict = {sym: True for sym in SYMBOLS}  # sentinel — no actual model objects

# ── Per-pair config ───────────────────────────────────────────────────────────
# Kept for scheduler / app.py compatibility (get_pair_config())
PAIR_CONFIG = {
    "BTC-USDT":  {"tier": "A", "invert": False, "active": True},
    "ETH-USDT":  {"tier": "A", "invert": False, "active": True},
    "XRP-USDT":  {"tier": "C", "invert": False, "active": True},
    "SOL-USDT":  {"tier": "B", "invert": False, "active": False},
    "DOGE-USDT": {"tier": "B", "invert": False, "active": False},
    "BNB-USDT":  {"tier": "C", "invert": False, "active": False},
}

# ── Strategy thresholds ───────────────────────────────────────────────────────
# These are the EXACT thresholds validated in the backtest.
# Do NOT loosen these to generate more signals — the edge comes from the
# extreme thresholds. More signals at weaker levels = lower WR.
_RSI_LONG    = 25.0   # RSI14 must be BELOW this for LONG
_RSI_SHORT   = 75.0   # RSI14 must be ABOVE this for SHORT
_BBP_LONG    = 0.05   # BB%B must be BELOW this for LONG  (5% = near lower band)
_BBP_SHORT   = 0.95   # BB%B must be ABOVE this for SHORT (95% = near upper band)
_VOL_MIN     = 1.1    # Volume ratio must exceed this (participation filter)
_ATR_LO_PCT  = 15     # Skip bottom 15th percentile ATR (dead market)
_ATR_HI_PCT  = 90     # Skip top 90th percentile ATR (explosive/news)
_ATR_WINDOW  = 50     # Lookback for ATR percentile calculation

# ── System-level state ────────────────────────────────────────────────────────
_dir_loss_streak:    dict = {'UP': 0, 'DOWN': 0}
_dir_blocked_until:  dict = {'UP': 0, 'DOWN': 0}
_system_bar_counter: int  = 0
DIR_BLOCK_THRESHOLD  = 2
DIR_BLOCK_DURATION   = 2

_pair_stats: dict = {}
_pair_consec: dict = {}   # cluster state (kept for compatibility)


# ══════════════════════════════════════════════════════════════════════════════
# INDICATOR HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=p, adjust=False).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))

def _atr(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    tr = pd.concat(
        [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def _bb_pct(c: pd.Series, p: int = 20) -> pd.Series:
    """Bollinger Band %B: 0=at lower band, 1=at upper band."""
    bm = c.rolling(p).mean()
    bs = c.rolling(p).std()
    bu = bm + 2 * bs
    bl = bm - 2 * bs
    return (c - bl) / (bu - bl + 1e-9)

def _vol_ratio(v: pd.Series, p: int = 20) -> pd.Series:
    return v / (v.rolling(p).mean() + 1e-9)

def _williams_r(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    hh = h.rolling(p).max()
    ll = l.rolling(p).min()
    return -100 * (hh - c) / (hh - ll + 1e-9)

def _adx(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14):
    pdm = h.diff().clip(lower=0)
    mdm = (-l.diff()).clip(lower=0)
    tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    at  = tr.rolling(p).mean()
    pdi = 100 * pdm.rolling(p).mean() / (at + 1e-9)
    mdi = 100 * mdm.rolling(p).mean() / (at + 1e-9)
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.rolling(p).mean(), pdi, mdi

def _macd_hist(c: pd.Series) -> pd.Series:
    m  = _ema(c, 12) - _ema(c, 26)
    ms = _ema(m, 9)
    return m - ms


# ══════════════════════════════════════════════════════════════════════════════
# INDICATOR COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add all indicators needed by the signal gate and compatibility layer.
    Operates on a copy — does NOT modify the input DataFrame.
    """
    df = df.copy()
    o, h, l, c, v = df['open'], df['high'], df['low'], df['close'], df['vol']

    # Core signal indicators
    df['rsi_14']    = _rsi(c, 14)
    df['bb_pct']    = _bb_pct(c, 20)
    df['vol_ratio'] = _vol_ratio(v, 20)
    df['atr_14']    = _atr(h, l, c, 14)
    df['atr_norm']  = df['atr_14'] / (c + 1e-9)   # ATR as % of price

    # Additional indicators (kept for app.py / dashboard / signal dict compat)
    df['rsi_7']      = _rsi(c, 7)
    df['macd_hist']  = _macd_hist(c)
    adx_v, pdi, mdi = _adx(h, l, c, 14)
    df['adx']        = adx_v
    df['di_diff']    = pdi - mdi
    df['adx_trend']  = (adx_v > 25).astype(int)
    df['wr']         = _williams_r(h, l, c, 14)

    # EMA for regime label and bull_market
    df['ema20']      = _ema(c, 20)
    df['ema50']      = _ema(c, 50)
    df['ema200']     = _ema(c, 200)
    df['bull_market']= (df['ema20'] > df['ema50']).astype(int)

    # Candle geometry (for signal dict)
    hl_range = (h - l).clip(lower=1e-9)
    df['lower_wick'] = (c.clip(upper=o) - l) / hl_range
    df['upper_wick'] = (h - c.clip(lower=o)) / hl_range

    # Timestamp helpers
    ts_col = 'timestamp' if 'timestamp' in df.columns else 'ts'
    if ts_col in df.columns:
        df['hour'] = df[ts_col].dt.hour
    else:
        df['hour'] = 0

    return df


# ══════════════════════════════════════════════════════════════════════════════
# CORE SIGNAL GATE — MR_EXTREME_RSI_BB
# ══════════════════════════════════════════════════════════════════════════════

def _mr_extreme_signal(symbol: str, df: pd.DataFrame) -> dict | None:
    """
    Apply MR_extreme_rsi_bb gate to the LAST CONFIRMED CANDLE (iloc[-2]).

    Returns signal dict or None.

    Gate (ALL conditions must be true simultaneously):
      LONG:  RSI14 < 25   AND  BB%B < 5%  AND  Vol > 1.1×
      SHORT: RSI14 > 75   AND  BB%B > 95% AND  Vol > 1.1×

    Volatility kill switch:
      Skip when ATR% < 15th percentile (dead/drift) or
                ATR% > 90th percentile (explosive/news breakout).
    """
    if df.empty or len(df) < 60:
        logger.debug("[%s] Not enough candles for signal (%d)", symbol, len(df))
        return None

    df = _compute_indicators(df)
    df_clean = df.dropna(subset=['rsi_14', 'bb_pct', 'vol_ratio', 'atr_norm'])
    if len(df_clean) < 30:
        return None

    # Use last CONFIRMED candle ([-2]) — [-1] is still open
    row = df_clean.iloc[-2]

    rsi14     = float(row['rsi_14'])
    bbp       = float(row['bb_pct'])
    vol       = float(row['vol_ratio'])
    atr_norm  = float(row['atr_norm'])
    hour      = int(row.get('hour', 0))

    # ── ATR volatility kill switch ─────────────────────────────────────────
    atr_history = df_clean['atr_norm'].values
    if len(atr_history) >= _ATR_WINDOW:
        lo_cut = float(np.nanpercentile(atr_history[-_ATR_WINDOW:], _ATR_LO_PCT))
        hi_cut = float(np.nanpercentile(atr_history[-_ATR_WINDOW:], _ATR_HI_PCT))
        if atr_norm < lo_cut:
            logger.debug("[%s] ATR kill: dead market (atr=%.4f < lo=%.4f)", symbol, atr_norm, lo_cut)
            return None
        if atr_norm > hi_cut:
            logger.debug("[%s] ATR kill: explosive market (atr=%.4f > hi=%.4f)", symbol, atr_norm, hi_cut)
            return None

    # ── Volume participation filter ────────────────────────────────────────
    if vol < _VOL_MIN:
        logger.debug("[%s] Vol filter: vol=%.2f < min=%.1f", symbol, vol, _VOL_MIN)
        return None

    # ── Core gate ─────────────────────────────────────────────────────────
    direction = None
    if rsi14 < _RSI_LONG and bbp < _BBP_LONG:
        direction = 'UP'
    elif rsi14 > _RSI_SHORT and bbp > _BBP_SHORT:
        direction = 'DOWN'

    if direction is None:
        return None

    # ── Confidence computation ─────────────────────────────────────────────
    # Based on how extreme the indicators are — higher distance from threshold = more conviction.
    # Range: ~0.55 (just above threshold) to ~0.95 (maximum extreme)
    if direction == 'UP':
        rsi_score = min(1.0, (_RSI_LONG  - rsi14) / _RSI_LONG)
        bb_score  = min(1.0, (_BBP_LONG  - bbp)   / _BBP_LONG)
    else:
        rsi_score = min(1.0, (rsi14 - _RSI_SHORT) / (100 - _RSI_SHORT))
        bb_score  = min(1.0, (bbp   - _BBP_SHORT) / (1.0 - _BBP_SHORT))

    vol_boost   = min(0.08, (vol - _VOL_MIN) * 0.04)
    confidence  = 0.55 + 0.30 * (rsi_score + bb_score) / 2 + vol_boost
    confidence  = float(min(0.97, max(0.55, confidence)))

    # ── Tier (T1 = high-volume signal, T2 = standard) ─────────────────────
    tier = "T1" if vol > 1.5 else "T2"

    # ── Candle timing ──────────────────────────────────────────────────────
    latest    = df_clean.iloc[-1]
    ts        = latest['timestamp']
    minutes   = ts.minute
    boundary  = (minutes // 15) * 15
    candle_open  = ts.replace(minute=boundary, second=0, microsecond=0)
    candle_close = candle_open + pd.Timedelta(minutes=15)

    # ── Extract extra fields for signal dict ──────────────────────────────
    adx_val      = float(row.get('adx', 0))
    macd_val     = float(row.get('macd_hist', 0))
    lower_wick   = float(row.get('lower_wick', 0))
    upper_wick   = float(row.get('upper_wick', 0))
    bull_market  = bool(row.get('bull_market', 0))

    logger.info(
        "[%s] SIGNAL %s | RSI14=%.1f BBp=%.3f Vol=%.2f ATR=%.4f conf=%.3f %s",
        symbol, direction, rsi14, bbp, vol, atr_norm, confidence, tier
    )

    return {
        'symbol':            symbol,
        'direction':         direction,
        'invert':            PAIR_CONFIG.get(symbol, {}).get('invert', False),
        'confidence':        confidence,
        'threshold':         0.55,
        'margin':            confidence - 0.55,
        'tier':              tier,
        'signal_gate':       'mr_extreme',
        'tail_wick':         lower_wick if direction == 'UP' else upper_wick,
        'tail_boost':        0.0,
        'tail_label':        'NONE',
        'vol_spike':         bool(vol > 1.5),
        'rsi_14':            float(row.get('rsi_14', 50)),
        'macd_hist':         macd_val,
        'adx':               adx_val,
        'vol_ratio':         vol,
        'adx_trend':         bool(row.get('adx_trend', 0)),
        'bull_market':       bull_market,
        'open_price':        float(latest['close']),
        'candle_open_time':  candle_open.to_pydatetime().replace(tzinfo=None),
        'candle_close_time': candle_close.to_pydatetime().replace(tzinfo=None),
        'in_momentum':       False,
        'momentum_move_pct': 0.0,
        'cluster_streak':    _pair_consec.get(symbol, {}).get('count', 1),
        'hour_penalty':      False,
        'hour_blocked':      False,
    }


# ══════════════════════════════════════════════════════════════════════════════
# OKX DATA FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 300) -> pd.DataFrame:
    """
    Fetch 15m OHLCV candles from OKX public API.
    Paginates automatically when limit > 300.
    Returns sorted DataFrame with columns: timestamp, open, high, low, close, vol.
    """
    OKX_PAGE_MAX = 300
    all_frames   = []
    fetched      = 0
    after_ts     = None

    while fetched < limit:
        batch_size = min(OKX_PAGE_MAX, limit - fetched)
        if after_ts is None:
            endpoint = f"{OKX_BASE}/api/v5/market/candles"
            params   = {"instId": symbol, "bar": bar, "limit": str(batch_size)}
        else:
            endpoint = f"{OKX_BASE}/api/v5/market/history-candles"
            params   = {"instId": symbol, "bar": bar, "limit": str(batch_size),
                        "after": str(after_ts)}
        try:
            resp = requests.get(endpoint, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "0":
                logger.error("[%s] OKX error: %s", symbol, data.get('msg'))
                break
            rows = data.get("data", [])
            if not rows:
                break
            df_batch = pd.DataFrame(rows, columns=[
                "timestamp", "open", "high", "low", "close",
                "vol", "volCcy", "volCcyQuote", "confirm"
            ])
            df_batch["timestamp"] = pd.to_datetime(
                df_batch["timestamp"].astype(float), unit="ms", utc=True
            )
            for col in ["open", "high", "low", "close", "vol"]:
                df_batch[col] = df_batch[col].astype(float)
            all_frames.append(df_batch)
            fetched  += len(df_batch)
            after_ts  = int(df_batch["timestamp"].min().timestamp() * 1000)
            if len(df_batch) < batch_size:
                break
        except Exception as e:
            logger.error("[%s] OKX fetch error: %s", symbol, e)
            break

    if not all_frames:
        return pd.DataFrame()

    df = pd.concat(all_frames, ignore_index=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    logger.info("[%s] Fetched %d candles", symbol, len(df))
    return df


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE-PAIR SIGNAL ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def get_signal_for_symbol(symbol: str) -> dict | None:
    """
    Fetch live OKX data and generate a signal for a single symbol.
    Returns signal dict or None.
    Called by pick_best_signal() for each active pair.
    """
    if not PAIR_CONFIG.get(symbol, {}).get('active', False):
        logger.debug("[%s] Pair not active — skipping", symbol)
        return None

    df = fetch_okx_candles(symbol, limit=120)
    if df.empty or len(df) < 60:
        logger.warning("[%s] Insufficient data (%d candles)", symbol, len(df))
        return None

    return _mr_extreme_signal(symbol, df)


# ══════════════════════════════════════════════════════════════════════════════
# PICK BEST SIGNAL  (called by scheduler.py)
# ══════════════════════════════════════════════════════════════════════════════

def pick_best_signal(
    min_confidence: float = None,
    exclude: list = None,
    preferred_families: list = None,
    excluded_families: list = None,
    blocked_directions: dict = None,
) -> dict | None:
    """
    Evaluate all active pairs and return the single best signal.

    Args:
        min_confidence:    Global confidence floor (0.0 = disabled).
        exclude:           List of symbol strings to skip (per-pair cooldown).
        preferred_families: Not used for filtering — informational only.
        excluded_families:  Family strings to exclude (family rotation).
        blocked_directions: Dict of {direction: floor} for Rule 2 saturation.

    Returns the highest-scoring qualifying signal dict, or None.

    Scoring: margin (= confidence − 0.55) + 0.04 bonus for T1 vol spike.
    """
    PAIR_FAMILY = {
        "BTC-USDT":  "A", "ETH-USDT":  "A",
        "DOGE-USDT": "B", "SOL-USDT":  "B",
        "XRP-USDT":  "C", "BNB-USDT":  "C",
    }

    global _system_bar_counter
    _system_bar_counter += 1

    # ── System-level directional block ────────────────────────────────────
    _currently_blocked_dirs = set()
    for _dir in ('UP', 'DOWN'):
        if _system_bar_counter <= _dir_blocked_until.get(_dir, 0):
            _currently_blocked_dirs.add(_dir)
            logger.warning(
                "[DIR_BLOCK] %s blocked until bar %d (now %d)",
                _dir, _dir_blocked_until[_dir], _system_bar_counter
            )

    active_symbols = [
        s for s in ACTIVE_PAIRS
        if not (exclude and s in exclude)
    ]

    candidates = []
    for sym in active_symbols:
        try:
            sig = get_signal_for_symbol(sym)
            if sig:
                sig['family'] = PAIR_FAMILY.get(sym, "A")
                candidates.append(sig)
                logger.info(
                    "[%s] candidate %s conf=%.3f margin=%.3f family=%s",
                    sym, sig['direction'], sig['confidence'],
                    sig['margin'], sig['family']
                )
        except Exception as e:
            logger.error("[%s] get_signal_for_symbol error: %s", sym, e)

    if not candidates:
        logger.info("[ENGINE] No qualifying signals this candle")
        return None

    # ── Apply directional block ────────────────────────────────────────────
    if _currently_blocked_dirs:
        before     = len(candidates)
        candidates = [s for s in candidates if s['direction'] not in _currently_blocked_dirs]
        removed    = before - len(candidates)
        if removed:
            logger.warning("[DIR_BLOCK] Removed %d candidate(s) in blocked dirs %s",
                           removed, _currently_blocked_dirs)
        if not candidates:
            logger.warning("[DIR_BLOCK] All candidates blocked — skipping candle")
            return None

    # ── Min confidence floor ───────────────────────────────────────────────
    if min_confidence:
        candidates = [s for s in candidates if s['confidence'] >= min_confidence]
        if not candidates:
            logger.info("[ENGINE] No signals above min_confidence=%.2f", min_confidence)
            return None

    # ── Rule 2: directional saturation ────────────────────────────────────
    _dir_blocked = blocked_directions or {}
    if _dir_blocked:
        filtered = []
        for s in candidates:
            floor = _dir_blocked.get(s['direction'])
            if floor and s['confidence'] < floor:
                logger.info("[ENGINE] Rule2 blocked %s %s conf=%.3f < floor=%.2f",
                            s['symbol'], s['direction'], s['confidence'], floor)
            else:
                filtered.append(s)
        candidates = filtered
        if not candidates:
            logger.info("[ENGINE] Rule2: all candidates blocked")
            return None

    # ── Scoring function ───────────────────────────────────────────────────
    def score(s):
        return s['margin'] + (0.04 if s['tier'] == 'T1' else 0.0)

    # ── Family rotation — hard exclusion ──────────────────────────────────
    _excl_set = set(excluded_families) if excluded_families else set()
    if _excl_set:
        eligible = [s for s in candidates if s['family'] not in _excl_set]
        if eligible:
            best = max(eligible, key=score)
            logger.info("[ENGINE] Best: %s %s conf=%.3f family=%s gate=%s",
                        best['symbol'], best['direction'], best['confidence'],
                        best['family'], best.get('signal_gate', 'mr_extreme'))
            return best
        else:
            logger.info("[ENGINE] Family rotation: no signal outside %s — skipping candle",
                        _excl_set)
            return None

    best = max(candidates, key=score)
    logger.info("[ENGINE] Best: %s %s conf=%.3f family=%s gate=%s",
                best['symbol'], best['direction'], best['confidence'],
                best['family'], best.get('signal_gate', 'mr_extreme'))
    return best


# ══════════════════════════════════════════════════════════════════════════════
# OUTCOME TRACKING  (called by scheduler.py resolve job)
# ══════════════════════════════════════════════════════════════════════════════

def record_outcome(symbol: str, outcome: str, direction: str = None):
    """
    Update per-pair stats and system-level directional block on LOSS.
    Called by job_resolve_outcomes() in scheduler.py.
    """
    if symbol not in _pair_stats:
        _pair_stats[symbol] = {'wins': 0, 'losses': 0, 'signals': 0}
    _pair_stats[symbol]['signals'] += 1

    if outcome == 'WIN':
        _pair_stats[symbol]['wins'] += 1
        if direction:
            _dir_loss_streak[direction] = 0
            logger.debug("[DIR_BLOCK] %s WIN → %s streak reset", symbol, direction)
    elif outcome == 'LOSS':
        _pair_stats[symbol]['losses'] += 1
        if direction:
            _dir_loss_streak[direction] = _dir_loss_streak.get(direction, 0) + 1
            streak = _dir_loss_streak[direction]
            logger.info("[DIR_BLOCK] %s LOSS | %s streak=%d", symbol, direction, streak)
            if streak >= DIR_BLOCK_THRESHOLD:
                _dir_blocked_until[direction] = _system_bar_counter + DIR_BLOCK_DURATION
                _dir_loss_streak[direction]   = 0
                logger.warning(
                    "[DIR_BLOCK] %s direction BLOCKED for %d candles (expires bar %d)",
                    direction, DIR_BLOCK_DURATION, _dir_blocked_until[direction]
                )


# ══════════════════════════════════════════════════════════════════════════════
# COMPAT STUBS  (called by scheduler.py — kept to avoid import errors)
# ══════════════════════════════════════════════════════════════════════════════

def retrain_all(limit: int = 960):
    """
    No-op in v5 — ML has been removed.
    Called by job_retrain() in scheduler.py; kept to avoid ImportError.
    The scheduler's retrain job will fire harmlessly every 4 hours.
    """
    logger.info(
        "[ENGINE] retrain_all called — no-op in v5 (ML removed). "
        "Engine is always ready; no retraining required."
    )


def reset_cluster_state(symbol: str = None):
    """Cluster state reset — kept for scheduler compatibility."""
    if symbol:
        _pair_consec.pop(symbol, None)
    else:
        _pair_consec.clear()


# ══════════════════════════════════════════════════════════════════════════════
# ACCESSORS  (called by app.py)
# ══════════════════════════════════════════════════════════════════════════════

def get_pair_stats() -> dict:
    """Return live win/loss counts per pair. Called by app.py dashboard."""
    result = {}
    for sym in SYMBOLS:
        s     = _pair_stats.get(sym, {'wins': 0, 'losses': 0, 'signals': 0})
        total = s['wins'] + s['losses']
        cfg   = PAIR_CONFIG.get(sym, {})
        result[sym] = {
            'wins':        s['wins'],
            'losses':      s['losses'],
            'signals':     s['signals'],
            'win_rate':    round(s['wins'] / total * 100, 1) if total > 0 else None,
            'threshold':   0.55,
            'tier':        cfg.get('tier', 'A'),
            'signal_gate': 'mr_extreme',
            'active':      cfg.get('active', False),
        }
    return result


def get_pair_config() -> dict:
    """Return per-pair configuration. Called by app.py."""
    return {sym: dict(cfg) for sym, cfg in PAIR_CONFIG.items()}


def get_direction_block_status() -> dict:
    """Return current directional block state. Called by app.py."""
    return {
        'dir_loss_streak':   dict(_dir_loss_streak),
        'dir_blocked_until': dict(_dir_blocked_until),
        'system_bar':        _system_bar_counter,
        'currently_blocked': {
            d: _system_bar_counter <= _dir_blocked_until.get(d, 0)
            for d in ('UP', 'DOWN')
        }
    }
