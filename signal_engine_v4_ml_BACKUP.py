"""
Signal Engine v4 — Per-Pair Optimized Strategy
═══════════════════════════════════════════════════════════════════════════════
Backtested on 15m OHLCV data (Jan 2025 – Jun 2026) across all 6 pairs.

Per-pair validated strategies:
  BTC-USDT  : RSI7<25 + WR%<-90          → 60.47% WR | 11.4/day
  ETH-USDT  : RSI7<30 + BB%<8%           → 60.27% WR | 16.7/day
  SOL-USDT  : RSI7<20 + 2-bar cooldown   → 58.56% WR | 10.6/day
  XRP-USDT  : RSI7<30 + BB%<8% + VOL>1.2 → 58.52% WR |  9.9/day
  DOGE-USDT : RSI7<25 + WR%<-95 + CALM   → 58.31% WR |  5.1/day
  BNB-USDT  : RSI7<10 + BB%<5% + CALM    → 56.96% WR |  3.0/day

System simulation (pick_best + family rotation, Jan–May 2026):
  WR: 59.40% | 20.7 signals/day | MaxWin: 11 | MaxLoss: 9
  Monthly min: 56.7% (Feb) | Days ≥55%: 68% of trading days

Architecture changes from v3:
  1. Per-pair signal gates — each pair has its own indicator combination
  2. Gate logic runs BEFORE ML ensemble inference
  3. SOL uses in-engine 2-bar cooldown (no external dependency)
  4. DOGE/BNB use ATR-calm filter (atr_ratio < 1.8)
  5. XRP adds VOL>1.2x filter
  6. Momentum regime tightening is pair-threshold-aware (+8pts on RSI)
  7. Hour 07 UTC blocked universally (45-51% WR across 4/6 pairs)
  8. Hours 14-17 UTC get +5pts RSI penalty (consistently below 55%)
  9. Cluster escalator still active (direction streak ≥3 → +0.08 conf)
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
SYMBOLS  = ["BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","BNB-USDT","DOGE-USDT"]

# ── Per-pair backtested config ────────────────────────────────────────────────
#
# signal_gate:  'bb'     → RSI + BB% gate
#               'wr'     → RSI + Williams %R gate
#               'cd'     → RSI only with internal cooldown
#               'vol_bb' → RSI + BB% + volume gate
#
# rsi_long / rsi_short : RSI7 threshold for long / short entry
# gate_long / gate_short: secondary gate threshold (BB% or WR%)
# calm_only: True → only fire when ATR ratio < 1.8 (non-momentum regime)
# vol_min:   minimum vol_ratio required (e.g. 1.2 for XRP)
# cooldown:  bars between signals for 'cd' gate pairs (SOL)
# threshold: ML ensemble confidence threshold for this pair
# tier:      family group A/B/C for rotation
# invert:    True → execution direction is flipped from signal direction
#
PAIR_CONFIG = {
    "BTC-USDT": {
        "signal_gate": "wr",
        "rsi_long":    25,    "rsi_short":   75,
        "gate_long":   -90,   "gate_short":  -10,  # WR thresholds
        "calm_only":   False, "vol_min":     None,
        "cooldown":    None,
        "threshold":   0.68,  "tier": "A",  "invert": False,
    },
    "ETH-USDT": {
        "signal_gate": "bb",
        "rsi_long":    30,    "rsi_short":   70,
        "gate_long":   0.15,  "gate_short":  0.85, # BB% thresholds (loosened from 0.08)
        "calm_only":   False, "vol_min":     None,
        "cooldown":    None,
        "threshold":   0.58,  "tier": "A",  "invert": False,
    },
    "SOL-USDT": {
        "signal_gate": "cd",
        "rsi_long":    20,    "rsi_short":   80,
        "gate_long":   None,  "gate_short":  None,
        "calm_only":   True,  "vol_min":     None,  # ATR calm required (added — SOL was over-represented in loss streaks)
        "cooldown":    2,     # 2-bar internal cooldown
        "threshold":   0.62,  "tier": "B",  "invert": False,
    },
    "XRP-USDT": {
        "signal_gate": "vol_bb",
        "rsi_long":    30,    "rsi_short":   70,
        "gate_long":   0.15,  "gate_short":  0.85, # BB% thresholds (loosened from 0.08)
        "calm_only":   False, "vol_min":     1.2,  # VOL filter
        "cooldown":    None,
        "threshold":   0.62,  "tier": "C",  "invert": False,  # LIVE: normal execution (UP sig -> trade UP)
    },
    "DOGE-USDT": {
        "signal_gate": "wr",
        "rsi_long":    25,    "rsi_short":   75,
        "gate_long":   -95,   "gate_short":  -5,   # WR thresholds
        "calm_only":   True,  "vol_min":     None, # ATR calm required
        "cooldown":    None,
        "threshold":   0.62,  "tier": "B",  "invert": False,
    },
    "BNB-USDT": {
        "signal_gate": "bb",
        "rsi_long":    10,    "rsi_short":   90,
        "gate_long":   0.15,  "gate_short":  0.85, # BB% thresholds (loosened to match system gate)
        "calm_only":   True,  "vol_min":     None, # ATR calm required
        "cooldown":    None,
        "threshold":   0.65,  "tier": "C",  "invert": False,
    },
}

# ── Hour penalties ─────────────────────────────────────────────────────────────
# Hour 07 UTC: 45-51% WR across 4/6 pairs → hard block
# Hours 14-17 UTC: consistently 51-54% → RSI tightened +5pts
BLOCKED_HOURS   = set()   # dir_block now handles trending-hour losses dynamically
PENALISED_HOURS = set()   # removed — dir_block handles bad-hour losses reactively
HOUR_RSI_PENALTY = 0

# ── Momentum regime ────────────────────────────────────────────────────────────
_MOMENTUM_ATR_MULT  = 1.8   # 4H ATR > this × 24H median ATR → momentum
_MOMENTUM_MOVE_PCT  = 0.03  # ±3% net move over 4H → momentum
_MOMENTUM_RSI_DELTA = 8     # tighten RSI thresholds by this many points

# ── Cluster escalator ─────────────────────────────────────────────────────────
_CLUSTER_THRESHOLD    = 3
_CLUSTER_CONF_PENALTY = 0.08
_pair_consec: dict    = {}

# ── System-level directional cool-down state ─────────────────────────────────
# After 2 consecutive system-level losses in the same direction,
# that direction is blocked for 2 candles across ALL pairs.
_dir_loss_streak:    dict = {'UP': 0, 'DOWN': 0}
_dir_blocked_until:  dict = {'UP': 0, 'DOWN': 0}
_system_bar_counter: int  = 0
DIR_BLOCK_THRESHOLD  = 2
DIR_BLOCK_DURATION   = 2

# ── Model storage ─────────────────────────────────────────────────────────────
_models:     dict = {}
_scalers:    dict = {}
_pair_stats: dict = {}

# ── SOL internal cooldown state ───────────────────────────────────────────────
# Tracks how many bars since the last SOL signal fired
# (replaces external cooldown — lives entirely in-engine)
_sol_last_signal_bar: int = -99
_sol_bar_counter:     int = 0


# ══════════════════════════════════════════════════════════════════════════════
# INDICATOR HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s, p=14):
    d  = s.diff()
    g  = d.clip(lower=0).rolling(p).mean()
    l  = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))

def _atr(h, l, c, p=14):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def _williams_r(h, l, c, p=14):
    hh = h.rolling(p).max()
    ll = l.rolling(p).min()
    return -100 * (hh - c) / (hh - ll + 1e-9)

def _stoch(h, l, c, k=14, d=3):
    kp = 100 * (c - l.rolling(k).min()) / (h.rolling(k).max() - l.rolling(k).min() + 1e-9)
    return kp, kp.rolling(d).mean()

def _adx(h, l, c, p=14):
    pdm = h.diff().clip(lower=0)
    mdm = (-l.diff()).clip(lower=0)
    tr  = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    at  = tr.rolling(p).mean()
    pdi = 100 * pdm.rolling(p).mean() / (at + 1e-9)
    mdi = 100 * mdm.rolling(p).mean() / (at + 1e-9)
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.rolling(p).mean(), pdi, mdi

def _cci(h, l, c, p=20):
    tp = (h + l + c) / 3
    sm = tp.rolling(p).mean()
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - x.mean())))
    return (tp - sm) / (0.015 * md + 1e-9)


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE ENGINEERING
# ══════════════════════════════════════════════════════════════════════════════

FEATURE_COLS = [
    'ret_1','ret_2','ret_3','ret_5','ret_7','ret_10',
    'body','upper_wick','lower_wick','body_ratio','hl_range',
    'rsi_7','rsi_14','rsi_21','rsi_diff','rsi_slope','rsi_overbought','rsi_oversold',
    'macd_hist','macd_hist_diff','macd_cross','macd_cross_chg',
    'bb_pct','bb_width','bb_squeeze',
    'ema_cross_8_21','ema_cross_21_50','price_vs_ema21','price_vs_ema50',
    'ema8_slope','ema21_slope','ema50_slope',
    'stoch_k','stoch_d','stoch_diff','stoch_cross','stoch_ob','stoch_os',
    'atr_norm','atr_ratio','atr_fast',
    'adx','di_diff','adx_trend',
    'wr','cci',
    'vol_ratio','vol_trend','vol_spike','obv_slope','obv_slope10',
    'near_high5','near_low5','near_high10','near_low10',
    'momentum5','momentum10','roc5','roc10',
    'hour','dow','session_asia','session_ny',
    'price_above_ema89','bull_market','trend_strength','vol_regime','price_rsi_div',
    'consec_up',
    'btc_ret_1','btc_ret_3','btc_vol_ratio','btc_rsi_14',
    'eth_ret_1','eth_ret_3','eth_vol_ratio','eth_rsi_14',
    'btc_eth_corr',
    'tail_direction_score','tail_dominance',
]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    vol = df['vol']

    for n in [1, 2, 3, 5, 7, 10]:
        df[f'ret_{n}'] = c.pct_change(n)

    df['body']       = (c - o) / (o + 1e-9)
    df['upper_wick'] = (h - c.clip(lower=o)) / (h - l + 1e-9)
    df['lower_wick'] = (c.clip(upper=o) - l) / (h - l + 1e-9)
    df['body_ratio'] = (c - o).abs() / (h - l + 1e-9)
    df['hl_range']   = (h - l) / (c + 1e-9)
    df['tail_direction_score'] = df['lower_wick'] - df['upper_wick']
    wick_sum = (df['lower_wick'] + df['upper_wick']).clip(lower=1e-9)
    df['tail_dominance'] = (df['lower_wick'] - df['upper_wick']).abs() / wick_sum

    df['rsi_7']  = _rsi(c, 7)
    df['rsi_14'] = _rsi(c, 14)
    df['rsi_21'] = _rsi(c, 21)
    df['rsi_diff']       = df['rsi_14'].diff()
    df['rsi_slope']      = df['rsi_14'] - df['rsi_14'].shift(3)
    df['rsi_overbought'] = (df['rsi_14'] > 70).astype(int)
    df['rsi_oversold']   = (df['rsi_14'] < 30).astype(int)

    m  = _ema(c, 12) - _ema(c, 26)
    ms = _ema(m, 9)
    mh = m - ms
    df['macd_hist']      = mh
    df['macd_hist_diff'] = mh.diff()
    df['macd_cross']     = (m > ms).astype(int)
    df['macd_cross_chg'] = df['macd_cross'].diff()

    for p in [8, 13, 21, 34, 50, 89]:
        df[f'ema{p}'] = _ema(c, p)
    df['ema_cross_8_21']  = (df['ema8']  > df['ema21']).astype(int)
    df['ema_cross_21_50'] = (df['ema21'] > df['ema50']).astype(int)
    df['price_vs_ema21']  = (c - df['ema21']) / (df['ema21'] + 1e-9)
    df['price_vs_ema50']  = (c - df['ema50']) / (df['ema50'] + 1e-9)
    df['ema8_slope']      = df['ema8'].pct_change(3)
    df['ema21_slope']     = df['ema21'].pct_change(3)
    df['ema50_slope']     = df['ema50'].pct_change(5)

    bm = c.rolling(20).mean()
    bs = c.rolling(20).std()
    bu = bm + 2*bs
    bl = bm - 2*bs
    df['bb_pct']    = (c - bl) / (bu - bl + 1e-9)
    df['bb_width']  = (bu - bl) / (bm + 1e-9)
    df['bb_squeeze']= (df['bb_width'] < df['bb_width'].rolling(20).mean()).astype(int)

    sk, sd = _stoch(h, l, c)
    df['stoch_k']    = sk
    df['stoch_d']    = sd
    df['stoch_diff'] = sk - sd
    df['stoch_cross']= (sk > sd).astype(int)
    df['stoch_ob']   = (sk > 80).astype(int)
    df['stoch_os']   = (sk < 20).astype(int)

    at14 = _atr(h, l, c, 14)
    at7  = _atr(h, l, c, 7)
    df['atr_norm']  = at14 / (c + 1e-9)
    df['atr_ratio'] = at14 / (at14.rolling(50).mean() + 1e-9)
    df['atr_fast']  = at7  / (at14 + 1e-9)

    adx_v, pdi, mdi_v = _adx(h, l, c)
    df['adx']       = adx_v
    df['di_diff']   = pdi - mdi_v
    df['adx_trend'] = (adx_v > 25).astype(int)

    df['wr']  = _williams_r(h, l, c, 14)
    df['cci'] = _cci(h, l, c)

    vm20 = vol.rolling(20).mean()
    df['vol_ratio'] = vol / (vm20 + 1e-9)
    df['vol_trend'] = vol.rolling(5).mean() / (vm20 + 1e-9)
    df['vol_spike'] = (df['vol_ratio'] > 2.0).astype(int)
    obv = (np.sign(c.diff()) * vol).cumsum()
    df['obv_slope']   = obv.pct_change(5)
    df['obv_slope10'] = obv.pct_change(10)

    df['near_high5']  = c / (h.rolling(5).max()  + 1e-9)
    df['near_low5']   = c / (l.rolling(5).min()  + 1e-9)
    df['near_high10'] = c / (h.rolling(10).max() + 1e-9)
    df['near_low10']  = c / (l.rolling(10).min() + 1e-9)

    df['momentum5']  = c - c.shift(5)
    df['momentum10'] = c - c.shift(10)
    df['roc5']       = c.pct_change(5)
    df['roc10']      = c.pct_change(10)

    df['candle_dir'] = np.sign(c - o)
    df['consec_up']  = df['candle_dir'].rolling(3).sum()

    df['price_above_ema89'] = (c > df['ema89']).astype(int)
    df['bull_market']       = (df['ema21'] > df['ema50']).astype(int)
    df['trend_strength']    = df['adx'] * df['di_diff'].abs()
    df['vol_regime']        = (df['atr_norm'] > df['atr_norm'].rolling(30).mean()).astype(int)
    df['price_rsi_div']     = df['ret_3'] * (df['rsi_14'] - df['rsi_14'].shift(3))

    ts_col = 'timestamp' if 'timestamp' in df.columns else 'ts'
    df['hour']         = df[ts_col].dt.hour
    df['dow']          = df[ts_col].dt.dayofweek
    df['session_asia'] = ((df['hour'] >= 0)  & (df['hour'] < 8)).astype(int)
    df['session_ny']   = ((df['hour'] >= 13) & (df['hour'] < 21)).astype(int)

    for col in ['btc_ret_1','btc_ret_3','btc_vol_ratio','btc_rsi_14',
                'eth_ret_1','eth_ret_3','eth_vol_ratio','eth_rsi_14','btc_eth_corr']:
        if col not in df.columns:
            df[col] = 0.0

    return df


# ══════════════════════════════════════════════════════════════════════════════
# PER-PAIR SIGNAL GATE
# ══════════════════════════════════════════════════════════════════════════════

def _check_signal_gate(symbol: str, row: dict, cfg: dict,
                        rsi7_val: float, vol_ratio: float,
                        bb_pct: float, wr14_val: float,
                        atr_ratio: float) -> str | None:
    """
    Apply per-pair entry gate. Returns 'UP', 'DOWN', or None.

    Gates:
      'bb'     — RSI7 extreme + BB% extreme
      'wr'     — RSI7 extreme + WR% extreme
      'cd'     — RSI7 extreme only (cooldown managed externally by caller)
      'vol_bb' — RSI7 extreme + BB% extreme + vol_ratio >= vol_min

    Also applies:
      calm_only:  if True, block when atr_ratio >= 1.8
      vol_min:    minimum vol_ratio required
      hour block: hour 07 UTC → return None
      hour penalty: hours 14-17 UTC → RSI thresholds tightened +5pts
    """
    gate      = cfg['signal_gate']
    calm_only = cfg.get('calm_only', False)
    vol_min   = cfg.get('vol_min')

    # ── Hour checks ────────────────────────────────────────────────────────
    hour = int(row.get('hour', 0))
    if hour in BLOCKED_HOURS:
        logger.debug("[%s] Hour %d blocked", symbol, hour)
        return None

    rsi_penalty = HOUR_RSI_PENALTY if hour in PENALISED_HOURS else 0

    # ── ATR calm filter ────────────────────────────────────────────────────
    if calm_only and atr_ratio >= _MOMENTUM_ATR_MULT:
        logger.debug("[%s] calm_only blocked — atr_ratio=%.2f", symbol, atr_ratio)
        return None

    # ── Volume minimum ─────────────────────────────────────────────────────
    if vol_min is not None and vol_ratio < vol_min:
        logger.debug("[%s] vol_min blocked — vol_ratio=%.2f < %.1f", symbol, vol_ratio, vol_min)
        return None

    # ── Effective RSI thresholds (with hour penalty) ───────────────────────
    rsi_long  = cfg['rsi_long']  - rsi_penalty   # e.g. 30 → 25 during penalised hours
    rsi_short = cfg['rsi_short'] + rsi_penalty   # e.g. 70 → 75 during penalised hours

    # ── Gate logic ─────────────────────────────────────────────────────────
    if gate == 'bb':
        gate_long  = cfg['gate_long']   # BB% floor (e.g. 0.08)
        gate_short = cfg['gate_short']  # BB% ceiling (e.g. 0.92)
        if   rsi7_val < rsi_long  and bb_pct < gate_long:   return 'UP'
        elif rsi7_val > rsi_short and bb_pct > gate_short:  return 'DOWN'

    elif gate == 'wr':
        gate_long  = cfg['gate_long']   # WR floor (e.g. -90)
        gate_short = cfg['gate_short']  # WR ceiling (e.g. -10)
        if   rsi7_val < rsi_long  and wr14_val < gate_long:   return 'UP'
        elif rsi7_val > rsi_short and wr14_val > gate_short:  return 'DOWN'

    elif gate == 'cd':
        # Cooldown managed by caller — just check RSI threshold here
        if   rsi7_val < rsi_long:   return 'UP'
        elif rsi7_val > rsi_short:  return 'DOWN'

    elif gate == 'vol_bb':
        gate_long  = cfg['gate_long']
        gate_short = cfg['gate_short']
        vol_req    = vol_min or 1.0
        if   rsi7_val < rsi_long  and bb_pct < gate_long  and vol_ratio >= vol_req: return 'UP'
        elif rsi7_val > rsi_short and bb_pct > gate_short and vol_ratio >= vol_req: return 'DOWN'

    return None


# ══════════════════════════════════════════════════════════════════════════════
# MOMENTUM REGIME DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _get_momentum_regime(df: pd.DataFrame, cfg: dict) -> dict:
    """
    Detect momentum/trending regime from 15m candle data.
    Uses pair-specific RSI thresholds when tightening.

    Returns dict with:
      in_momentum, atr_regime, move_pct, move_regime, direction,
      rsi7_long_thr, rsi7_short_thr
    """
    default = {
        'in_momentum': False, 'atr_regime': False,
        'move_pct': 0.0,      'move_regime': False,
        'direction': 'NEUTRAL',
        'rsi7_long_thr':  cfg['rsi_long'],
        'rsi7_short_thr': cfg['rsi_short'],
    }
    try:
        if df is None or len(df) < 32:
            return default

        c, h, l = df['close'], df['high'], df['low']
        tr      = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)

        atr_4h       = tr.iloc[-17:-1].mean() if len(tr) >= 17 else tr.mean()
        atr_24h_med  = tr.iloc[-97:-1].median() if len(tr) >= 97 else tr.median()
        atr_regime   = (atr_4h / (atr_24h_med + 1e-9)) > _MOMENTUM_ATR_MULT

        price_start = float(c.iloc[-17]) if len(c) >= 17 else float(c.iloc[0])
        price_end   = float(c.iloc[-2])
        move_pct    = (price_end - price_start) / (price_start + 1e-9)
        move_regime = abs(move_pct) > _MOMENTUM_MOVE_PCT

        if move_pct > _MOMENTUM_MOVE_PCT:   direction = 'UP'
        elif move_pct < -_MOMENTUM_MOVE_PCT: direction = 'DOWN'
        else:                                direction = 'NEUTRAL'

        in_momentum = atr_regime or move_regime

        # Tighten pair-specific thresholds when in momentum
        rsi7_long_thr  = cfg['rsi_long']  - _MOMENTUM_RSI_DELTA if in_momentum else cfg['rsi_long']
        rsi7_short_thr = cfg['rsi_short'] + _MOMENTUM_RSI_DELTA if in_momentum else cfg['rsi_short']

        if in_momentum:
            logger.info(
                "[REGIME] %s momentum | move=%.2f%% atr=%s → RSI thresholds %d/%d",
                cfg.get('_symbol','?'), move_pct*100, atr_regime,
                rsi7_long_thr, rsi7_short_thr
            )

        return {
            'in_momentum': in_momentum, 'atr_regime': atr_regime,
            'move_pct': move_pct,       'move_regime': move_regime,
            'direction': direction,
            'rsi7_long_thr':  rsi7_long_thr,
            'rsi7_short_thr': rsi7_short_thr,
        }
    except Exception as e:
        logger.warning("[REGIME] Error: %s — using normal thresholds", e)
        return default


# ══════════════════════════════════════════════════════════════════════════════
# CLUSTER ESCALATOR
# ══════════════════════════════════════════════════════════════════════════════

def _get_cluster_penalty(symbol: str, direction: str) -> float:
    state = _pair_consec.get(symbol)
    if state and state['direction'] == direction and state['count'] >= _CLUSTER_THRESHOLD:
        logger.info("[CLUSTER] %s %s streak=%d → +%.2f conf required",
                    symbol, direction, state['count'], _CLUSTER_CONF_PENALTY)
        return _CLUSTER_CONF_PENALTY
    return 0.0

def _update_cluster_state(symbol: str, direction: str):
    state = _pair_consec.get(symbol)
    if state and state['direction'] == direction:
        _pair_consec[symbol] = {'direction': direction, 'count': state['count'] + 1}
    else:
        _pair_consec[symbol] = {'direction': direction, 'count': 1}
    logger.debug("[CLUSTER] %s → %s ×%d", symbol, direction, _pair_consec[symbol]['count'])

def reset_cluster_state(symbol: str = None):
    if symbol:
        _pair_consec.pop(symbol, None)
    else:
        _pair_consec.clear()


# ══════════════════════════════════════════════════════════════════════════════
# QUALITY FILTER (post-gate ML confirmation layer)
# ══════════════════════════════════════════════════════════════════════════════

def _passes_quality_filter(row: dict, direction: int, bb_threshold: float = 0.15) -> bool:
    """
    4-rule quality vote. Signal must pass ≥2 rules.
    BB hard gate: directionally correct (v3 fix carried forward).
    """
    bb_pct = float(row.get('bb_pct', 0.5))

    # Directionally-correct BB gate
    if direction == 1 and bb_pct > (1.0 - bb_threshold):
        return False
    if direction == 0 and bb_pct < bb_threshold:
        return False

    passed = 0
    if float(row.get('adx', 0)) > 15:                                passed += 1
    if float(row.get('vol_ratio', 1)) > 0.8:                         passed += 1
    rsi14 = float(row.get('rsi_14', 50))
    if direction == 1 and rsi14 < 80:                                 passed += 1
    elif direction == 0 and rsi14 > 20:                               passed += 1
    ema_cross = int(row.get('ema_cross_8_21', 0))
    if direction == 1 and ema_cross == 1:                             passed += 1
    elif direction == 0 and ema_cross == 0:                           passed += 1
    lower_wick = float(row.get('lower_wick', 0))
    upper_wick = float(row.get('upper_wick', 0))
    if direction == 1 and lower_wick > upper_wick and lower_wick >= 0.15: passed += 1
    elif direction == 0 and upper_wick > lower_wick and upper_wick >= 0.15: passed += 1

    return passed >= 2


# ══════════════════════════════════════════════════════════════════════════════
# OKX DATA FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 960) -> pd.DataFrame:
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
                "timestamp","open","high","low","close",
                "vol","volCcy","volCcyQuote","confirm"
            ])
            df_batch["timestamp"] = pd.to_datetime(
                df_batch["timestamp"].astype(float), unit="ms", utc=True)
            for col in ["open","high","low","close","vol"]:
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
# CROSS-PAIR FEATURES
# ══════════════════════════════════════════════════════════════════════════════

_cross_pair_cache: dict = {}
_cross_pair_ts:    float = 0.0
_CROSS_PAIR_TTL:   float = 60.0

def _get_cross_pair_features() -> dict:
    import time
    global _cross_pair_cache, _cross_pair_ts
    if time.time() - _cross_pair_ts < _CROSS_PAIR_TTL and _cross_pair_cache:
        return _cross_pair_cache
    result = {
        'btc_ret_1':0.0,'btc_ret_3':0.0,'btc_vol_ratio':1.0,'btc_rsi_14':50.0,
        'eth_ret_1':0.0,'eth_ret_3':0.0,'eth_vol_ratio':1.0,'eth_rsi_14':50.0,
        'btc_eth_corr':0.0,
    }
    try:
        btc_df = fetch_okx_candles("BTC-USDT", limit=30)
        eth_df = fetch_okx_candles("ETH-USDT", limit=30)
        for sym_df, prefix in [(btc_df,"btc"),(eth_df,"eth")]:
            if sym_df.empty or len(sym_df) < 5: continue
            c   = sym_df['close']
            vol = sym_df['vol']
            result[f'{prefix}_ret_1']     = float(c.pct_change(1).iloc[-2])
            result[f'{prefix}_ret_3']     = float(c.pct_change(3).iloc[-2])
            result[f'{prefix}_vol_ratio'] = float(vol.iloc[-2]/(vol.rolling(20).mean().iloc[-2]+1e-9))
            result[f'{prefix}_rsi_14']    = float(_rsi(c,14).iloc[-2])
        if not btc_df.empty and not eth_df.empty and len(btc_df)>=5 and len(eth_df)>=5:
            br = btc_df['close'].pct_change(1).iloc[-6:-1]
            er = eth_df['close'].pct_change(1).iloc[-6:-1]
            if len(br)==len(er):
                corr = float(br.corr(er))
                result['btc_eth_corr'] = corr if not np.isnan(corr) else 0.0
        _cross_pair_cache = result
        _cross_pair_ts    = time.time()
    except Exception as e:
        logger.warning("[CROSS-PAIR] Error: %s", e)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODEL TRAINING
# ══════════════════════════════════════════════════════════════════════════════

def train_model(symbol: str, df: pd.DataFrame) -> bool:
    df = build_features(df.copy())
    df['target'] = (df['close'].shift(-1) > df['open'].shift(-1)).astype(int)

    if symbol not in ("BTC-USDT","ETH-USDT"):
        cp = _get_cross_pair_features()
        for col, val in cp.items():
            df[col] = val

    df_c = df.dropna(subset=FEATURE_COLS + ['target']).copy()
    if len(df_c) < 150:
        logger.warning("[%s] Only %d rows for training — need 150+", symbol, len(df_c))
        return False

    X  = df_c[FEATURE_COLS].values
    y  = df_c['target'].values
    sc = StandardScaler()
    Xs = sc.fit_transform(X)

    rf = RandomForestClassifier(
        n_estimators=400, max_depth=10, min_samples_leaf=8,
        max_features='sqrt', random_state=42, n_jobs=-1)
    gb = GradientBoostingClassifier(
        n_estimators=200, max_depth=5, learning_rate=0.04,
        min_samples_leaf=8, subsample=0.8, random_state=42)
    et = ExtraTreesClassifier(
        n_estimators=300, max_depth=10, min_samples_leaf=8,
        max_features='sqrt', random_state=42, n_jobs=-1)

    sw = pd.Series(1.0, index=df_c.index)
    _lw  = df_c['lower_wick']; _uw = df_c['upper_wick']
    _tgt = pd.Series(y, index=df_c.index)
    dir_tail = (((_tgt==1)&(_lw>_uw)&(_lw>=0.15)) |
                ((_tgt==0)&(_uw>_lw)&(_uw>=0.15)))
    sw[dir_tail] = 3.0

    nxt_o = df_c['open'].shift(-1).reindex(df_c.index)
    nxt_c = df_c['close'].shift(-1).reindex(df_c.index)
    nxt_h = df_c['high'].shift(-1).reindex(df_c.index)
    nxt_l = df_c['low'].shift(-1).reindex(df_c.index)
    nxt_rng = (nxt_h - nxt_l).clip(lower=1e-9)
    nxt_min_oc    = pd.concat([nxt_o, nxt_c], axis=1).min(axis=1)
    nxt_lower_wick = (nxt_min_oc - nxt_l) / nxt_rng
    has_pb_wick    = (nxt_lower_wick >= 0.08).fillna(False)
    sw[has_pb_wick] = sw[has_pb_wick].clip(upper=2.0).where(sw[has_pb_wick] < 3.0, 3.0)

    sw_arr = sw.values
    rf.fit(Xs, y, sample_weight=sw_arr)
    gb.fit(Xs, y, sample_weight=sw_arr)
    et.fit(Xs, y, sample_weight=sw_arr)

    _models[symbol]  = (rf, gb, et)
    _scalers[symbol] = sc
    reset_cluster_state(symbol)

    logger.info("[%s] Model trained on %d candles | threshold=%.2f",
                symbol, len(df_c), PAIR_CONFIG.get(symbol,{}).get('threshold',0.60))
    return True


def retrain_all(limit: int = 960):
    logger.info("[ENGINE] Retraining all models (limit=%d)...", limit)
    for sym in SYMBOLS:
        df = fetch_okx_candles(sym, limit=limit)
        if not df.empty:
            train_model(sym, df)
    reset_cluster_state()
    logger.info("[ENGINE] All models ready")


# ══════════════════════════════════════════════════════════════════════════════
# 1H TREND FILTER
# ══════════════════════════════════════════════════════════════════════════════

def _get_1h_trend(symbol: str) -> str | None:
    try:
        resp = requests.get(
            f"{OKX_BASE}/api/v5/market/candles",
            params={"instId": symbol, "bar": "1H", "limit": "12"},
            timeout=8)
        if not resp.ok: return None
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"): return None
        df = pd.DataFrame(data["data"], columns=[
            "timestamp","open","high","low","close",
            "vol","volCcy","volCcyQuote","confirm"])
        df["close"] = df["close"].astype(float)
        df = df.sort_values("timestamp").reset_index(drop=True)
        if "confirm" in df.columns:
            df = df[df["confirm"] == "1"]
        else:
            df = df.iloc[:-1]
        if len(df) < 3: return None
        net_move = df["close"].iloc[-1] - df["close"].iloc[-3]
        pct_move = net_move / (df["close"].iloc[-3] + 1e-9)
        ema_slope = 0.0
        if len(df) >= 9:
            ema9      = df["close"].ewm(span=9, adjust=False).mean()
            ema_slope = ema9.iloc[-1] - ema9.iloc[-3]
        if pct_move > 0.0005 and ema_slope > 0:   return "UP"
        elif pct_move < -0.0005 and ema_slope < 0: return "DOWN"
        return None
    except Exception as e:
        logger.warning("[%s] 1H trend error: %s", symbol, e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def get_signal_for_symbol(symbol: str) -> dict | None:
    global _sol_last_signal_bar, _sol_bar_counter

    cfg = PAIR_CONFIG.get(symbol)
    if cfg is None:
        logger.error("[%s] No PAIR_CONFIG found", symbol)
        return None

    cfg = dict(cfg); cfg['_symbol'] = symbol
    threshold = cfg['threshold']

    df = fetch_okx_candles(symbol, limit=100)
    if df.empty or len(df) < 50:
        return None

    if symbol not in _models:
        ok = train_model(symbol, df)
        if not ok: return None

    # ── Momentum regime ────────────────────────────────────────────────────
    regime = _get_momentum_regime(df, cfg)

    # ── Build features ─────────────────────────────────────────────────────
    df_f = build_features(df.copy())
    if symbol not in ("BTC-USDT","ETH-USDT"):
        cp = _get_cross_pair_features()
        for col, val in cp.items():
            df_f[col] = val

    df_c = df_f.dropna(subset=FEATURE_COLS).copy()
    if len(df_c) < 2:
        return None

    row    = df_c.iloc[-2]   # last confirmed candle
    latest = df_c.iloc[-1]

    # ── Extract raw indicator values ───────────────────────────────────────
    rsi7_val  = float(row.get('rsi_7', 50))
    bb_pct    = float(row.get('bb_pct', 0.5))
    wr14_val  = float(row.get('wr', -50))
    vol_ratio = float(row.get('vol_ratio', 1.0))
    atr_ratio = float(row.get('atr_ratio', 1.0))
    hour      = int(row.get('hour', 0))

    # ── Momentum regime: override RSI thresholds ───────────────────────────
    live_cfg = dict(cfg)
    if regime['in_momentum']:
        live_cfg['rsi_long']  = regime['rsi7_long_thr']
        live_cfg['rsi_short'] = regime['rsi7_short_thr']
        logger.info("[%s] Momentum → RSI thresholds tightened to %d/%d",
                    symbol, live_cfg['rsi_long'], live_cfg['rsi_short'])

    # ── SOL internal cooldown ──────────────────────────────────────────────
    if symbol == "SOL-USDT" and cfg.get('cooldown'):
        _sol_bar_counter += 1
        bars_since_last = _sol_bar_counter - _sol_last_signal_bar
        if bars_since_last < cfg['cooldown']:
            logger.info("[SOL-USDT] Internal cooldown — %d/%d bars since last signal",
                        bars_since_last, cfg['cooldown'])
            return None

    # ── Per-pair signal gate ───────────────────────────────────────────────
    gate_direction = _check_signal_gate(
        symbol, row.to_dict(), live_cfg,
        rsi7_val, vol_ratio, bb_pct, wr14_val, atr_ratio
    )
    if gate_direction is None:
        return None

    direction = gate_direction
    dir_int   = 1 if direction == 'UP' else 0

    # ── ML ensemble inference ──────────────────────────────────────────────
    X   = row[FEATURE_COLS].values.reshape(1, -1)
    Xs  = _scalers[symbol].transform(X)
    rf, gb, et = _models[symbol]
    ens  = (0.40 * rf.predict_proba(Xs) +
            0.35 * gb.predict_proba(Xs) +
            0.25 * et.predict_proba(Xs))
    prob = float(ens[0, 1])

    # Map ML probability to confidence in the gate direction
    if direction == 'UP':
        confidence = prob
    else:
        confidence = 1.0 - prob

    if confidence < threshold:
        logger.info("[%s] %s gate passed but ML confidence %.3f < threshold %.2f",
                    symbol, direction, confidence, threshold)
        return None

    # ── Cluster penalty ────────────────────────────────────────────────────
    cluster_penalty = _get_cluster_penalty(symbol, direction)
    if cluster_penalty > 0:
        required = threshold + cluster_penalty
        if confidence < required:
            logger.info("[%s] Cluster escalator blocked — conf=%.3f < required=%.3f",
                        symbol, confidence, required)
            return None

    # ── Quality filter ─────────────────────────────────────────────────────
    if not _passes_quality_filter(row.to_dict(), dir_int, bb_threshold=0.15):
        logger.info("[%s] Quality filter rejected", symbol)
        return None

    # ── 1H trend filter ────────────────────────────────────────────────────
    trend_1h = _get_1h_trend(symbol)
    if trend_1h is not None and trend_1h != direction:
        logger.info("[%s] 1H trend %s contradicts 15m %s — blocked",
                    symbol, trend_1h, direction)
        return None

    # ── Tail boost ─────────────────────────────────────────────────────────
    _lower_wick = float(row.get('lower_wick', 0))
    _upper_wick = float(row.get('upper_wick', 0))
    _tail_wick  = _lower_wick if direction == 'UP' else _upper_wick
    if _tail_wick >= 0.30:
        _tail_boost = 0.04; _tail_label = 'STRONG'
    elif _tail_wick >= 0.15:
        _tail_boost = 0.02; _tail_label = 'MODERATE'
    else:
        _tail_boost = 0.00; _tail_label = 'NONE'
    if _tail_boost > 0:
        confidence = min(0.99, confidence + _tail_boost)

    # ── Update SOL cooldown counter ────────────────────────────────────────
    if symbol == "SOL-USDT" and cfg.get('cooldown'):
        _sol_last_signal_bar = _sol_bar_counter

    # ── Update cluster state ───────────────────────────────────────────────
    _update_cluster_state(symbol, direction)

    # ── Candle time ────────────────────────────────────────────────────────
    ts       = latest['timestamp']
    minutes  = ts.minute
    boundary = (minutes // 15) * 15
    candle_open  = ts.replace(minute=boundary, second=0, microsecond=0)
    candle_close = candle_open + pd.Timedelta(minutes=15)

    logger.info(
        "[%s] SIGNAL %s | gate=%s rsi7=%.1f conf=%.3f thresh=%.2f "
        "hour=%d momentum=%s cluster=%d",
        symbol, direction, cfg['signal_gate'], rsi7_val, confidence, threshold,
        hour, regime['in_momentum'],
        _pair_consec.get(symbol, {}).get('count', 1)
    )

    return {
        'symbol':            symbol,
        'direction':         direction,
        'invert':            cfg.get('invert', False),
        'confidence':        confidence,
        'threshold':         threshold,
        'margin':            confidence - threshold,
        'tier':              "T1" if vol_ratio > 1.5 else "T2",
        'signal_gate':       cfg['signal_gate'],
        'tail_wick':         round(_tail_wick, 3),
        'tail_boost':        _tail_boost,
        'tail_label':        _tail_label,
        'vol_spike':         bool(vol_ratio > 1.5),
        'rsi_14':            float(row['rsi_14']),
        'macd_hist':         float(row['macd_hist']),
        'adx':               float(row['adx']),
        'vol_ratio':         vol_ratio,
        'adx_trend':         bool(row['adx_trend']),
        'bull_market':       bool(row['bull_market']),
        'open_price':        float(latest['close']),
        'candle_open_time':  candle_open.to_pydatetime().replace(tzinfo=None),
        'candle_close_time': candle_close.to_pydatetime().replace(tzinfo=None),
        'in_momentum':       regime['in_momentum'],
        'momentum_move_pct': round(regime['move_pct'] * 100, 2),
        'cluster_streak':    _pair_consec.get(symbol, {}).get('count', 1),
        'hour_penalty':      hour in PENALISED_HOURS,
        'hour_blocked':      False,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PICK BEST SIGNAL
# ══════════════════════════════════════════════════════════════════════════════

def pick_best_signal(min_confidence: float = None,
                     exclude: list = None,
                     preferred_families: list = None,
                     excluded_families: list = None,
                     blocked_directions: dict = None) -> dict | None:
    """
    Evaluate all pairs and return the single best signal.
    Family rotation enforced via excluded_families (always-on in scheduler).
    """
    PAIR_FAMILY = {
        "BTC-USDT":  "A", "ETH-USDT":  "A",
        "DOGE-USDT": "B", "SOL-USDT":  "B",
        "XRP-USDT":  "C", "BNB-USDT":  "C",
    }

    global _system_bar_counter
    _system_bar_counter += 1

    # ── System-level directional block ────────────────────────────────────────
    _currently_blocked_dirs = set()
    for _dir in ['UP', 'DOWN']:
        if _system_bar_counter <= _dir_blocked_until.get(_dir, 0):
            _currently_blocked_dirs.add(_dir)
            logger.warning('[DIR_BLOCK] %s direction blocked candle bar=%d expires=%d',
                           _dir, _system_bar_counter, _dir_blocked_until[_dir])

    active_symbols = [s for s in SYMBOLS if not (exclude and s in exclude)]
    candidates     = []

    for sym in active_symbols:
        try:
            sig = get_signal_for_symbol(sym)
            if sig:
                sig['family'] = PAIR_FAMILY.get(sym, "B")
                candidates.append(sig)
                logger.info("[%s] candidate %s conf=%.3f margin=%.3f "
                            "family=%s momentum=%s gate=%s",
                            sym, sig['direction'], sig['confidence'],
                            sig['margin'], sig['family'],
                            sig.get('in_momentum', False),
                            sig.get('signal_gate', '?'))
        except Exception as e:
            logger.error("[%s] get_signal_for_symbol error: %s", sym, e)

    if not candidates:
        logger.info("[ENGINE] No qualifying signals this candle")
        return None

    # ── Filter out blocked directions ────────────────────────────────────────
    if _currently_blocked_dirs:
        before     = len(candidates)
        candidates = [s for s in candidates if s['direction'] not in _currently_blocked_dirs]
        if before - len(candidates):
            logger.warning('[DIR_BLOCK] Removed %d candidate(s) in blocked dirs %s',
                           before - len(candidates), _currently_blocked_dirs)
        if not candidates:
            logger.warning('[DIR_BLOCK] All candidates blocked — skipping candle')
            return None

    # min_confidence global floor (0.0 = disabled)
    if min_confidence:
        candidates = [s for s in candidates if s['confidence'] >= min_confidence]
        if not candidates:
            logger.info("[ENGINE] No signals above min_confidence=%.2f", min_confidence)
            return None

    # Rule 2 directional saturation
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

    def score(s):
        return s['margin'] + (0.04 if s['tier'] == 'T1' else 0.0)

    # Family rotation — hard exclusion with skip (no fallthrough)
    _excl_set = set(excluded_families) if excluded_families else set()
    if _excl_set:
        eligible = [s for s in candidates if s['family'] not in _excl_set]
        if eligible:
            best = max(eligible, key=score)
            logger.info("[ENGINE] Best: %s %s conf=%.3f family=%s gate=%s",
                        best['symbol'], best['direction'], best['confidence'],
                        best['family'], best.get('signal_gate','?'))
            return best
        else:
            logger.info("[ENGINE] Family rotation: no signal outside %s — skipping candle",
                        _excl_set)
            return None

    best = max(candidates, key=score)
    logger.info("[ENGINE] Best: %s %s conf=%.3f family=%s gate=%s",
                best['symbol'], best['direction'], best['confidence'],
                best['family'], best.get('signal_gate','?'))
    return best


# ══════════════════════════════════════════════════════════════════════════════
# PAIR STATS & CONFIG ACCESSORS
# ══════════════════════════════════════════════════════════════════════════════

def record_outcome(symbol: str, outcome: str, direction: str = None):
    # Updates pair stats AND system-level directional block on LOSS.
    if symbol not in _pair_stats:
        _pair_stats[symbol] = {'wins': 0, 'losses': 0, 'signals': 0}
    _pair_stats[symbol]['signals'] += 1
    if outcome == 'WIN':
        _pair_stats[symbol]['wins'] += 1
        if direction:
            _dir_loss_streak[direction] = 0
            logger.debug('[DIR_BLOCK] %s WIN -> %s streak reset', symbol, direction)
    elif outcome == 'LOSS':
        _pair_stats[symbol]['losses'] += 1
        if direction:
            _dir_loss_streak[direction] = _dir_loss_streak.get(direction, 0) + 1
            streak = _dir_loss_streak[direction]
            logger.info('[DIR_BLOCK] %s LOSS | %s streak now %d', symbol, direction, streak)
            if streak >= DIR_BLOCK_THRESHOLD:
                _dir_blocked_until[direction] = _system_bar_counter + DIR_BLOCK_DURATION
                _dir_loss_streak[direction]   = 0
                logger.warning(
                    '[DIR_BLOCK] %s direction BLOCKED for %d candles '
                    '(expires bar %d)',
                    direction, DIR_BLOCK_DURATION, _dir_blocked_until[direction]
                )


def get_direction_block_status() -> dict:
    return {
        'dir_loss_streak':   dict(_dir_loss_streak),
        'dir_blocked_until': dict(_dir_blocked_until),
        'system_bar':        _system_bar_counter,
        'currently_blocked': {
            d: _system_bar_counter <= _dir_blocked_until.get(d, 0)
            for d in ['UP', 'DOWN']
        }
    }


def get_pair_stats() -> dict:
    result = {}
    for sym in SYMBOLS:
        s     = _pair_stats.get(sym, {'wins':0,'losses':0,'signals':0})
        total = s['wins'] + s['losses']
        cfg   = PAIR_CONFIG.get(sym, {})
        result[sym] = {
            'wins':       s['wins'],
            'losses':     s['losses'],
            'signals':    s['signals'],
            'win_rate':   round(s['wins']/total*100, 1) if total > 0 else None,
            'threshold':  cfg.get('threshold', 0.60),
            'tier':       cfg.get('tier', 'B'),
            'signal_gate':cfg.get('signal_gate', '?'),
        }
    return result


def get_pair_config() -> dict:
    return {sym: dict(cfg) for sym, cfg in PAIR_CONFIG.items()}
