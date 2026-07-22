from backend.app.strategy import Candle, R25Strategy

def test_strategy_returns_none_with_insufficient_data():
    s = R25Strategy()
    candles = [Candle(i, 1, 1, 1, 1) for i in range(10)]
    assert s.evaluate(candles) is None
