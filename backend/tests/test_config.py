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


def test_barrier_atr_fraction_must_be_positive():
    assert Settings(barrier_atr_fraction=0.25).barrier_atr_fraction == 0.25
    for bad_value in (0, -0.1):
        try:
            Settings(barrier_atr_fraction=bad_value)
            assert False, "expected ValueError for a non-positive BARRIER_ATR_FRACTION"
        except ValueError:
            pass


def test_build_version_lives_in_config_not_main():
    # BUILD_VERSION lives here (not main.py) specifically so api.py can
    # import it without a main.py <-> api.py circular import (main.py
    # imports the router from api.py). Guards against that getting
    # "cleaned up" back into main.py in a future edit.
    assert isinstance(BUILD_VERSION, str) and BUILD_VERSION


def test_invert_signals_defaults_to_false():
    # Regression test: invert_signals must default to False (2026-07-26) --
    # it previously was an unconditional flip with no setting at all.
    assert Settings().invert_signals is False
    assert Settings(invert_signals=True).invert_signals is True
