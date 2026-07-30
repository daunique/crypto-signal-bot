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


def test_barrier_must_be_positive():
    assert Settings(barrier=0.30).barrier == 0.30
    for bad_value in (0, -0.1):
        try:
            Settings(barrier=bad_value)
            assert False, "expected ValueError for a non-positive BARRIER"
        except ValueError:
            pass


def test_build_version_lives_in_config_not_main():
    # BUILD_VERSION lives here (not main.py) specifically so api.py can
    # import it without a main.py <-> api.py circular import (main.py
    # imports the router from api.py). Guards against that getting
    # "cleaned up" back into main.py in a future edit.
    assert isinstance(BUILD_VERSION, str) and BUILD_VERSION


def test_bet_direction_defaults_to_lower_and_is_normalized():
    # LOWER is the backtested default (barrier 0.30, ~31.9-32.0% win rate at
    # the quoted payout -- see the PnL note at the bottom of this file).
    # Direction has no backtested edge either way (see report), so this only
    # needs to be a fixed, consistent choice.
    assert Settings().bet_direction == "LOWER"
    assert Settings(bet_direction="higher").bet_direction == "HIGHER"
    assert Settings(bet_direction=" Lower ").bet_direction == "LOWER"


def test_bet_direction_rejects_unknown_values():
    try:
        Settings(bet_direction="sideways")
        assert False, "expected ValueError for an unrecognized BET_DIRECTION"
    except ValueError:
        pass


def test_contract_duration_ticks_defaults_to_backtested_value():
    # 10 ticks is what was actually backtested (see report); this isn't an
    # arbitrary default; changing it means trading an unvalidated variant.
    assert Settings().contract_duration_ticks == 10
    try:
        Settings(contract_duration_ticks=0)
        assert False, "expected ValueError for a non-positive CONTRACT_DURATION_TICKS"
    except ValueError:
        pass


def test_volatility_settings_have_backtested_defaults_and_are_validated():
    s = Settings()
    assert s.vol_window_ticks == 100
    assert s.vol_trailing_days == 90
    assert s.vol_target_percentile == 90.0
    for bad_window in (1, 0, -5):
        try:
            Settings(vol_window_ticks=bad_window)
            assert False, "expected ValueError for VOL_WINDOW_TICKS <= 1"
        except ValueError:
            pass
    for bad_pct in (0, 100, -1, 150):
        try:
            Settings(vol_target_percentile=bad_pct)
            assert False, "expected ValueError for VOL_TARGET_PERCENTILE outside (0, 100)"
        except ValueError:
            pass
