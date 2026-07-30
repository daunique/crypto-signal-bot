"""
Main/sub PnL-track state machine -- a risk-management overlay on top of the
trading strategy itself (see strategy.py), not a signal for when to trade.

Rule (as specified): trades are recorded against "main" until main's PnL
(accumulated since it was last entered) reaches +profit_target; the next
trade then switches to "sub". Trades stay on "sub" until a loss streak of
loss_streak_limit happens; the next trade after that switches back to
"main". On a **live** account, entering "sub" this way also switches the bot
to demo mode, and returning to "main" switches it back to live -- on a demo
account, main/sub is a pure bookkeeping split (already in demo either way).

Kept as a pure function (no I/O, no DB) so it's trivially unit-testable,
matching this project's existing split between decision logic (here,
strategy.py) and stateful orchestration (engine.py). engine.py owns loading/
persisting PnLTrackState and acting on the returned mode_switch.
"""

from dataclasses import dataclass


@dataclass
class PnLTrackState:
    track: str = "main"          # "main" or "sub"
    delta: float = 0.0           # PnL accumulated since entering the current track
    consec_losses: int = 0       # consecutive losses since entering the current track
    auto_demo_active: bool = False  # True only while in a system-triggered (not user-chosen) demo period


def apply_trade_outcome(
    state: PnLTrackState,
    profit: float,
    was_live_mode: bool,
    profit_target: float = 10.0,
    loss_streak_limit: int = 5,
) -> tuple[PnLTrackState, str | None]:
    """Given the just-settled trade's profit (positive=win, negative/zero=
    loss) and whether it was placed in live mode, returns the updated state
    plus an optional mode_switch ("demo", "live", or None) that the caller
    should apply for the *next* trade -- never the one that just settled.
    """
    new_delta = state.delta + profit
    new_consec_losses = 0 if profit > 0 else state.consec_losses + 1
    track = state.track
    auto_demo_active = state.auto_demo_active
    mode_switch: str | None = None

    if track == "main" and new_delta >= profit_target:
        track = "sub"
        new_delta = 0.0
        new_consec_losses = 0
        if was_live_mode:
            auto_demo_active = True
            mode_switch = "demo"
    elif track == "sub" and new_consec_losses >= loss_streak_limit:
        track = "main"
        new_delta = 0.0
        new_consec_losses = 0
        if auto_demo_active:
            mode_switch = "live"
        auto_demo_active = False

    return (
        PnLTrackState(track=track, delta=new_delta, consec_losses=new_consec_losses, auto_demo_active=auto_demo_active),
        mode_switch,
    )
