from backend.app.config import Settings, BUILD_VERSION


def test_bot_mode_is_normalized_to_lowercase_and_stripped():
    # Regression test: model_post_init validates bot_mode case-insensitively
    # ("Demo", "DEMO", "demo " all pass), but deriv.py's account selection
    # does an exact `== "demo"` comparison. Without normalizing bot_mode in
    # place, any non-canonical casing/whitespace here would silently select
    # the *real* (live) account instead of demo, even though startup looked
    # completely healthy.
    assert Settings(bot_mode="Demo").bot_mode == "demo"
    assert Settings(bot_mode="DEMO").bot_mode == "demo"
    assert Settings(bot_mode="  live  ").bot_mode == "live"
    assert Settings(bot_mode="LIVE").bot_mode == "live"


def test_bot_mode_rejects_unknown_values():
    try:
        Settings(bot_mode="paper")
        assert False, "expected ValueError for an unrecognized BOT_MODE"
    except ValueError:
        pass


def test_barrier_fixed_offset_must_be_positive():
    assert Settings(barrier_fixed_offset=0.25).barrier_fixed_offset == 0.25
    for bad_value in (0, -0.1):
        try:
            Settings(barrier_fixed_offset=bad_value)
            assert False, "expected ValueError for a non-positive BARRIER_FIXED_OFFSET"
        except ValueError:
            pass


def test_trade_duration_ticks_must_be_one_to_ten():
    # Deriv's tick-duration contracts are only valid for 1-10 ticks; the
    # strategy this bot ships with (strategy.py) was backtested specifically
    # at 10. Reject anything outside that range at startup rather than
    # letting Deriv reject every single trade at execution time.
    assert Settings(trade_duration_ticks=10).trade_duration_ticks == 10
    assert Settings(trade_duration_ticks=1).trade_duration_ticks == 1
    for bad_value in (0, -1, 11, 100):
        try:
            Settings(trade_duration_ticks=bad_value)
            assert False, "expected ValueError for TRADE_DURATION_TICKS outside 1-10"
        except ValueError:
            pass


def test_tick_vol_window_must_be_at_least_two():
    assert Settings(tick_vol_window=20).tick_vol_window == 20
    for bad_value in (0, 1, -5):
        try:
            Settings(tick_vol_window=bad_value)
            assert False, "expected ValueError for TICK_VOL_WINDOW < 2"
        except ValueError:
            pass


def test_build_version_lives_in_config_not_main():
    # BUILD_VERSION lives here (not main.py) specifically so api.py can
    # import it without a main.py <-> api.py circular import (main.py
    # imports the router from api.py). Guards against that getting
    # "cleaned up" back into main.py in a future edit.
    assert isinstance(BUILD_VERSION, str) and BUILD_VERSION
