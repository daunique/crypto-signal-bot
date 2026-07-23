from backend.app.config import Settings


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
