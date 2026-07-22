from dataclasses import dataclass
from typing import Optional
import math


@dataclass
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class SignalDecision:
    direction: str
    score: int
    reason: str


class R25Strategy:
    """
    Modular starter strategy.

    All features are computed from completed candles. The decision is made
    only at a 180-second candle boundary. This implementation is intentionally
    replaceable as the backtest strategy evolves.
    """

    def __init__(self, minimum_score: int = 6):
        self.minimum_score = minimum_score

    def evaluate(self, candles: list[Candle]) -> Optional[SignalDecision]:
        if len(candles) < 55:
            return None

        c = candles[-1]
        closes = [x.close for x in candles]
        ema9 = self.ema(closes, 9)
        ema21 = self.ema(closes, 21)
        ema50 = self.ema(closes, 50)

        score_up = 0
        score_down = 0
        reasons_up = []
        reasons_down = []

        if ema9 > ema21:
            score_up += 2
            reasons_up.append("EMA9>EMA21")
        elif ema9 < ema21:
            score_down += 2
            reasons_down.append("EMA9<EMA21")

        if ema21 > ema50:
            score_up += 1
            reasons_up.append("EMA21>EMA50")
        elif ema21 < ema50:
            score_down += 1
            reasons_down.append("EMA21<EMA50")

        if closes[-1] > closes[-4]:
            score_up += 1
            reasons_up.append("3-candle momentum up")
        elif closes[-1] < closes[-4]:
            score_down += 1
            reasons_down.append("3-candle momentum down")

        if closes[-1] > closes[-6]:
            score_up += 1
            reasons_up.append("5-candle momentum up")
        elif closes[-1] < closes[-6]:
            score_down += 1
            reasons_down.append("5-candle momentum down")

        candle_range = max(c.high - c.low, 1e-12)
        close_position = (c.close - c.low) / candle_range

        if c.close > c.open and close_position >= 0.60:
            score_up += 1
            reasons_up.append("strong bullish close")
        if c.close < c.open and close_position <= 0.40:
            score_down += 1
            reasons_down.append("strong bearish close")

        atr = self.atr(candles[-15:])
        avg_range = sum(x.high - x.low for x in candles[-15:]) / 15
        if atr >= 0.8 * avg_range:
            if score_up > score_down:
                score_up += 1
                reasons_up.append("acceptable volatility")
            elif score_down > score_up:
                score_down += 1
                reasons_down.append("acceptable volatility")

        if score_up >= self.minimum_score and score_up > score_down:
            return SignalDecision("UP", score_up, ", ".join(reasons_up))
        if score_down >= self.minimum_score and score_down > score_up:
            return SignalDecision("DOWN", score_down, ", ".join(reasons_down))
        return None

    @staticmethod
    def ema(values: list[float], period: int) -> float:
        k = 2 / (period + 1)
        value = values[0]
        for x in values[1:]:
            value = x * k + value * (1 - k)
        return value

    @staticmethod
    def atr(candles: list[Candle]) -> float:
        if not candles:
            return 0.0
        return sum(c.high - c.low for c in candles) / len(candles)
