"""
Signal Engine — Deterministic V2 (parallel, dual-timeframe)
─────────────────────────────────────────────────────────────────────────────
Replaces the ML ensemble entirely. No model, no training, no retraining, no
feature engineering, no 1H trend filter, no quality-vote filter. Everything
below was chosen because it was the specific thing that survived walk-forward
backtesting (tuned on Jan-Apr 2026 data, validated blind on May-Jun 2026) —
nothing here is a guess.

METHOD (Method 2 — peek, don't forecast):
  Decide using the first few minutes of REALIZED price action inside the
  candle that just opened, resolve against that SAME candle's own open/close.
  This is not a forecast of the future; it's partial-information inference
  on a path that's already partly walked — the sign of an early move is
  structurally informative about the final candle sign even under a pure
  random walk, and empirically the effect is much stronger than that,
  because real moves tend to continue (momentum) rather than mean-revert.

  - 5-minute candles:  peek = first 1-minute bar  (decide ~1 min in)
  - 15-minute candles: peek = first 3-minute bar  (decide ~3 min in)

QUALIFICATION:
  A candidate fires if |early move| >= a per-pair, per-timeframe magnitude
  threshold. That's it. Two other filters were extensively tested and
  DELIBERATELY DROPPED because they added no measurable win-rate value while
  cutting real volume:
    - multi-pair agreement (how many of the 6 pairs agree on direction)
    - early-candle volume vs. its trailing rolling median
  Magnitude alone was carrying all the signal.

PARALLEL, NOT SINGLE-PICK:
  Every pair that qualifies fires independently, every tick. This is NOT a
  "pick the single best signal system-wide" design — multiple pairs (and
  both timeframes) can hold open positions at the same time. Each
  (symbol, timeframe) stream is gated by its OWN breaker/cooldown, tracked
  per venue in PairLadder (see models.py) — there is deliberately no shared
  global streak counter, because a shared stream's realistic worst-case loss
  streak was measured at 14 (interleaving 12 independent streams), vs. ~3-4
  for each stream kept independent.

BREAKER (per symbol + timeframe + venue, state lives in PairLadder):
  After 3 consecutive REAL losses (a win breaks the counter, a cooldown trip
  does not reset it), pause new signals for that stream until:
    1. cooldown_until has passed (COOLDOWN_BARS native bars of that
       timeframe — 8 bars = 40 min for 5m, 2h for 15m), AND
    2. the next candidate's magnitude clears base_threshold * REARM_MULT
       (a stricter re-entry bar, using magnitude — the one feature that's
       actually predictive — rather than agreement, which isn't).
  This does not mathematically guarantee a hard cap of 3 in live trading
  (nothing can, without seeing the future) — in the Jan-Jun 2026 backtest,
  including the out-of-sample half, the worst realized streak on any single
  stream was 4, occurring rarely.

Backtested performance (Jan-Jun 2026, walk-forward validated):
  5-min combined (6 pairs):  ~30 signals/day, ~87-88% win rate
  15-min combined (6 pairs): ~47 signals/day, ~83-85% win rate
"""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"

SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT"]
TIMEFRAMES = ["5m", "15m"]

# Which native OKX bar size to read for the "peek" of each candle timeframe,
# and how many minutes that peek covers. 1m divides evenly into 5m; 3m
# divides evenly into 15m — so the peek bar's own open_time IS the parent
# candle's open_time, no separate boundary math needed.
PEEK_BAR       = {"5m": "1m", "15m": "3m"}
PEEK_MINUTES   = {"5m": 1,    "15m": 3}
CANDLE_MINUTES = {"5m": 5,    "15m": 15}

# Breaker tuning — identical shape for both timeframes, only the base
# magnitude thresholds differ (tuned independently per timeframe).
REARM_MULT    = 1.5   # resuming candidate must clear base_threshold * this
COOLDOWN_BARS = 8      # in units of the STREAM's OWN timeframe (native bars)

# Per-(timeframe, symbol) magnitude threshold — the ONLY qualification rule.
# Frozen from walk-forward tuning on Jan-Apr 2026, validated blind on May-Jun.
MAG_THRESHOLD = {
    "5m": {
        "BTC-USDT":  0.0018,
        "ETH-USDT":  0.0025,
        "BNB-USDT":  0.0025,
        "XRP-USDT":  0.0030,
        "DOGE-USDT": 0.0025,
        "SOL-USDT":  0.0025,
    },
    "15m": {
        "BTC-USDT":  0.0025,
        "ETH-USDT":  0.0022,
        "BNB-USDT":  0.0018,
        "XRP-USDT":  0.0022,
        "DOGE-USDT": 0.0022,
        "SOL-USDT":  0.0045,
    },
}

_pair_stats: dict = {}   # (symbol, timeframe) -> {"wins": int, "losses": int}


def cooldown_seconds(timeframe: str) -> int:
    """Cooldown duration in seconds for a stream's own timeframe."""
    return COOLDOWN_BARS * CANDLE_MINUTES[timeframe] * 60


def rearm_threshold(symbol: str, timeframe: str) -> float:
    return MAG_THRESHOLD[timeframe][symbol] * REARM_MULT


# ── OKX data fetch ────────────────────────────────────────────────────────────
# Unchanged from the original engine — OKX's REST shape, pagination, and
# instId format didn't change; only what we ask it for did.

def fetch_okx_candles(symbol: str, bar: str = "15m", limit: int = 10) -> pd.DataFrame:
    """
    Fetch up to `limit` candles from OKX for `symbol` at the given bar size.
    Small limit is enough here — the deterministic engine only ever needs
    the most recent one or two confirmed peek-bars, never a lookback window.
    """
    OKX_PAGE_MAX = 300
    all_frames = []
    fetched = 0
    after_ts = None

    while fetched < limit:
        batch_size = min(OKX_PAGE_MAX, limit - fetched)
        if after_ts is None:
            endpoint = f"{OKX_BASE}/api/v5/market/candles"
            params = {"instId": symbol, "bar": bar, "limit": str(batch_size)}
        else:
            endpoint = f"{OKX_BASE}/api/v5/market/history-candles"
            params = {"instId": symbol, "bar": bar, "limit": str(batch_size), "after": str(after_ts)}
        try:
            resp = requests.get(endpoint, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "0":
                logger.error("[%s] OKX error: %s (bar=%s)", symbol, data.get("msg"), bar)
                break
            rows = data.get("data", [])
            if not rows:
                break
            df_batch = pd.DataFrame(rows, columns=[
                "timestamp", "open", "high", "low", "close",
                "vol", "volCcy", "volCcyQuote", "confirm"
            ])
            df_batch["timestamp"] = pd.to_datetime(df_batch["timestamp"].astype(float), unit="ms", utc=True)
            for col in ["open", "high", "low", "close", "vol"]:
                df_batch[col] = df_batch[col].astype(float)
            all_frames.append(df_batch)
            fetched += len(df_batch)
            after_ts = int(df_batch["timestamp"].min().timestamp() * 1000)
            if len(df_batch) < batch_size:
                break
        except Exception as e:
            logger.error("[%s] OKX fetch error (bar=%s): %s", symbol, bar, e)
            break

    if not all_frames:
        return pd.DataFrame()
    df = pd.concat(all_frames, ignore_index=True)
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    return df


# ── Core signal logic ────────────────────────────────────────────────────────

def fetch_okx_candles_for_resolution(symbol: str, bar: str = "15m", limit: int = 10) -> pd.DataFrame:
    """
    OKX fetch used for the resolution fallback path (deciding WIN/LOSS when
    Limitless/Polymarket's own — Chainlink-backed — resolution isn't
    available in time, and always for BNB/DOGE which aren't on the
    Chainlink feed captured by chainlink_feed.py at all).

    Uses the USDT-quoted instrument directly — the same reliable feed
    signal generation already reads, available on every OKX account
    regardless of region. This is deliberately the FALLBACK tier only:
    Limitless/Polymarket's own native resolution (checked first, in
    job_resolve_outcomes) is what actually determines WIN/LOSS in the
    normal case, since that's the real Chainlink-backed settlement the
    platforms pay out on — this OKX path only matters when that isn't
    available in time.
    """
    return fetch_okx_candles(symbol, bar=bar, limit=limit)


def get_signal_for_symbol_tf(symbol: str, timeframe: str) -> dict | None:
    """
    Read the peek bar for `symbol` at `timeframe` and return a candidate dict
    if it qualifies (|early move| >= this pair+timeframe's magnitude
    threshold), else None.

    Does NOT check cooldown/breaker state — that lives in PairLadder and is
    checked by the scheduler per venue, since the same magnitude candidate
    might be tradeable on one venue and still cooling down on another.
    """
    bar = PEEK_BAR[timeframe]
    df = fetch_okx_candles(symbol, bar=bar, limit=3)
    if df.empty:
        return None

    # Only confirmed (closed) bars — the peek bar must have fully closed.
    confirmed = df[df["confirm"] == "1"] if "confirm" in df.columns else df
    if confirmed.empty:
        return None
    peek = confirmed.iloc[-1]

    # The peek bar's own open_time must align to this timeframe's candle
    # boundary — since PEEK_BAR divides evenly into the parent timeframe,
    # the peek bar that starts exactly on a valid boundary IS bar #1 of a
    # fresh parent candle.
    candle_minutes = CANDLE_MINUTES[timeframe]
    open_time = peek["timestamp"].to_pydatetime().replace(tzinfo=timezone.utc)
    if open_time.minute % candle_minutes != 0:
        return None  # this peek bar isn't the first sub-bar of a new candle

    candle_open = float(peek["open"])
    peek_close  = float(peek["close"])
    if candle_open <= 0:
        return None

    ret_early = (peek_close - candle_open) / candle_open
    if ret_early == 0:
        return None  # flat — no direction to trade

    direction = "UP" if ret_early > 0 else "DOWN"
    magnitude = abs(ret_early)
    threshold = MAG_THRESHOLD[timeframe][symbol]
    if magnitude < threshold:
        return None

    candle_close_time = open_time + timedelta(minutes=candle_minutes)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "magnitude": magnitude,
        "confidence": magnitude,   # alias — existing Telegram/logging code reads 'confidence';
                                    # it's the same deterministic strength value, not an ML probability
        "threshold": threshold,
        "rearm_threshold": threshold * REARM_MULT,
        "margin": magnitude - threshold,
        "open_price": candle_open,
        "candle_open_time": open_time.replace(tzinfo=None),
        "candle_close_time": candle_close_time.replace(tzinfo=None),
    }


def get_all_candidates(timeframe: str, exclude: set | None = None) -> list[dict]:
    """
    Return every qualifying candidate across all 6 pairs for this timeframe.
    This is the PARALLEL entry point — unlike the old pick_best_signal, it
    does not choose just one; the scheduler places an order for every
    candidate that also clears its own PairLadder gating per venue.

    `exclude` — optional set of symbols to skip outright (e.g. symbols
    currently in cooldown on EVERY enabled venue, so there's no point even
    generating the candidate).
    """
    exclude = exclude or set()
    out = []
    for symbol in SYMBOLS:
        if symbol in exclude:
            continue
        try:
            sig = get_signal_for_symbol_tf(symbol, timeframe)
        except Exception as e:
            logger.error("[%s/%s] signal generation error: %s", symbol, timeframe, e)
            continue
        if sig is not None:
            out.append(sig)
    return out


# ── Stats / dashboard compatibility ──────────────────────────────────────────

def get_pair_config() -> dict:
    """Dashboard-facing view of current thresholds, keyed 'SYMBOL:timeframe'."""
    out = {}
    for tf in TIMEFRAMES:
        for symbol in SYMBOLS:
            out[f"{symbol}:{tf}"] = {
                "threshold": MAG_THRESHOLD[tf][symbol],
                "rearm_threshold": MAG_THRESHOLD[tf][symbol] * REARM_MULT,
                "timeframe": tf,
            }
    return out


def record_outcome(symbol: str, timeframe: str, outcome: str) -> None:
    """Track running win/loss counts per (symbol, timeframe) for the dashboard."""
    key = (symbol, timeframe)
    stats = _pair_stats.setdefault(key, {"wins": 0, "losses": 0})
    if outcome == "WIN":
        stats["wins"] += 1
    elif outcome == "LOSS":
        stats["losses"] += 1


def get_pair_stats() -> dict:
    out = {}
    for (symbol, tf), stats in _pair_stats.items():
        total = stats["wins"] + stats["losses"]
        wr = (stats["wins"] / total * 100) if total else None
        out[f"{symbol}:{tf}"] = {**stats, "total": total, "win_rate": wr}
    return out
