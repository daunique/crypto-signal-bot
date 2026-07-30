from backend.app.pnl_tracker import PnLTrackState, apply_trade_outcome


def test_stays_on_main_until_profit_target_reached():
    state = PnLTrackState()
    for profit in (3.0, 4.0, 2.0):  # cumulative 3, 7, 9 -- never reaches 10
        state, switch = apply_trade_outcome(state, profit, was_live_mode=False)
        assert state.track == "main"
        assert switch is None
    assert state.delta == 9.0


def test_switches_main_to_sub_once_profit_target_reached_demo_no_mode_switch():
    state = PnLTrackState()
    state, switch = apply_trade_outcome(state, 5.0, was_live_mode=False)
    assert state.track == "main" and switch is None
    state, switch = apply_trade_outcome(state, 5.0, was_live_mode=False)  # delta now 10
    assert state.track == "sub"
    assert state.delta == 0.0          # resets on switch
    assert state.consec_losses == 0
    assert switch is None              # demo account: bookkeeping only, no mode change
    assert state.auto_demo_active is False


def test_switches_main_to_sub_on_live_account_also_switches_to_demo():
    state = PnLTrackState()
    state, switch = apply_trade_outcome(state, 10.0, was_live_mode=True)
    assert state.track == "sub"
    assert switch == "demo"
    assert state.auto_demo_active is True


def test_overshooting_the_target_in_one_trade_still_switches():
    # A single win can jump straight past the target -- should still fire,
    # not require landing exactly on it.
    state = PnLTrackState()
    state, switch = apply_trade_outcome(state, 17.0, was_live_mode=True)
    assert state.track == "sub" and switch == "demo"


def test_losses_while_on_main_do_not_trigger_a_switch():
    state = PnLTrackState()
    for profit in (-1.0, -1.0, -1.0, -1.0, -1.0, -1.0):  # even 6 losses in a row
        state, switch = apply_trade_outcome(state, profit, was_live_mode=True)
        assert state.track == "main"
        assert switch is None
    assert state.delta == -6.0
    assert state.consec_losses == 6  # tracked for display, but main's own exit rule ignores it


def test_sub_switches_back_to_main_after_loss_streak_limit():
    state = PnLTrackState(track="sub", auto_demo_active=True)
    for i in range(4):
        state, switch = apply_trade_outcome(state, -1.0, was_live_mode=False)
        assert state.track == "sub"
        assert switch is None
        assert state.consec_losses == i + 1
    state, switch = apply_trade_outcome(state, -1.0, was_live_mode=False)  # 5th consecutive loss
    assert state.track == "main"
    assert state.consec_losses == 0
    assert state.delta == 0.0


def test_sub_switches_back_to_live_only_if_auto_demo_was_active():
    # If the account was already demo before entering "sub" (never
    # auto-switched), returning to "main" must NOT force it to "live" --
    # that would override the user's own choice of demo mode.
    state = PnLTrackState(track="sub", auto_demo_active=False)
    for _ in range(5):
        state, switch = apply_trade_outcome(state, -1.0, was_live_mode=False)
    assert state.track == "main"
    assert switch is None


def test_sub_switches_back_to_live_when_auto_demo_was_active():
    state = PnLTrackState(track="sub", auto_demo_active=True)
    for _ in range(4):
        state, switch = apply_trade_outcome(state, -1.0, was_live_mode=False)
    state, switch = apply_trade_outcome(state, -1.0, was_live_mode=False)
    assert state.track == "main"
    assert switch == "live"
    assert state.auto_demo_active is False


def test_a_win_on_sub_resets_the_loss_streak_and_does_not_switch_early():
    state = PnLTrackState(track="sub", auto_demo_active=True)
    for _ in range(4):
        state, switch = apply_trade_outcome(state, -1.0, was_live_mode=False)
    assert state.consec_losses == 4
    state, switch = apply_trade_outcome(state, 2.0, was_live_mode=False)  # a win breaks the streak
    assert state.track == "sub"
    assert state.consec_losses == 0
    assert switch is None
    # confirm it now takes a fresh 5 losses, not just 1 more
    for _ in range(4):
        state, switch = apply_trade_outcome(state, -1.0, was_live_mode=False)
        assert state.track == "sub"
    state, switch = apply_trade_outcome(state, -1.0, was_live_mode=False)
    assert state.track == "main"


def test_thresholds_are_configurable_not_hardcoded():
    state = PnLTrackState()
    state, switch = apply_trade_outcome(state, 6.0, was_live_mode=True, profit_target=5.0, loss_streak_limit=2)
    assert state.track == "sub" and switch == "demo"
    state, switch = apply_trade_outcome(state, -1.0, was_live_mode=True, profit_target=5.0, loss_streak_limit=2)
    assert state.track == "sub"
    state, switch = apply_trade_outcome(state, -1.0, was_live_mode=True, profit_target=5.0, loss_streak_limit=2)
    assert state.track == "main" and switch == "live"


def test_full_cycle_matches_the_specified_behavior():
    """End-to-end: main accumulates to +$10, switches to sub (+demo on a
    live account); sub takes a 5-loss streak; switches back to main
    (+live)."""
    state = PnLTrackState()
    trace = []
    for profit in (4.0, 4.0, 3.0):  # crosses +10 on the 3rd trade
        state, switch = apply_trade_outcome(state, profit, was_live_mode=True)
        trace.append((state.track, switch))
    assert trace[-1] == ("sub", "demo")

    for profit in (2.0, -1.0, -1.0, -1.0, -1.0, -1.0):  # a win, then 5 in a row
        state, switch = apply_trade_outcome(state, profit, was_live_mode=True)
        trace.append((state.track, switch))
    assert trace[-1] == ("main", "live")
    assert state.delta == 0.0 and state.consec_losses == 0 and not state.auto_demo_active
