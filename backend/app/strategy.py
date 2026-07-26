from collections import deque
from dataclasses import dataclass
from typing import Optional
import statistics


@dataclass
class Tick:
    epoch: int
    price: float


@dataclass
class SignalDecision:
    direction: str
    reason: str
    # Rolling tick volatility (stdev of the last vol_window tick-to-tick
    # price changes). As of 2026-07-26 this does NOT size the barrier --
    # engine.py uses a literal fixed barrier (config.py's
    # barrier_fixed_offset) instead. Kept for two reasons: (1) a flat
    # (exactly zero) reading most likely means a frozen/stale price feed
    # rather than genuine market inactivity, worth skipping regardless of
    # how the barrier is sized -- see the vol<=0 check in evaluate() below;
    # (2) informational context on the dashboard for how the fixed barrier
    # compares to actual current market movement.
    vol: float
    # EMA(fast)/EMA(slow) separation, in basis points of the current price.
    # Purely informational (dashboard/API display) -- unlike the old candle
    # strategy's confluence score, it is not compared against a threshold;
    # a signal fires whenever the two EMAs are on different sides, however
    # close.
    score: int = 0


class TickEMAStrategy:
    """
    EMA(10)/EMA(50) tick-crossover strategy, trading a 10-tick-duration
    Higher/Lower contract with a literal fixed barrier (config.py's
    barrier_fixed_offset, 0.25 by default) -- NOT scaled by volatility.

    KNOWN ECONOMICS, STATED HONESTLY: at this barrier size and duration,
    backtesting against ~199 days of R_25 tick data found a win rate of
    roughly 32-34% (best found across ~95 filter combinations, including
    conditioning on only the most extreme volatility spikes) -- see
    tick_backtest_addendum.md and the README's changelog for the full
    session. Against a payout of $2.60 back on a $1 stake, breakeven needs
    ~38.46%; this backtest sits below that. This configuration is shipped
    at explicit user request, to redeploy and observe real results
    directly, not because backtesting supports it as profitable.

    (An earlier same-day revision instead scaled the barrier to 0.25x the
    rolling 20-tick volatility, which backtested at ~46.9%/~44.9%
    overall/min-daily win rate -- a materially different, much smaller
    barrier in absolute terms. See README for why this changed back to a
    fixed value.)

    Operates directly on the raw tick stream rather than completed candles.
    Every incoming tick is fed to `push_tick()`; `evaluate()` reads off the
    current EMA/volatility state whenever it's called. The engine only
    calls `evaluate()` at decision points -- every `trade_duration_ticks`
    ticks (see engine.py) -- so trades are non-overlapping, matching
    exactly how the backtests above were run. Evaluating on overlapping
    windows was never simulated or validated.
    """

    EMA_FAST = 10
    EMA_SLOW = 50
    # Default only -- engine.py actually passes settings.tick_vol_window
    # into __init__ below, so TICK_VOL_WINDOW in config/.env takes effect.
    VOL_WINDOW = 20
    # EMA(50)'s weight on a data point n ticks back decays as
    # (1 - 2/51)^n, i.e. below 0.1% by n=~170. Keeping this many most-recent
    # ticks and recomputing the EMA from scratch on every evaluate() call
    # (rather than hand-maintaining incremental running state tick-to-tick)
    # is therefore numerically indistinguishable from a true
    # infinite-history streaming EMA, while being simpler to get right and
    # easier to test -- there's no running state that can silently drift or
    # desync across a reconnect.
    HISTORY = 320
    # Warm-up gate: below this many ticks, EMA(50) and the volatility
    # window aren't meaningfully populated yet. At R_25's observed ~2s/tick
    # rate this is roughly 6-7 minutes after (re)connect -- see engine.py's
    # WARMING_UP status and the README.
    MIN_TICKS = 200

    def __init__(self, vol_window: int | None = None):
        self._ticks: deque[Tick] = deque(maxlen=self.HISTORY)
        # Regression fix (2026-07-26): this used to silently ignore
        # TICK_VOL_WINDOW from config entirely -- VOL_WINDOW was a
        # class-level constant only, never actually read from settings, so
        # changing TICK_VOL_WINDOW in .env had zero effect on live
        # behavior. engine.py now passes settings.tick_vol_window here.
        self.vol_window = vol_window if vol_window is not None else self.VOL_WINDOW

    def push_tick(self, tick: Tick) -> None:
        self._ticks.append(tick)

    @property
    def tick_count(self) -> int:
        return len(self._ticks)

    @property
    def ready(self) -> bool:
        return self.tick_count >= self.MIN_TICKS

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        k = 2 / (period + 1)
        value = values[0]
        for x in values[1:]:
            value = x * k + value * (1 - k)
        return value

    def evaluate(self) -> Optional[SignalDecision]:
        if not self.ready:
            return None
        prices = [t.price for t in self._ticks]

        ema_fast = self._ema(prices, self.EMA_FAST)
        ema_slow = self._ema(prices, self.EMA_SLOW)

        recent = prices[-(self.vol_window + 1):]
        diffs = [b - a for a, b in zip(recent, recent[1:])]
        vol = statistics.stdev(diffs) if len(diffs) >= 2 else 0.0
        if vol <= 0:
            # A perfectly flat window (zero movement across every one of
            # the last vol_window ticks) most likely indicates a
            # frozen/stale price feed rather than genuine market
            # inactivity -- R_25 is a continuously-generated synthetic
            # index that in practice never shows exactly zero variation
            # over a real 20-tick span. Skip rather than trade on a
            # signal that may not reflect live prices. (Does not affect
            # barrier sizing -- see config.py's barrier_fixed_offset.)
            return None

        last_price = prices[-1]
        spread_bps = round(abs(ema_fast - ema_slow) / last_price * 10000)

        if ema_fast > ema_slow:
            return SignalDecision("UP", f"EMA{self.EMA_FAST} > EMA{self.EMA_SLOW} (tick)", vol=vol, score=spread_bps)
        if ema_fast < ema_slow:
            return SignalDecision("DOWN", f"EMA{self.EMA_FAST} < EMA{self.EMA_SLOW} (tick)", vol=vol, score=spread_bps)
        return None
