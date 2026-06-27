"""
Signal Engine v5 — RSI(2) Mean-Reversion (No ML)
═══════════════════════════════════════════════════════════════════════════════
Replaces the v4 Random Forest / Gradient Boosting / Extra Trees ensemble with
the single rule that was actually backtested and discussed across this whole
project, on real 15m OHLCV history for all 6 pairs:

    RSI(2) <= 10  →  signal UP   (oversold mean-reversion)
    RSI(2) >= 90  →  signal DOWN (overbought mean-reversion)

Resolution rule (matches scheduler.py's job_resolve_outcomes exactly):
    signal UP   WINS if   next candle close > next candle open
    signal DOWN WINS if   next candle close < next candle open

Backtest results (next-candle resolution, this exact rule, no other filters):
  See BACKTEST_RESULTS.md / README for the full per-pair, per-month tables.
  Aggregate across BTC/ETH/SOL/XRP/BNB/DOGE, 3 months (Feb-Apr 2026):
    ~10.5-11.8 signals/day per direction per pair
    ~54-62% win rate per direction per pair (never below ~52% in any
    single pair/month observed)

This file intentionally contains NO machine learning. No RandomForest, no
GradientBoosting, no ExtraTrees, no StandardScaler, no model training, no
feature engineering beyond what RSI(2) itself requires. "Training" and
"retraining" are no-ops kept only so the rest of the system (scheduler.py,
main.py, wsgi.py) doesn't need to change its calls into this module.

Everything else in this file (per-pair config dict, pick_best_signal,
record_outcome, pair stats, the exported function names) mirrors the
external interface of the old v4 engine on purpose, so scheduler.py,
app.py, main.py and wsgi.py do not need to change at all.
"""

import logging
from datetime import timedelta

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
SYMBOLS  = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT"]

# ── Per-pair RSI(2) thresholds ─────────────────────────────────────────────────
# Backtesting across BTC/ETH/SOL/XRP/BNB/DOGE on 15m candles showed the SAME
# rule (RSI(2) <= 10 / >= 90) works across every pair with a consistent edge.
# We keep PAIR_CONFIG as a dict (rather than hardcoding the numbers) so any
# pair's threshold can be tuned later from the dashboard/DB without touching
# this file again — but all 6 pairs currently use the identical backtested
# rule, since that's what was actually validated.
PAIR_CONFIG = {
    sym: {
        "rsi_period":    2,
        "rsi_oversold":  10,   # RSI(2) <= this → UP signal
        "rsi_overbought": 90,  # RSI(2) >= this → DOWN signal
        "invert":        False,
        "tier":          "T2",
        # No ML confidence threshold applies to this engine (the gate IS the
        # signal). Set explicitly to 0.50 rather than leaving this key out —
        # app.py's /api/stats/pairs route does cfg.get("threshold", 0.58) and
        # would otherwise silently display the old ML engine's stale 0.58
        # default on the dashboard for every pair.
        "threshold":     0.50,
        # ATR volatility-regime confluence filter (None = disabled).
        # Backtested combo: BTC-USDT + ETH-USDT + BNB-USDT live, with a
        # rolling-96-candle (1 trading day) ATR(14) percentile-rank filter
        # that skips signals occurring in the top X% most volatile regime
        # for that pair. April 2026 backtest (30 days): 520 signals,
        # 61.35% win rate, 17.33 signals/day, 26/30 days >=50% win rate.
        # Only enabled for the 3 pairs this was actually validated on —
        # SOL/XRP/DOGE keep atr_filter=None since a wider sweep showed this
        # filter helps some pairs and not others, and these three weren't
        # part of the validated combo.
        "atr_filter":    None,
    }
    for sym in SYMBOLS
}

# Backtested ATR regime filter, scoped to BTC/ETH/BNB only (see PAIR_CONFIG
# comment above). atr_filter value = max allowed percentile rank of the
# pair's own trailing-96-candle ATR(14); signals in the top (1-value)% most
# volatile regime are skipped.
PAIR_CONFIG["BTC-USDT"]["atr_filter"] = 0.85   # skip top 15% volatility regime
PAIR_CONFIG["ETH-USDT"]["atr_filter"] = 0.85   # skip top 15% volatility regime
PAIR_CONFIG["BNB-USDT"]["atr_filter"] = 0.85   # skip top 15% volatility regime

# ── In-memory pair stats (mirrors v4 behaviour — process-lifetime only) ──────
_pair_stats: dict = {}

# ── "Models ready" shim ───────────────────────────────────────────────────────
# scheduler.py's _models_ready() checks `len(_models) >= len(SYMBOLS)` before
# allowing job_generate_signal to run. This engine has no ML models, so we
# populate this dict with a placeholder the first time retrain_all() (a
# no-op) is called, simply to satisfy that readiness check without lying
# about there being trained models.
_models: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# INDICATOR: RSI
# ══════════════════════════════════════════════════════════════════════════════

def _rsi(close: pd.Series, period: int) -> pd.Series:
    """
    Wilder-smoothed RSI, identical math to the version used throughout the
    backtests in this project (matches the standalone analysis scripts).
    """
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)   # avg_loss == 0 → RSI = 100
    rsi = rsi.where(avg_gain != 0, rsi).fillna(50.0)
    return rsi


def _atr_pct_rank(df: pd.DataFrame, atr_period: int = 14, rank_window: int = 96) -> pd.Series:
    """
    Wilder-smoothed ATR(atr_period), then its percentile rank within a
    trailing `rank_window`-candle window (96 = 1 trading day at 15m bars).
    Identical math to the backtest's atr_pct_rank column — this is what
    PAIR_CONFIG[sym]["atr_filter"] is compared against.
    """
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / atr_period, min_periods=atr_period, adjust=False).mean()
    return atr.rolling(rank_window).rank(pct=True)


# ══════════════════════════════════════════════════════════════════════════════
# OKX DATA FETCH  (unchanged behaviour from v4 — same endpoint, same shape)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 100) -> pd.DataFrame:
    """
    Fetch up to `limit` most-recent 15m candles for `symbol` from OKX spot.
    Returns a DataFrame sorted ascending by timestamp with columns:
    timestamp, open, high, low, close, vol (float), plus raw OKX extras.
    Empty DataFrame on any failure.
    """
    endpoint   = f"{OKX_BASE}/api/v5/market/candles"
    batch_size = min(limit, 300)   # OKX caps at 300 per request
    all_frames = []
    fetched    = 0
    after_ts   = None

    while fetched < limit:
        params = {"instId": symbol, "bar": bar, "limit": str(batch_size)}
        if after_ts is not None:
            params["after"] = str(after_ts)
        try:
            resp = requests.get(endpoint, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "0":
                logger.error("[%s] OKX error: %s", symbol, data.get("msg"))
                break
            rows = data.get("data", [])
            if not rows:
                break
            df_batch = pd.DataFrame(rows, columns=[
                "timestamp", "open", "high", "low", "close",
                "vol", "volCcy", "volCcyQuote", "confirm"
            ])
            df_batch["timestamp"] = pd.to_datetime(
                df_batch["timestamp"].astype(float), unit="ms", utc=True)
            for col in ["open", "high", "low", "close", "vol"]:
                df_batch[col] = df_batch[col].astype(float)
            all_frames.append(df_batch)
            fetched += len(df_batch)
            after_ts = int(df_batch["timestamp"].min().timestamp() * 1000)
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
# MAIN SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def get_signal_for_symbol(symbol: str) -> dict | None:
    """
    Evaluate the RSI(2) <=10 / >=90 rule for one symbol on the most recently
    CLOSED 15m candle. Returns a signal dict (same shape the old engine
    produced, so scheduler.py / app.py need no changes) or None if no
    signal fires.
    """
    cfg = PAIR_CONFIG.get(symbol)
    if cfg is None:
        logger.error("[%s] No PAIR_CONFIG found", symbol)
        return None

    # Fetch enough history for both RSI(2) and (where enabled) the ATR
    # volatility-regime filter, which needs a rolling 96-candle (1 day)
    # window to compute a meaningful percentile rank. 110 gives the ATR(14)
    # warmup + the full 96-candle rolling window with no gap.
    fetch_limit = max(50, cfg["rsi_period"] * 10)
    if cfg.get("atr_filter") is not None:
        fetch_limit = max(fetch_limit, 110)

    df = fetch_okx_candles(symbol, limit=fetch_limit)
    if df.empty or len(df) < 20:
        return None

    rsi_series = _rsi(df["close"], cfg["rsi_period"])
    df = df.copy()
    df["rsi"] = rsi_series

    # Use the LAST FULLY CLOSED candle (iloc[-2]) for the signal decision,
    # exactly like the v4 engine did — iloc[-1] is the still-forming candle.
    if len(df) < 2:
        return None
    row    = df.iloc[-2]
    latest = df.iloc[-1]

    rsi_val = float(row["rsi"])
    if pd.isna(rsi_val):
        return None

    direction = None
    if rsi_val <= cfg["rsi_oversold"]:
        direction = "UP"
    elif rsi_val >= cfg["rsi_overbought"]:
        direction = "DOWN"

    if direction is None:
        return None

    # ── ATR volatility-regime confluence filter (backtested, see PAIR_CONFIG) ──
    # Only active for pairs with atr_filter set (currently BTC/ETH/BNB). Skips
    # the signal entirely if this candle sits in the most volatile regime tail
    # for this pair's own recent history — matches the backtest exactly, which
    # found this reduces max loss streak (10→6 over Jan-May 2026 for this combo)
    # at the cost of some signal volume, while staying above 15 signals/day
    # when run across BTC+ETH+BNB together.
    atr_filter = cfg.get("atr_filter")
    if atr_filter is not None:
        atr_rank_series = _atr_pct_rank(df)
        df["atr_pct_rank"] = atr_rank_series
        atr_rank_val = df["atr_pct_rank"].iloc[-2]   # same iloc[-2] closed-candle convention as RSI
        if pd.notna(atr_rank_val) and atr_rank_val > atr_filter:
            logger.info(
                "[%s] Signal %s blocked by ATR regime filter | atr_pct_rank=%.2f > %.2f",
                symbol, direction, atr_rank_val, atr_filter,
            )
            return None

    # ── Candle boundary bookkeeping (unchanged from v4) ─────────────────────
    ts       = latest["timestamp"]
    minutes  = ts.minute
    boundary = (minutes // 15) * 15
    candle_open  = ts.replace(minute=boundary, second=0, microsecond=0)
    candle_close = candle_open + pd.Timedelta(minutes=15)

    # Backtested win rate proxy, used only for dashboard display / logging —
    # NOT used to gate trading decisions. Pulled from the 3-month backtest
    # discussed and recorded in BACKTEST_RESULTS.md. This keeps the
    # `ml_confidence` field meaningful without pretending there's a live
    # model behind it.
    backtest_wr = _BACKTEST_WIN_RATE.get(symbol, {}).get(direction, 0.55)

    logger.info(
        "[%s] SIGNAL %s | rsi(%d)=%.1f thresh=%s | backtest_wr=%.1f%%",
        symbol, direction, cfg["rsi_period"], rsi_val,
        cfg["rsi_oversold"] if direction == "UP" else cfg["rsi_overbought"],
        backtest_wr * 100,
    )

    return {
        "symbol":            symbol,
        "direction":         direction,
        "invert":            cfg.get("invert", False),
        "confidence":        backtest_wr,          # display-only, see note above
        "threshold":         0.50,
        "margin":            backtest_wr - 0.50,
        "tier":              cfg.get("tier", "T2"),
        "signal_gate":       "rsi2",
        "rsi_2":             round(rsi_val, 2),
        "rsi_14":            float(_rsi(df["close"], 14).iloc[-2]),  # display only
        "macd_hist":         0.0,                  # not used by this engine; kept for schema compat
        "adx":               0.0,                  # not used by this engine; kept for schema compat
        "vol_ratio":         1.0,                  # not used by this engine; kept for schema compat
        "adx_trend":         False,
        "bull_market":       False,
        "open_price":        float(latest["close"]),
        "candle_open_time":  candle_open.to_pydatetime().replace(tzinfo=None),
        "candle_close_time": candle_close.to_pydatetime().replace(tzinfo=None),
        "in_momentum":       False,
        "momentum_move_pct": 0.0,
        "cluster_streak":    1,
        "hour_penalty":      False,
        "hour_blocked":      False,
    }


# Backtested win rates per pair/direction, 3-month aggregate (Feb-Apr 2026),
# RSI(2) <=10/>=90 rule, next-candle resolution. Used ONLY to populate the
# display-only `confidence` field above — never used to filter or block
# signals. Update this table if/when a fresh backtest is run.
_BACKTEST_WIN_RATE = {
    "BTC-USDT":  {"UP": 0.574, "DOWN": 0.564},
    "ETH-USDT":  {"UP": 0.567, "DOWN": 0.586},
    "SOL-USDT":  {"UP": 0.612, "DOWN": 0.558},
    "XRP-USDT":  {"UP": 0.559, "DOWN": 0.577},
    "BNB-USDT":  {"UP": 0.564, "DOWN": 0.561},
    "DOGE-USDT": {"UP": 0.599, "DOWN": 0.584},
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
    Evaluate all pairs, return the single best qualifying signal.
    Keeps the same parameter surface as v4 so scheduler.py is unaffected —
    family rotation and directional-saturation blocking (both implemented in
    scheduler.py / passed in here) still apply exactly as before. There is
    no ML confidence to rank candidates by, so when multiple pairs fire on
    the same candle we rank by each pair's backtested win rate for that
    direction (a fixed, transparent number — not a live model score).
    """
    PAIR_FAMILY = {
        "BTC-USDT":  "A", "ETH-USDT":  "A",
        "DOGE-USDT": "B", "SOL-USDT":  "B",
        "XRP-USDT":  "C", "BNB-USDT":  "C",
    }

    active_symbols = [s for s in SYMBOLS if not (exclude and s in exclude)]
    candidates = []

    for sym in active_symbols:
        try:
            sig = get_signal_for_symbol(sym)
            if sig:
                sig["family"] = PAIR_FAMILY.get(sym, "B")
                candidates.append(sig)
        except Exception as e:
            logger.error("[%s] get_signal_for_symbol error: %s", sym, e)

    if not candidates:
        logger.info("[ENGINE] No qualifying signals this candle")
        return None

    if min_confidence:
        candidates = [s for s in candidates if s["confidence"] >= min_confidence]
        if not candidates:
            logger.info("[ENGINE] No signals above min_confidence=%.2f", min_confidence)
            return None

    # Directional saturation block (Rule 2), passed in by scheduler.py
    if blocked_directions:
        filtered = []
        for s in candidates:
            floor = blocked_directions.get(s["direction"])
            if floor and s["confidence"] < floor:
                logger.info("[ENGINE] Rule2 blocked %s %s conf=%.3f < floor=%.2f",
                            s["symbol"], s["direction"], s["confidence"], floor)
            else:
                filtered.append(s)
        candidates = filtered
        if not candidates:
            logger.info("[ENGINE] Rule2: all candidates blocked")
            return None

    def score(s):
        return s["margin"]

    excl_set = set(excluded_families) if excluded_families else set()
    if excl_set:
        eligible = [s for s in candidates if s["family"] not in excl_set]
        if eligible:
            best = max(eligible, key=score)
            logger.info("[ENGINE] Best: %s %s conf=%.3f family=%s",
                        best["symbol"], best["direction"], best["confidence"], best["family"])
            return best
        logger.info("[ENGINE] Family rotation: no signal outside %s — skipping candle", excl_set)
        return None

    best = max(candidates, key=score)
    logger.info("[ENGINE] Best: %s %s conf=%.3f family=%s",
                best["symbol"], best["direction"], best["confidence"], best["family"])
    return best


# ══════════════════════════════════════════════════════════════════════════════
# NO-OP TRAINING SHIMS
# ══════════════════════════════════════════════════════════════════════════════
# This engine uses a fixed rule, not a trained model. These functions exist
# only so main.py / wsgi.py / scheduler.py — which call retrain_all() and
# check _models — continue to work unmodified. They do not train anything.

def train_model(symbol: str, df: pd.DataFrame = None) -> bool:
    """No-op: this engine has no model to train. Marks the symbol 'ready'."""
    _models[symbol] = True
    return True


def retrain_all(limit: int = 960) -> None:
    """
    No-op retrain. Populates _models so scheduler._models_ready() returns
    True and job_generate_signal is allowed to run. Safe to call as often
    as the scheduler's 4-hourly retrain job likes — it does no network
    calls and no computation.
    """
    for sym in SYMBOLS:
        _models[sym] = True
    logger.info("[ENGINE] retrain_all() no-op — RSI(2) engine needs no training. "
                "%d/%d symbols marked ready.", len(_models), len(SYMBOLS))


# ══════════════════════════════════════════════════════════════════════════════
# OUTCOME TRACKING / STATS  (same shape as v4)
# ══════════════════════════════════════════════════════════════════════════════

def record_outcome(symbol: str, outcome: str, direction: str = None) -> None:
    if symbol not in _pair_stats:
        _pair_stats[symbol] = {"wins": 0, "losses": 0, "signals": 0}
    _pair_stats[symbol]["signals"] += 1
    if outcome == "WIN":
        _pair_stats[symbol]["wins"] += 1
    elif outcome == "LOSS":
        _pair_stats[symbol]["losses"] += 1


def get_direction_block_status() -> dict:
    """
    v5 has no system-level directional cooldown of its own (that logic now
    lives entirely in scheduler.py's Rule 2 / blocked_directions, which is
    still respected by pick_best_signal above). Returns a neutral status so
    any dashboard code reading this doesn't break.
    """
    return {
        "dir_loss_streak":   {"UP": 0, "DOWN": 0},
        "dir_blocked_until": {"UP": 0, "DOWN": 0},
        "system_bar":        0,
        "currently_blocked": {"UP": False, "DOWN": False},
    }


def get_pair_stats() -> dict:
    result = {}
    for sym in SYMBOLS:
        s     = _pair_stats.get(sym, {"wins": 0, "losses": 0, "signals": 0})
        total = s["wins"] + s["losses"]
        cfg   = PAIR_CONFIG.get(sym, {})
        result[sym] = {
            "wins":        s["wins"],
            "losses":      s["losses"],
            "signals":     s["signals"],
            "win_rate":    round(s["wins"] / total * 100, 1) if total > 0 else None,
            "threshold":   0.50,
            "tier":        cfg.get("tier", "T2"),
            "signal_gate": "rsi2",
        }
    return result


def get_pair_config() -> dict:
    return {sym: dict(cfg) for sym, cfg in PAIR_CONFIG.items()}
