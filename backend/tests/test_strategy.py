from backend.app.strategy import Tick, TickEMAStrategy, SignalDecision


def _push_constant_walk(strategy: TickEMAStrategy, n: int, start_price: float, step: float, start_epoch: int = 0):
    price = start_price
    for i in range(n):
        strategy.push_tick(Tick(start_epoch + i * 2, price))
        price += step


def _push_trending_walk(strategy: TickEMAStrategy, n: int, start_price: float, avg_step: float, start_epoch: int = 0):
    # Alternates the step size around avg_step (rather than a perfectly
    # constant step) so tick-to-tick volatility is genuinely nonzero, while
    # still trending in one direction on average -- a perfectly linear walk
    # has zero variance in its diffs, which would make the strategy's own
    # vol<=0 guard return None regardless of the trend, defeating the point
    # of these tests.
    price = start_price
    for i in range(n):
        strategy.push_tick(Tick(start_epoch + i * 2, price))
        price += avg_step * (1.4 if i % 2 == 0 else 0.6)


def test_strategy_returns_none_before_warmup():
    s = TickEMAStrategy()
    _push_constant_walk(s, s.MIN_TICKS - 1, 100.0, 0.01)
    assert not s.ready
    assert s.evaluate() is None


def test_strategy_ready_once_warmup_reached():
    s = TickEMAStrategy()
    _push_constant_walk(s, s.MIN_TICKS, 100.0, 0.01)
    assert s.ready
    assert s.tick_count == s.MIN_TICKS


def test_strategy_direction_is_up_or_down_only():
    s = TickEMAStrategy()
    _push_trending_walk(s, s.HISTORY, 100.0, 0.05)
    result = s.evaluate()
    assert result is None or isinstance(result, SignalDecision)
    assert result is None or result.direction in {"UP", "DOWN"}


def test_strategy_uptrend_produces_up_with_positive_vol():
    # A rising (but not perfectly linear) price stream: EMA(fast) should
    # sit above EMA(slow).
    s = TickEMAStrategy()
    _push_trending_walk(s, s.HISTORY, 100.0, 0.05)
    result = s.evaluate()
    assert result is not None
    assert result.direction == "UP"
    # engine.py sizes the real Higher/Lower barrier as a fraction of this,
    # so it must actually be positive (not left at some degenerate
    # default) whenever a decision is returned.
    assert result.vol > 0
    assert result.score >= 0


def test_strategy_downtrend_produces_down():
    s = TickEMAStrategy()
    _push_trending_walk(s, s.HISTORY, 200.0, -0.05)
    result = s.evaluate()
    assert result is not None
    assert result.direction == "DOWN"
    assert result.vol > 0


def test_strategy_flat_zero_volatility_series_returns_none():
    # A perfectly flat price stream can't size a real barrier (vol would be
    # exactly 0) -- the strategy should skip rather than hand engine.py a
    # degenerate barrier_offset of 0, which deriv.py's build_proposal_payload
    # rejects outright.
    s = TickEMAStrategy()
    _push_constant_walk(s, s.HISTORY, 150.0, 0.0)
    assert s.evaluate() is None


def test_push_tick_deque_is_bounded_by_history():
    s = TickEMAStrategy()
    _push_constant_walk(s, s.HISTORY + 100, 100.0, 0.01)
    assert s.tick_count == s.HISTORY


def test_vol_window_is_actually_configurable():
    # Regression test: vol_window used to be a class-level constant only
    # (VOL_WINDOW), never actually read from settings.tick_vol_window --
    # so changing TICK_VOL_WINDOW in .env had zero effect on live
    # behavior. engine.py now passes it into __init__ explicitly.
    default_strategy = TickEMAStrategy()
    assert default_strategy.vol_window == TickEMAStrategy.VOL_WINDOW

    custom_strategy = TickEMAStrategy(vol_window=5)
    assert custom_strategy.vol_window == 5

    # A smaller window should generally produce a different (typically
    # smaller-sample, noisier) volatility estimate than a larger one on the
    # same tick data -- confirms the parameter actually reaches evaluate(),
    # not just that the attribute is stored.
    _push_trending_walk(custom_strategy, custom_strategy.HISTORY, 100.0, 0.05)
    result_custom = custom_strategy.evaluate()

    default_strategy2 = TickEMAStrategy(vol_window=20)
    _push_trending_walk(default_strategy2, default_strategy2.HISTORY, 100.0, 0.05)
    result_default = default_strategy2.evaluate()

    assert result_custom is not None and result_default is not None
    assert result_custom.vol != result_default.vol
