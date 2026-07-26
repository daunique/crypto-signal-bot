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
    # Rolling tick volatility (stdev of the last VOL_WINDOW tick-to-tick
    # price changes). engine.py sizes the real Higher/Lower barrier as a
    # fraction of this -- the tick-strategy equivalent of the old candle
    # strategy's `atr`.
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
    Higher/Lower contract with a barrier sized at 0.25x the rolling 20-tick
    price volatility.

    This is the strategy validated by backtest against ~199 days of R_25
    tick data (see backtest_report.md, tick_backtest_addendum.md, and
    best_config_bf25.json from the chat session that produced this bot
    revision) -- not a default guess. Out-of-sample, full-dataset results
    for these exact parameters (duration=10 ticks, barrier=0.25x rolling
    20-tick stdev): ~46.9% overall win rate, ~44.9% minimum single-day win
    rate, ~4,300 signals/day, worst observed same-day losing streak 20.
    That min-daily figure is *below* the 47% floor that was actually asked
    for -- it's the closest the search got, not a strategy that hits it.
    See tick_backtest_addendum.md for why (this account's synthetic index
    ticks have ~50% raw directional accuracy with no real edge for any
    indicator combination tried; the barrier structurally requires beating
    that, not just matching it).

    Operates directly on the raw tick stream rather than completed candles.
    Every incoming tick is fed to `push_tick()`; `evaluate()` reads off the
    current EMA/volatility state whenever it's called. The engine only
    calls `evaluate()` at decision points -- every `trade_duration_ticks`
    ticks (see engine.py) -- so trades are non-overlapping, matching
    exactly how the backtest that produced the numbers above was run.
    Evaluating on overlapping windows was never simulated or validated.
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
            # A perfectly flat recent window can't size a real barrier
            # (engine.py/deriv.py both reject a non-positive barrier
            # offset outright) -- skip rather than send a degenerate trade.
            return None

        last_price = prices[-1]
        spread_bps = round(abs(ema_fast - ema_slow) / last_price * 10000)

        if ema_fast > ema_slow:
            return SignalDecision("UP", f"EMA{self.EMA_FAST} > EMA{self.EMA_SLOW} (tick)", vol=vol, score=spread_bps)
        if ema_fast < ema_slow:
            return SignalDecision("DOWN", f"EMA{self.EMA_FAST} < EMA{self.EMA_SLOW} (tick)", vol=vol, score=spread_bps)
        return None
