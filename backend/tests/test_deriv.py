import pytest

from backend.app.deriv import DerivClient, DerivAPIError


def test_deriv_api_error_exposes_code_and_subcode():
    # Regression test: engine.py's barrier-retry logic needs to distinguish
    # an InvalidBarrier rejection from any other API error programmatically
    # (exc.subcode), not by parsing the stringified exception message.
    exc = DerivAPIError({
        "code": "ContractBuyValidationError",
        "message": "Invalid barrier.",
        "subcode": "InvalidBarrier",
    })
    assert exc.code == "ContractBuyValidationError"
    assert exc.subcode == "InvalidBarrier"
    assert exc.message == "Invalid barrier."


def test_proposal_payload_uses_higher_lower_contract_types():
    # Confirmed directly against this account's own live contracts_for
    # response (2026-07-24, see README): the "higherlower" contract
    # category uses contract_type HIGHER/LOWER, separate from "callput"
    # (CALL/PUT, which is the barrier-free ATM Rise/Fall product on this
    # account). The original InvalidBarrierSingle bug was a missing
    # barrier field, not a wrong contract_type -- HIGHER/LOWER was already
    # correct; an earlier version of this incorrectly "fixed" it to
    # CALL/PUT based on Deriv's general docs, which describe a different
    # convention than what this account's API actually accepts. That's
    # also why every barrier value was rejected as InvalidBarrier
    # regardless of magnitude or sign: CALL/PUT was never going to accept
    # a barrier here, no matter its value.
    client = DerivClient()
    assert client.build_proposal_payload("R_25", "UP", 1.0, "USD", 10, 0.5)["contract_type"] == "HIGHER"
    assert client.build_proposal_payload("R_25", "DOWN", 1.0, "USD", 10, 0.5)["contract_type"] == "LOWER"


def test_proposal_payload_includes_signed_barrier_for_higher_lower():
    # This bot trades genuine Higher/Lower contracts, which require a
    # signed, relative barrier offset: positive for HIGHER (barrier above
    # spot), negative for LOWER (barrier below spot). Confirmed against
    # this account's own contracts_for response (see README).
    client = DerivClient()
    up_payload = client.build_proposal_payload("R_25", "UP", 2.5, "USD", 10, 0.523)
    down_payload = client.build_proposal_payload("R_25", "DOWN", 2.5, "USD", 10, 0.523)
    assert up_payload == {
        "proposal": 1,
        "amount": 2.5,
        "basis": "stake",
        "contract_type": "HIGHER",
        "currency": "USD",
        "duration": 10,
        "duration_unit": "t",
        "underlying_symbol": "R_25",
        "barrier": "+0.523",
    }
    assert down_payload["barrier"] == "-0.523"


def test_proposal_payload_supports_seconds_duration_unit_too():
    # duration_unit defaults to "t" (ticks) since that's what this bot's
    # backtested strategy uses, but the parameter itself stays general --
    # nothing about a Higher/Lower proposal *requires* tick duration, and a
    # future duration change shouldn't need a deriv.py edit, only a config
    # one.
    client = DerivClient()
    payload = client.build_proposal_payload("R_25", "UP", 1.0, "USD", 60, 0.5, duration_unit="s")
    assert payload["duration"] == 60
    assert payload["duration_unit"] == "s"


def test_proposal_payload_rejects_bad_duration_unit():
    client = DerivClient()
    with pytest.raises(ValueError):
        client.build_proposal_payload("R_25", "UP", 1.0, "USD", 10, 0.5, duration_unit="m")


def test_proposal_payload_rejects_non_positive_barrier_offset():
    # A Higher/Lower contract with no real distance from spot isn't a valid
    # barrier -- this should fail loudly rather than silently send a
    # malformed/zero barrier.
    client = DerivClient()
    for bad_offset in (0, -0.1):
        with pytest.raises(ValueError):
            client.build_proposal_payload("R_25", "UP", 1.0, "USD", 10, bad_offset)


def test_proposal_payload_rejects_unknown_direction():
    client = DerivClient()
    with pytest.raises(ValueError):
        client.build_proposal_payload("R_25", "SIDEWAYS", 1.0, "USD", 10, 0.5)


def test_proposal_open_contract_payload_omits_subscribe():
    # Regression test: Deriv's schema for proposal_open_contract says
    # `subscribe` is optional but its only legal value is the integer 1
    # (there's no valid "0" for a one-shot check -- you omit the field
    # entirely). Sending 0, as this used to, was rejected with
    # InputValidationFailed on every single settlement poll, so no trade
    # ever recorded a win/loss.
    client = DerivClient()
    payload = client.build_proposal_open_contract_payload("6467434599")
    assert payload == {"proposal_open_contract": 1, "contract_id": 6467434599}
    assert "subscribe" not in payload


def test_trade_connected_reflects_actual_connection_state():
    # Regression test: a dead trade connection with a still-alive public
    # connection used to go undetected until a trade was attempted, and
    # even then the resulting error was swallowed silently (see README).
    # trade_connected is what tick_loop() now checks on every tick.
    client = DerivClient()
    assert client.trade_connected is False  # nothing connected yet

    class FakeTask:
        def __init__(self, done):
            self._done = done
        def done(self):
            return self._done

    client.trade_ws = object()
    client._trade_reader_task = FakeTask(done=False)
    assert client.trade_connected is True

    client._trade_reader_task = FakeTask(done=True)  # reader exited/crashed
    assert client.trade_connected is False

    client._trade_reader_task = FakeTask(done=False)
    client.trade_ws = None  # e.g. after close()
    assert client.trade_connected is False


def test_contracts_for_payload_matches_deriv_confirmed_shape():
    # Regression test: an earlier version of this guessed {"contracts_for":
    # 1, "underlying_symbol": symbol, "currency": ...}, matching the
    # flag=1-plus-separate-field pattern used by proposal/
    # proposal_open_contract. Deriv's actual error response ("Properties
    # not allowed: currency, underlying_symbol" -- notably NOT complaining
    # about contracts_for itself) showed that guess was wrong: this
    # endpoint takes the symbol as the value of contracts_for directly.
    client = DerivClient()
    payload = client.build_contracts_for_payload("R_25")
    assert payload == {"contracts_for": "R_25"}
