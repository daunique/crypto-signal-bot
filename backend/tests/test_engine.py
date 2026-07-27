import pytest

from backend.app.engine import invert_direction


def test_invert_direction_flips_up_and_down():
    assert invert_direction("UP") == "DOWN"
    assert invert_direction("DOWN") == "UP"


def test_invert_direction_is_its_own_inverse():
    # Regression guard: inverting twice must return the original value --
    # if this ever broke, on_decision_tick's Signal.direction/contract_type/
    # barrier-sign chain would silently trade something other than a clean
    # flip of the raw reading.
    for direction in ("UP", "DOWN"):
        assert invert_direction(invert_direction(direction)) == direction


def test_invert_direction_rejects_unknown_value():
    # strategy.py's SignalDecision only ever produces "UP"/"DOWN" (see
    # TickEMAStrategy.evaluate()), so this should never fire in practice --
    # but engine.py fails loudly here rather than silently mistrading if
    # that ever changes.
    with pytest.raises(ValueError):
        invert_direction("SIDEWAYS")
