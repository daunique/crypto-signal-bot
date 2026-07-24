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


def test_proposal_payload_uses_deriv_contract_types_not_direction_labels():
    # Regression test for InvalidBarrierSingle: every proposal used to send
    # contract_type "HIGHER"/"LOWER", which Deriv does not recognize. The
    # real values (confirmed against developers.deriv.com/docs/higherlower
    # and legacy-docs.deriv.com/docs/higherlower) are CALL/PUT.
    client = DerivClient()
    assert client.build_proposal_payload("R_25", "UP", 1.0, "USD", 180, 0.5)["contract_type"] == "CALL"
    assert client.build_proposal_payload("R_25", "DOWN", 1.0, "USD", 180, 0.5)["contract_type"] == "PUT"


def test_proposal_payload_includes_signed_barrier_for_higher_lower():
    # This bot trades genuine Higher/Lower contracts, which require a
    # signed, relative barrier offset: positive for CALL ("Higher", barrier
    # above spot), negative for PUT ("Lower", barrier below spot). See
    # developers.deriv.com/docs/higherlower.
    client = DerivClient()
    up_payload = client.build_proposal_payload("R_25", "UP", 2.5, "USD", 180, 0.523)
    down_payload = client.build_proposal_payload("R_25", "DOWN", 2.5, "USD", 180, 0.523)
    assert up_payload == {
        "proposal": 1,
        "amount": 2.5,
        "basis": "stake",
        "contract_type": "CALL",
        "currency": "USD",
        "duration": 180,
        "duration_unit": "s",
        "underlying_symbol": "R_25",
        "barrier": "+0.523",
    }
    assert down_payload["barrier"] == "-0.523"


def test_proposal_payload_rejects_non_positive_barrier_offset():
    # A Higher/Lower contract with no real distance from spot isn't a valid
    # barrier -- this should fail loudly rather than silently send a
    # malformed/zero barrier.
    client = DerivClient()
    for bad_offset in (0, -0.1):
        with pytest.raises(ValueError):
            client.build_proposal_payload("R_25", "UP", 1.0, "USD", 180, bad_offset)


def test_proposal_payload_rejects_unknown_direction():
    client = DerivClient()
    with pytest.raises(ValueError):
        client.build_proposal_payload("R_25", "SIDEWAYS", 1.0, "USD", 180, 0.5)


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


def test_contracts_for_payload_uses_underlying_symbol():
    client = DerivClient()
    payload = client.build_contracts_for_payload("R_25")
    assert payload["contracts_for"] == 1
    assert payload["underlying_symbol"] == "R_25"
    assert "symbol" not in payload
