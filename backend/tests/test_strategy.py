import pytest

from backend.app.strategy import RollingVolatility, VolatilityTimingStrategy


def test_rolling_volatility_none_until_window_fills():
    rv = RollingVolatility(window=3)
    assert rv.update(1.0) is None   # first price: no delta yet
    assert rv.update(2.0) is None   # 1 delta, need 3
    assert rv.update(3.0) is None   # 2 deltas, need 3
    assert rv.update(4.0) is not None  # 3 deltas: window full


def test_rolling_volatility_matches_known_value():
    # deltas of [1,2,3,4,5,6,7,8] are all exactly 1.0 -> zero variance
    rv = RollingVolatility(window=3)
    outs = [rv.update(p) for p in [1., 2., 3., 4., 5., 6., 7., 8.]]
    for v in outs[3:]:
        assert v == pytest.approx(0.0, abs=1e-9)


def test_rolling_volatility_reacts_to_varying_deltas():
    # A window that has just seen a big jump should read higher than one
    # that hasn't -- this is the entire premise the strategy trades on.
    rv = RollingVolatility(window=3)
    for p in [100.0, 100.01, 100.02, 100.03]:  # calm: tiny, steady deltas
        calm_vol = rv.update(p)
    rv2 = RollingVolatility(window=3)
    for p in [100.0, 100.01, 105.0, 100.02]:  # one big jump in the window
        volatile_vol = rv2.update(p)
    assert volatile_vol > calm_vol


def test_rolling_volatility_rejects_bad_window():
    with pytest.raises(ValueError):
        RollingVolatility(window=1)
    with pytest.raises(ValueError):
        RollingVolatility(window=0)


def test_strategy_fires_only_when_vol_at_or_above_threshold():
    strat = VolatilityTimingStrategy(bet_direction="LOWER", target_percentile=90.0)
    assert strat.evaluate(current_vol=0.05, threshold=0.10) is None       # below threshold
    decision = strat.evaluate(current_vol=0.10, threshold=0.10)           # exactly at threshold
    assert decision is not None
    decision2 = strat.evaluate(current_vol=0.20, threshold=0.10)          # above threshold
    assert decision2 is not None


def test_strategy_returns_none_when_inputs_missing():
    strat = VolatilityTimingStrategy(bet_direction="LOWER", target_percentile=90.0)
    assert strat.evaluate(current_vol=None, threshold=0.10) is None
    assert strat.evaluate(current_vol=0.20, threshold=None) is None
    assert strat.evaluate(current_vol=None, threshold=None) is None


def test_strategy_direction_is_fixed_not_computed():
    # No directional edge was found in backtesting (see report); direction
    # is a fixed configuration choice, so it must be identical regardless
    # of how large current_vol is relative to the threshold.
    strat = VolatilityTimingStrategy(bet_direction="LOWER", target_percentile=90.0)
    for vol in (0.10, 0.50, 5.0):
        decision = strat.evaluate(current_vol=vol, threshold=0.10)
        assert decision.direction == "LOWER"

    strat_higher = VolatilityTimingStrategy(bet_direction="HIGHER", target_percentile=90.0)
    decision = strat_higher.evaluate(current_vol=0.5, threshold=0.10)
    assert decision.direction == "HIGHER"


def test_strategy_rejects_bad_direction():
    with pytest.raises(ValueError):
        VolatilityTimingStrategy(bet_direction="SIDEWAYS", target_percentile=90.0)


def test_signal_decision_carries_the_values_it_fired_on():
    # These end up on the Signal DB row (current_vol/vol_threshold) purely
    # for audit/display -- assert they're the actual inputs, not the
    # configured percentile or some other derived number.
    strat = VolatilityTimingStrategy(bet_direction="LOWER", target_percentile=95.0)
    decision = strat.evaluate(current_vol=0.234, threshold=0.200)
    assert decision.current_vol == 0.234
    assert decision.threshold == 0.200
    assert decision.percentile == 95.0
