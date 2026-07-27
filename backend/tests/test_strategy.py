from backend.app.strategy import Candle, R25Strategy


def test_strategy_returns_none_with_insufficient_data():
    s = R25Strategy()
    candles = [Candle(i, 1, 1, 1, 1) for i in range(10)]
    assert s.evaluate(candles) is None


def test_strategy_direction_is_up_or_down_only():
    s = R25Strategy(minimum_score=1)
    candles = []
    for i in range(60):
        price = 100 + i
        candles.append(Candle(i * 180, price - 0.2, price + 1, price - 0.5, price))
    result = s.evaluate(candles)
    assert result is None or result.direction in {"UP", "DOWN"}
    assert result is None or result.atr > 0


def test_strategy_decision_carries_atr_for_barrier_sizing():
    # engine.py sizes the real Higher/Lower barrier as a fraction of this
    # value, so it must actually be populated (not left at the dataclass
    # default) whenever a decision is returned.
    s = R25Strategy(minimum_score=1)
    candles = []
    for i in range(60):
        price = 100 + i
        candles.append(Candle(i * 180, price - 0.2, price + 1, price - 0.5, price))
    result = s.evaluate(candles)
    assert result is not None
    assert result.atr == 1.5  # every candle here has a fixed 1.5 high-low range
