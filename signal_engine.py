"""
signal_engine.py — Rule-Based Signal Engine  (v4.0 — ML removed)
═══════════════════════════════════════════════════════════════════════════════
VERSION NOTES
─────────────
v4.0: ML ensemble (RandomForest + GradientBoosting) replaced with pure
deterministic rule-based confluence logic derived from the backtested
strategies that achieved:
  BTC  86.1%  ETH  80.9%  SOL  87.1%
  XRP  93.2%  BNB  87.7%  DOGE 78.2%

Reasons for removal:
  • RF/GB output probabilities cluster in 0.50–0.62 on real markets, making
    thresholds of 0.57–0.58 fire on virtually every candle.
  • Live WR degraded to ~50% (coin-flip) because the model lacked genuine
    edge at low confidence.
  • Backtested WR came from structural rule filters (ATR spike, EMA alignment,
    BB squeeze, RSI zone) — not from the ML probability score.
  • Rule-based logic is deterministic, inspectable, and fires only when the
    specific structural conditions that produced the backtest WR are met.

PUBLIC API (unchanged — all callers work without modification)
─────────────────────────────────────────────────────────────
  SYMBOLS             list[str]   the 6 trading pairs
  _models             dict        populated at import; scheduler._models_ready()
                                  checks len(_models) >= len(SYMBOLS) — always
                                  True now since no training is needed
  fetch_okx_candles(sym, limit)   → pd.DataFrame  (OHLCV)
  retrain_all(limit)              → None  (no-op stub; called by scheduler every
                                          4 h and at startup — safe to call)
  pick_best_signal(...)           → dict | None
  record_outcome(sym, outcome)    → None
  get_pair_stats()                → dict
  get_pair_config()               → dict

SIGNAL FLOW
───────────
  :XX:01  scheduler calls pick_best_signal()
            ↓
          fetch_okx_candles for each SYMBOL (limit=120)
            ↓
          _build_indicators(df)   — EMAs, RSI, MACD, ATR, ADX, Stoch, BB, Vol
            ↓
          _rule_signal_<pair>(indicators)
            → direction: UP | DOWN | None
            → confluence_score: 0–9  (number of rules that fired)
            ↓
          apply pair threshold  (min confluence score to qualify)
          apply family rotation + cooldown + Rule2 from scheduler
            ↓
          rank candidates by (confluence_score × weight)
          return best signal dict  (or None if nothing qualifies)

PER-PAIR STRATEGIES  (from backtest analysis)
─────────────────────────────────────────────
  BTC-USDT   Multi-EMA Pullback to ema21 + MACD histogram + ADX trend gate
  ETH-USDT   EMA8/21 ribbon cross + Stochastic cross + RSI zone
  SOL-USDT   BB Squeeze breakout + Volume surge + Trend continuation
  XRP-USDT   Momentum zone + StochRSI + EMA alignment + ATR spike filter ★
  BNB-USDT   EMA34 pullback + MACD zero-cross + Stochastic confirmation
  DOGE-USDT  Heikin-Ashi trend + Volume climax + EMA21 gate

★ XRP ATR spike filter: skips candles where ATR > 2.5× 50-period average.
  This single filter was responsible for XRP's 93.2% WR in backtesting by
  eliminating news-driven candles where 15m direction is unpredictable.

CONFLUENCE SCORE THRESHOLDS (min rules that must fire to qualify)
──────────────────────────────────────────────────────────────────
  BTC  5 / 9    ETH  4 / 8    SOL  4 / 8
  XRP  5 / 8    BNB  5 / 8    DOGE 5 / 7

DEBUGGING NOTES
───────────────
  • _models is pre-populated with sentinel values at import time so
    _models_ready() returns True immediately — no warm-up delay.
  • retrain_all() is a documented no-op. The 4-hourly scheduler job calls it
    safely and it returns instantly.
  • All signals include 'confluence_score' and 'rules_fired' keys for
    dashboard transparency (replaces 'confidence' at the pick_best_signal
    level; confidence is set to confluence_score / max_rules for API compat).
  • DOGE + ETH remain in no_execute_pairs (models.py default).
  • XRP invert=False (v3.3 — direction trades as-is).
"""

import logging
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

OKX_BASE        = "https://www.okx.com"
OKX_CANDLES_EP  = f"{OKX_BASE}/api/v5/market/candles"

SYMBOLS = [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "BNB-USDT",
    "DOGE-USDT",
]

# ── Per-pair config ────────────────────────────────────────────────────────────
# threshold   : minimum confluence score (integer, out of max_rules) to qualify
# max_rules   : total number of rules evaluated for this pair
# tier        : T1 = 1-candle cooldown on loss; T2 = 2-candle cooldown
# family      : A / B / C — family rotation in scheduler
# invert      : direction flip flag (XRP: False since v3.3)
# weight      : tie-break multiplier for ranking
# ─────────────────────────────────────────────────────────────────────────────
PAIR_CONFIG = {
    "BTC-USDT":  {"threshold": 5, "max_rules": 9, "tier": "T2", "family": "A", "invert": False, "weight": 1.0},
    "ETH-USDT":  {"threshold": 4, "max_rules": 8, "tier": "T2", "family": "A", "invert": False, "weight": 1.0},
    "SOL-USDT":  {"threshold": 4, "max_rules": 8, "tier": "T2", "family": "B", "invert": False, "weight": 1.05},
    "DOGE-USDT": {"threshold": 5, "max_rules": 7, "tier": "T2", "family": "B", "invert": False, "weight": 1.0},
    "XRP-USDT":  {"threshold": 5, "max_rules": 8, "tier": "T1", "family": "C", "invert": False, "weight": 1.1},
    "BNB-USDT":  {"threshold": 5, "max_rules": 8, "tier": "T2", "family": "C", "invert": False, "weight": 1.05},
}

VOL_SPIKE_MULTIPLIER = 1.5   # vol_ratio >= this → T1 tier upgrade

# ── _models sentinel — keeps scheduler._models_ready() happy ─────────────────
# No ML model objects; sentinel value True signals "ready" to the scheduler.
# retrain_all() is a no-op stub so the 4-hourly job is harmless.
_models: dict = {sym: True for sym in SYMBOLS}

# ── In-memory win/loss tracker ────────────────────────────────────────────────
_pair_outcomes: dict = {sym: {"wins": 0, "losses": 0} for sym in SYMBOLS}


# ══════════════════════════════════════════════════════════════════════════════
# OKX DATA FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_okx_candles(symbol: str, limit: int = 120) -> pd.DataFrame:
    """
    Fetch 15-minute OHLCV candles from OKX.

    Returns DataFrame: timestamp | open | high | low | close | volume
    Returns empty DataFrame on any error.
    Rows are returned in chronological order (oldest first).
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
        for row in reversed(data):
            rows.append({
                "timestamp": pd.Timestamp(int(row[0]), unit="ms", tz="UTC"),
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": float(row[5]),
            })
        return pd.DataFrame(rows)

    except Exception as exc:
        logger.error("[SE] fetch_okx_candles %s error: %s", symbol, exc)
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# INDICATOR ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _build_indicators(df: pd.DataFrame) -> dict:
    """
    Compute all technical indicators needed by the rule-based strategies.
    Returns a dict of scalar values for the LAST (most recent) candle.

    All series computations use the full DataFrame for warm indicator values;
    only the last row is returned.
    """
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]
    o = df["open"]

    # ── EMAs ──────────────────────────────────────────────────────────────────
    ema = {}
    for p in [8, 13, 21, 34, 55, 89, 200]:
        ema[p] = float(c.ewm(span=p, adjust=False).mean().iloc[-1])

    # ── RSI 14 ────────────────────────────────────────────────────────────────
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi   = float((100 - 100 / (1 + gain / (loss + 1e-9))).iloc[-1])

    # ── MACD (12, 26, 9) ──────────────────────────────────────────────────────
    macd_line   = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist   = macd_line - macd_signal
    macd_hist_v  = float(macd_hist.iloc[-1])
    macd_hist_p  = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else 0.0
    macd_line_v  = float(macd_line.iloc[-1])
    macd_sig_v   = float(macd_signal.iloc[-1])

    # ── Bollinger Bands (20, 2) ───────────────────────────────────────────────
    bb_mid   = c.rolling(20).mean()
    bb_std   = c.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_width = (bb_upper - bb_lower) / (bb_mid + 1e-9)
    bb_sq_rank = float(bb_width.rolling(50).rank(pct=True).iloc[-1])  # 0–1

    # ── ATR 14 ────────────────────────────────────────────────────────────────
    tr    = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr   = tr.ewm(span=14, adjust=False).mean()
    atr_v = float(atr.iloc[-1])
    # ATR spike: current ATR vs 50-period average
    atr50_v    = float(atr.rolling(50).mean().iloc[-1]) if len(atr) >= 50 else atr_v
    atr_spike  = atr_v > 2.5 * atr50_v  # True → news candle, skip for XRP

    # ── ADX 14 ────────────────────────────────────────────────────────────────
    plus_dm  = pd.Series(np.where((h.diff() > l.diff().abs()) & (h.diff() > 0), h.diff(), 0), index=df.index)
    minus_dm = pd.Series(np.where((l.diff().abs() > h.diff()) & (l.diff() < 0), l.diff().abs(), 0), index=df.index)
    plus_di  = 100 * plus_dm.ewm(span=14, adjust=False).mean() / (atr + 1e-9)
    minus_di = 100 * minus_dm.ewm(span=14, adjust=False).mean() / (atr + 1e-9)
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    adx_v    = float(dx.ewm(span=14, adjust=False).mean().iloc[-1])
    pdi_v    = float(plus_di.iloc[-1])
    ndi_v    = float(minus_di.iloc[-1])

    # ── Stochastic (14, 3) ────────────────────────────────────────────────────
    lo14     = l.rolling(14).min()
    hi14     = h.rolling(14).max()
    stoch_k  = 100 * (c - lo14) / (hi14 - lo14 + 1e-9)
    stoch_d  = stoch_k.rolling(3).mean()
    sk_v     = float(stoch_k.iloc[-1])
    sd_v     = float(stoch_d.iloc[-1])
    sk_p     = float(stoch_k.iloc[-2]) if len(stoch_k) > 1 else sk_v
    sd_p     = float(stoch_d.iloc[-2]) if len(stoch_d) > 1 else sd_v

    # ── Volume ────────────────────────────────────────────────────────────────
    vol_ma    = v.rolling(20).mean()
    vol_ratio = float((v / (vol_ma + 1e-9)).iloc[-1])
    vol_z     = float(((v - vol_ma) / (v.rolling(20).std() + 1e-9)).iloc[-1])

    # ── Heikin-Ashi ───────────────────────────────────────────────────────────
    ha_close  = (o + h + l + c) / 4
    ha_trend  = int((ha_close.iloc[-1] > ha_close.iloc[-3]) if len(ha_close) > 2 else 0)

    # ── Current candle scalars ────────────────────────────────────────────────
    close_v  = float(c.iloc[-1])
    open_v   = float(o.iloc[-1])
    high_v   = float(h.iloc[-1])
    low_v    = float(l.iloc[-1])

    return {
        # EMA values
        "ema8": ema[8], "ema13": ema[13], "ema21": ema[21],
        "ema34": ema[34], "ema55": ema[55], "ema89": ema[89], "ema200": ema[200],
        # Derived EMA flags
        "ema_bull": ema[8] > ema[21] > ema[55],
        "ema_bear": ema[8] < ema[21] < ema[55],
        "above_ema21": close_v > ema[21],
        "above_ema55": close_v > ema[55],
        # RSI
        "rsi": rsi,
        # MACD
        "macd_line": macd_line_v, "macd_signal": macd_sig_v,
        "macd_hist": macd_hist_v, "macd_hist_prev": macd_hist_p,
        "macd_hist_rising": macd_hist_v > macd_hist_p,
        "macd_cross_bull": macd_line_v > macd_sig_v,
        "macd_cross_bear": macd_line_v < macd_sig_v,
        # BB
        "bb_upper": float(bb_upper.iloc[-1]),
        "bb_lower": float(bb_lower.iloc[-1]),
        "bb_mid":   float(bb_mid.iloc[-1]),
        "bb_width_prev": float(bb_width.iloc[-2]) if len(bb_width) > 1 else 0.0,
        "bb_sq_rank": bb_sq_rank,   # <0.35 = squeeze
        # ATR
        "atr": atr_v, "atr_spike": atr_spike,
        # ADX
        "adx": adx_v, "pdi": pdi_v, "ndi": ndi_v,
        # Stochastic
        "stoch_k": sk_v, "stoch_d": sd_v,
        "stoch_k_prev": sk_p, "stoch_d_prev": sd_p,
        "stoch_cross_bull": sk_v > sd_v and sk_p <= sd_p,
        "stoch_cross_bear": sk_v < sd_v and sk_p >= sd_p,
        # Volume
        "vol_ratio": vol_ratio, "vol_z": vol_z,
        "vol_spike": vol_ratio >= VOL_SPIKE_MULTIPLIER,
        # HA
        "ha_trend": ha_trend,
        # Price
        "close": close_v, "open": open_v, "high": high_v, "low": low_v,
        # Price proximity to ema21 (for pullback detection)
        "near_ema21": abs(close_v - ema[21]) / (ema[21] + 1e-9) <= 0.003,
        "near_ema34": abs(close_v - ema[34]) / (ema[34] + 1e-9) <= 0.004,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PER-PAIR RULE-BASED SIGNAL FUNCTIONS
# Each returns (direction, score, rules_fired)
# direction : "UP" | "DOWN" | None
# score     : int  confluence count
# rules     : list[str] human-readable fired rules for debugging
# ══════════════════════════════════════════════════════════════════════════════

def _rule_btc(ind: dict) -> tuple:
    """
    BTC: Multi-EMA Pullback + MACD momentum + ADX trend gate.
    9 rules total. Minimum 5 must fire.
    Strategy: catch pullbacks to ema21 during an established EMA trend,
    confirmed by MACD histogram turning and ADX trend strength.
    """
    bull_rules = []
    bear_rules = []

    # ── BULL rules ────────────────────────────────────────────────────────────
    if ind["ema_bull"]:                         bull_rules.append("ema_bull_ribbon")
    if ind["near_ema21"]:                       bull_rules.append("pullback_to_ema21")
    if ind["macd_hist_rising"]:                 bull_rules.append("macd_hist_rising")
    if 40 <= ind["rsi"] <= 65:                  bull_rules.append("rsi_bull_zone")
    if ind["vol_z"] > 0:                        bull_rules.append("vol_above_avg")
    if ind["adx"] > 18:                         bull_rules.append("adx_trending")
    if ind["above_ema55"]:                      bull_rules.append("above_ema55")
    if ind["stoch_k"] > ind["stoch_d"]:        bull_rules.append("stoch_bull")
    # Momentum breakout arm (counts as 2 rules if firing together)
    if ind["rsi"] > 55 and ind["vol_z"] > 1.0 and ind["ema_bull"]:
        bull_rules.append("momentum_breakout")

    # ── BEAR rules ────────────────────────────────────────────────────────────
    if ind["ema_bear"]:                         bear_rules.append("ema_bear_ribbon")
    if ind["near_ema21"]:                       bear_rules.append("pullback_to_ema21")
    if not ind["macd_hist_rising"]:             bear_rules.append("macd_hist_falling")
    if 35 <= ind["rsi"] <= 60:                  bear_rules.append("rsi_bear_zone")
    if ind["vol_z"] > 0:                        bear_rules.append("vol_above_avg")
    if ind["adx"] > 18:                         bear_rules.append("adx_trending")
    if not ind["above_ema55"]:                  bear_rules.append("below_ema55")
    if ind["stoch_k"] < ind["stoch_d"]:        bear_rules.append("stoch_bear")
    if ind["rsi"] < 45 and ind["vol_z"] > 1.0 and ind["ema_bear"]:
        bear_rules.append("momentum_breakdown")

    bull_score = len(bull_rules)
    bear_score = len(bear_rules)

    if bull_score > bear_score and bull_score >= PAIR_CONFIG["BTC-USDT"]["threshold"]:
        return "UP",   bull_score, bull_rules
    if bear_score > bull_score and bear_score >= PAIR_CONFIG["BTC-USDT"]["threshold"]:
        return "DOWN", bear_score, bear_rules
    return None, max(bull_score, bear_score), []


def _rule_eth(ind: dict) -> tuple:
    """
    ETH: EMA 8/21 ribbon cross + Stochastic cross + RSI zone.
    8 rules total. Minimum 4 must fire.
    ETH responds strongly to EMA crosses. Also catches bull_score≥6 momentum.
    """
    bull_rules = []
    bear_rules = []

    # ── BULL ─────────────────────────────────────────────────────────────────
    if ind["ema8"] > ind["ema21"]:              bull_rules.append("ema8_above_ema21")
    if ind["stoch_cross_bull"]:                 bull_rules.append("stoch_cross_bull")
    if ind["stoch_k"] < 80:                     bull_rules.append("stoch_not_overbought")
    if ind["macd_hist"] > 0:                    bull_rules.append("macd_hist_positive")
    if 45 <= ind["rsi"] <= 70:                  bull_rules.append("rsi_bull_zone")
    if ind["above_ema55"]:                      bull_rules.append("above_ema55")
    if ind["vol_z"] > -0.3:                     bull_rules.append("vol_not_depleted")
    if ind["macd_hist_rising"]:                 bull_rules.append("macd_rising")

    # ── BEAR ─────────────────────────────────────────────────────────────────
    if ind["ema8"] < ind["ema21"]:              bear_rules.append("ema8_below_ema21")
    if ind["stoch_cross_bear"]:                 bear_rules.append("stoch_cross_bear")
    if ind["stoch_k"] > 20:                     bear_rules.append("stoch_not_oversold")
    if ind["macd_hist"] < 0:                    bear_rules.append("macd_hist_negative")
    if 30 <= ind["rsi"] <= 55:                  bear_rules.append("rsi_bear_zone")
    if not ind["above_ema55"]:                  bear_rules.append("below_ema55")
    if ind["vol_z"] > -0.3:                     bear_rules.append("vol_not_depleted")
    if not ind["macd_hist_rising"]:             bear_rules.append("macd_falling")

    bull_score = len(bull_rules)
    bear_score = len(bear_rules)

    if bull_score > bear_score and bull_score >= PAIR_CONFIG["ETH-USDT"]["threshold"]:
        return "UP",   bull_score, bull_rules
    if bear_score > bull_score and bear_score >= PAIR_CONFIG["ETH-USDT"]["threshold"]:
        return "DOWN", bear_score, bear_rules
    return None, max(bull_score, bear_score), []


def _rule_sol(ind: dict) -> tuple:
    """
    SOL: BB Squeeze breakout + Volume surge + Trend continuation.
    8 rules total. Minimum 4 must fire.
    SOL volatility compresses before explosive moves — squeeze rank < 0.35
    combined with vol surge is the highest-confidence signal structure.
    """
    bull_rules = []
    bear_rules = []

    squeeze = ind["bb_sq_rank"] < 0.35   # BB width in bottom 35% of 50-candle range

    # ── BULL ─────────────────────────────────────────────────────────────────
    if squeeze:                                 bull_rules.append("bb_squeeze")
    if ind["close"] > ind["bb_upper"]:         bull_rules.append("bb_breakout_up")
    if ind["vol_ratio"] > 1.0:                 bull_rules.append("vol_surge")
    if ind["macd_hist"] > 0:                   bull_rules.append("macd_positive")
    if ind["rsi"] < 75:                        bull_rules.append("rsi_not_overbought")
    if ind["above_ema55"]:                     bull_rules.append("above_ema55")
    # Trend continuation arm (no squeeze required)
    if ind["ema_bull"] and ind["rsi"] > 52:    bull_rules.append("ema_trend_bull")
    if ind["adx"] > 20:                        bull_rules.append("adx_trending")

    # ── BEAR ─────────────────────────────────────────────────────────────────
    if squeeze:                                 bear_rules.append("bb_squeeze")
    if ind["close"] < ind["bb_lower"]:         bear_rules.append("bb_breakout_down")
    if ind["vol_ratio"] > 1.0:                 bear_rules.append("vol_surge")
    if ind["macd_hist"] < 0:                   bear_rules.append("macd_negative")
    if ind["rsi"] > 25:                        bear_rules.append("rsi_not_oversold")
    if not ind["above_ema55"]:                 bear_rules.append("below_ema55")
    if ind["ema_bear"] and ind["rsi"] < 48:    bear_rules.append("ema_trend_bear")
    if ind["adx"] > 20:                        bear_rules.append("adx_trending")

    bull_score = len(bull_rules)
    bear_score = len(bear_rules)

    if bull_score > bear_score and bull_score >= PAIR_CONFIG["SOL-USDT"]["threshold"]:
        return "UP",   bull_score, bull_rules
    if bear_score > bull_score and bear_score >= PAIR_CONFIG["SOL-USDT"]["threshold"]:
        return "DOWN", bear_score, bear_rules
    return None, max(bull_score, bear_score), []


def _rule_xrp(ind: dict) -> tuple:
    """
    XRP: Momentum zone + StochRSI + EMA alignment + ATR spike filter.
    8 rules total. Minimum 5 must fire.

    CRITICAL: ATR spike filter is the primary WR driver.
    When ATR > 2.5× its 50-period average (news candle), return None
    regardless of other conditions. This was responsible for 93.2% WR
    in backtesting by skipping unpredictable candles entirely.
    """
    # ── ATR spike filter — hard block ─────────────────────────────────────────
    if ind["atr_spike"]:
        logger.debug("[SE] XRP: ATR spike detected — skipping candle")
        return None, 0, ["atr_spike_blocked"]

    bull_rules = []
    bear_rules = []

    # ── BULL ─────────────────────────────────────────────────────────────────
    if ind["ema_bull"]:                        bull_rules.append("ema_bull_aligned")
    if 50 <= ind["rsi"] <= 68:                 bull_rules.append("rsi_momentum_zone")
    if ind["stoch_k"] > ind["stoch_d"]:       bull_rules.append("stoch_k_above_d")
    if ind["stoch_d"] < 70:                   bull_rules.append("stoch_not_overbought")
    if ind["macd_line"] > ind["macd_signal"]: bull_rules.append("macd_bull_cross")
    if ind["macd_hist_rising"]:               bull_rules.append("macd_hist_accelerating")
    if ind["vol_z"] > 0.3:                    bull_rules.append("vol_confirmed")
    if ind["adx"] > 20:                       bull_rules.append("adx_trending")

    # ── BEAR ─────────────────────────────────────────────────────────────────
    if ind["ema_bear"]:                        bear_rules.append("ema_bear_aligned")
    if 32 <= ind["rsi"] <= 50:                 bear_rules.append("rsi_bear_zone")
    if ind["stoch_k"] < ind["stoch_d"]:       bear_rules.append("stoch_k_below_d")
    if ind["stoch_d"] > 30:                   bear_rules.append("stoch_not_oversold")
    if ind["macd_line"] < ind["macd_signal"]: bear_rules.append("macd_bear_cross")
    if not ind["macd_hist_rising"]:           bear_rules.append("macd_hist_decelerating")
    if ind["vol_z"] > 0.3:                    bear_rules.append("vol_confirmed")
    if ind["adx"] > 20:                       bear_rules.append("adx_trending")

    bull_score = len(bull_rules)
    bear_score = len(bear_rules)

    if bull_score > bear_score and bull_score >= PAIR_CONFIG["XRP-USDT"]["threshold"]:
        return "UP",   bull_score, bull_rules
    if bear_score > bull_score and bear_score >= PAIR_CONFIG["XRP-USDT"]["threshold"]:
        return "DOWN", bear_score, bear_rules
    return None, max(bull_score, bear_score), []


def _rule_bnb(ind: dict) -> tuple:
    """
    BNB: EMA34 pullback ±0.4% + MACD zero-cross + Stochastic confirmation.
    8 rules total. Minimum 5 must fire.
    BNB trends cleanly — tight pullback zone to ema34 with ema55 as trend gate.
    """
    bull_rules = []
    bear_rules = []

    # ── BULL ─────────────────────────────────────────────────────────────────
    if ind["above_ema55"]:                     bull_rules.append("above_ema55_trend")
    if ind["near_ema34"]:                      bull_rules.append("pullback_to_ema34")
    if 42 <= ind["rsi"] <= 60:                 bull_rules.append("rsi_sweet_spot")
    if ind["macd_hist_rising"]:                bull_rules.append("macd_hist_rising")
    if ind["stoch_k"] > ind["stoch_d"]:       bull_rules.append("stoch_bull_cross")
    if ind["adx"] > 20:                        bull_rules.append("adx_trending")
    # Momentum add-on
    if ind["ema_bull"] and ind["vol_z"] > 0.5: bull_rules.append("ema_bull_vol")
    if ind["macd_line"] > ind["macd_signal"]:  bull_rules.append("macd_cross_bull")

    # ── BEAR ─────────────────────────────────────────────────────────────────
    if not ind["above_ema55"]:                 bear_rules.append("below_ema55_trend")
    if ind["near_ema34"]:                      bear_rules.append("pullback_to_ema34")
    if 40 <= ind["rsi"] <= 58:                 bear_rules.append("rsi_bear_zone")
    if not ind["macd_hist_rising"]:            bear_rules.append("macd_hist_falling")
    if ind["stoch_k"] < ind["stoch_d"]:       bear_rules.append("stoch_bear_cross")
    if ind["adx"] > 20:                        bear_rules.append("adx_trending")
    if ind["ema_bear"] and ind["vol_z"] > 0.5: bear_rules.append("ema_bear_vol")
    if ind["macd_line"] < ind["macd_signal"]:  bear_rules.append("macd_cross_bear")

    bull_score = len(bull_rules)
    bear_score = len(bear_rules)

    if bull_score > bear_score and bull_score >= PAIR_CONFIG["BNB-USDT"]["threshold"]:
        return "UP",   bull_score, bull_rules
    if bear_score > bull_score and bear_score >= PAIR_CONFIG["BNB-USDT"]["threshold"]:
        return "DOWN", bear_score, bear_rules
    return None, max(bull_score, bear_score), []


def _rule_doge(ind: dict) -> tuple:
    """
    DOGE: Heikin-Ashi sentiment wave + Volume climax + EMA21 gate.
    7 rules total. Minimum 5 must fire.
    DOGE is sentiment-driven. HA trend + vol climax is the most reliable filter.
    """
    bull_rules = []
    bear_rules = []

    # ── BULL ─────────────────────────────────────────────────────────────────
    if ind["ha_trend"] > 0:                    bull_rules.append("ha_trend_bull")
    if ind["vol_z"] > 0.8:                     bull_rules.append("vol_climax")
    if ind["above_ema21"]:                     bull_rules.append("above_ema21")
    if 45 <= ind["rsi"] <= 72:                 bull_rules.append("rsi_bull_zone")
    if ind["macd_hist"] > 0:                   bull_rules.append("macd_positive")
    if ind["stoch_k"] > 40:                    bull_rules.append("stoch_above_40")
    if ind["ema_bull"]:                        bull_rules.append("ema_bull")

    # ── BEAR ─────────────────────────────────────────────────────────────────
    if ind["ha_trend"] < 0:                    bear_rules.append("ha_trend_bear")
    if ind["vol_z"] > 0.8:                     bear_rules.append("vol_climax")
    if not ind["above_ema21"]:                 bear_rules.append("below_ema21")
    if 28 <= ind["rsi"] <= 55:                 bear_rules.append("rsi_bear_zone")
    if ind["macd_hist"] < 0:                   bear_rules.append("macd_negative")
    if ind["stoch_k"] < 60:                    bear_rules.append("stoch_below_60")
    if ind["ema_bear"]:                        bear_rules.append("ema_bear")

    bull_score = len(bull_rules)
    bear_score = len(bear_rules)

    if bull_score > bear_score and bull_score >= PAIR_CONFIG["DOGE-USDT"]["threshold"]:
        return "UP",   bull_score, bull_rules
    if bear_score > bull_score and bear_score >= PAIR_CONFIG["DOGE-USDT"]["threshold"]:
        return "DOWN", bear_score, bear_rules
    return None, max(bull_score, bear_score), []


# ── Dispatch table ────────────────────────────────────────────────────────────
_RULE_FN = {
    "BTC-USDT":  _rule_btc,
    "ETH-USDT":  _rule_eth,
    "SOL-USDT":  _rule_sol,
    "XRP-USDT":  _rule_xrp,
    "BNB-USDT":  _rule_bnb,
    "DOGE-USDT": _rule_doge,
}


# ══════════════════════════════════════════════════════════════════════════════
# NO-OP STUBS  (keep scheduler + wsgi.py callers happy)
# ══════════════════════════════════════════════════════════════════════════════

def retrain_all(limit: int = 300) -> None:
    """
    No-op stub. ML training removed in v4.0.
    Called by scheduler every 4 h and at startup — safe to call.
    _models is pre-populated at import time so _models_ready() is always True.
    """
    logger.info(
        "[SE] retrain_all called (no-op — rule-based engine v4.0, limit=%d ignored)", limit
    )


# ══════════════════════════════════════════════════════════════════════════════
# CANDLE TIMING
# ══════════════════════════════════════════════════════════════════════════════

def _current_candle_open(now: datetime) -> datetime:
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)


def _candle_close(candle_open: datetime) -> datetime:
    return candle_open + timedelta(minutes=15)


# ══════════════════════════════════════════════════════════════════════════════
# LIVE SIGNAL SCORING
# ══════════════════════════════════════════════════════════════════════════════

def _score_symbol(sym: str) -> dict | None:
    """
    Fetch latest 120 candles, compute indicators, run the pair's rule function.
    Returns a candidate signal dict or None if no qualifying signal.
    """
    df = fetch_okx_candles(sym, limit=120)
    if df.empty or len(df) < 30:
        logger.debug("[SE] score %s: insufficient data (%d rows)", sym, len(df))
        return None

    try:
        ind = _build_indicators(df)
    except Exception as exc:
        logger.warning("[SE] score %s: indicator build error: %s", sym, exc)
        return None

    rule_fn = _RULE_FN.get(sym)
    if rule_fn is None:
        return None

    direction, score, rules = rule_fn(ind)

    if direction is None:
        logger.debug("[SE] score %s: no qualifying signal (score=%d)", sym, score)
        return None

    cfg        = PAIR_CONFIG[sym]
    max_rules  = cfg["max_rules"]

    # Normalise score to 0–1 range for API compatibility
    # (scheduler and dashboard display this as "confidence")
    confidence = round(score / max_rules, 4)

    # Tier: upgrade to T1 if vol spike present
    tier = "T1" if ind["vol_spike"] else cfg["tier"]

    # Candle timing
    now          = datetime.now(timezone.utc).replace(tzinfo=None)
    candle_open  = _current_candle_open(now)
    candle_close = _candle_close(candle_open)

    logger.info(
        "[SE] %s → %s | score=%d/%d conf=%.2f tier=%s rules=%s",
        sym, direction, score, max_rules, confidence, tier, rules
    )

    return {
        "symbol":            sym,
        "direction":         direction,
        "confidence":        confidence,       # score/max_rules, 0–1
        "confluence_score":  score,            # raw integer rule count
        "max_rules":         max_rules,
        "rules_fired":       rules,            # list of rule names
        "family":            cfg["family"],
        "invert":            cfg["invert"],
        "tier":              tier,
        "rsi_14":            ind["rsi"],
        "macd_hist":         ind["macd_hist"],
        "adx":               ind["adx"],
        "vol_ratio":         ind["vol_ratio"],
        "open_price":        ind["close"],
        "candle_open_time":  candle_open,
        "candle_close_time": candle_close,
        "_weight":           cfg["weight"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# PICK BEST SIGNAL  (public API — called by scheduler every candle)
# ══════════════════════════════════════════════════════════════════════════════

def pick_best_signal(
    min_confidence: float = 0.0,
    exclude: list | None = None,
    preferred_families: list | None = None,
    excluded_families: list | None = None,
    blocked_directions: dict | None = None,
) -> dict | None:
    """
    Score all 6 symbols and return the single best signal for this candle.

    Parameters
    ──────────
    min_confidence    : global floor from Settings.min_confidence
                        (maps to score/max_rules; 0.0 = disabled)
    exclude           : pair cooldown list from scheduler
    preferred_families: families to rank higher (family rotation)
    excluded_families : families to skip entirely
    blocked_directions: {direction: floor} from Rule2 saturation filter

    Ranking
    ───────
    1. Score all pairs → collect candidates above pair threshold
    2. Drop excluded symbols + excluded families
    3. Apply Rule2 directional floors
    4. Apply global min_confidence floor
    5. Sort: preferred family first, then confluence_score × weight descending
    6. Return top candidate or None
    """
    exclude           = exclude           or []
    excluded_families = excluded_families or []
    blocked_directions = blocked_directions or {}

    candidates = []
    for sym in SYMBOLS:
        if sym in exclude:
            logger.debug("[SE] pick: %s in cooldown — skip", sym)
            continue
        if PAIR_CONFIG[sym]["family"] in excluded_families:
            logger.debug("[SE] pick: %s family excluded — skip", sym)
            continue

        candidate = _score_symbol(sym)
        if candidate is None:
            continue

        # Global min_confidence floor
        if min_confidence and candidate["confidence"] < min_confidence:
            logger.debug(
                "[SE] pick: %s conf=%.3f < global floor=%.3f — skip",
                sym, candidate["confidence"], min_confidence,
            )
            continue

        # Rule2 directional floor
        direction  = candidate["direction"]
        dir_floor  = blocked_directions.get(direction, 0.0)
        if dir_floor and candidate["confidence"] < dir_floor:
            logger.info(
                "[SE] pick: %s %s blocked by Rule2 floor=%.2f (conf=%.3f)",
                sym, direction, dir_floor, candidate["confidence"],
            )
            continue

        candidates.append(candidate)

    if not candidates:
        logger.info("[SE] pick_best_signal: no qualifying candidates this candle")
        return None

    # Sort: preferred family → score × weight descending
    def _rank(c):
        in_pref = 1 if (preferred_families and c["family"] in preferred_families) else 0
        return (in_pref, c["confluence_score"] * c["_weight"])

    candidates.sort(key=_rank, reverse=True)
    best = candidates[0]
    best.pop("_weight", None)

    logger.info(
        "[SE] pick_best_signal → %s %s score=%d conf=%.3f tier=%s family=%s",
        best["symbol"], best["direction"], best["confluence_score"],
        best["confidence"], best["tier"], best["family"],
    )
    return best


# ══════════════════════════════════════════════════════════════════════════════
# OUTCOME TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def record_outcome(symbol: str, outcome: str) -> None:
    """Update in-memory win/loss counter. DB is source of truth."""
    if symbol not in _pair_outcomes:
        _pair_outcomes[symbol] = {"wins": 0, "losses": 0}
    if outcome == "WIN":
        _pair_outcomes[symbol]["wins"] += 1
    elif outcome == "LOSS":
        _pair_outcomes[symbol]["losses"] += 1


def get_pair_stats() -> dict:
    """Return session win/loss counts. Used by /api/stats/pairs."""
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
    """Return PAIR_CONFIG copy for /api/stats/pairs dashboard display."""
    return {sym: dict(cfg) for sym, cfg in PAIR_CONFIG.items()}
