import pytest

from backend.app.deriv import DerivClient


def test_proposal_payload_uses_deriv_contract_types_not_direction_labels():
    # Regression test for InvalidBarrierSingle: every proposal used to send
    # contract_type "HIGHER"/"LOWER", which Deriv does not recognize. The
    # real values (confirmed against developers.deriv.com/docs/higherlower
    # and legacy-docs.deriv.com/docs/higherlower) are CALL/PUT.
    client = DerivClient()
    assert client.build_proposal_payload("R_25", "UP", 1.0, "USD", 180)["contract_type"] == "CALL"
    assert client.build_proposal_payload("R_25", "DOWN", 1.0, "USD", 180)["contract_type"] == "PUT"


def test_proposal_payload_is_barrier_free_rise_fall_style():
    # This bot intentionally trades barrier-free (decided purely against the
    # entry spot), not Deriv's separate barrier-based Higher/Lower product,
    # so `barrier` must never appear in the payload.
    client = DerivClient()
    payload = client.build_proposal_payload("R_25", "UP", 2.5, "USD", 180)
    assert "barrier" not in payload
    assert payload == {
        "proposal": 1,
        "amount": 2.5,
        "basis": "stake",
        "contract_type": "CALL",
        "currency": "USD",
        "duration": 180,
        "duration_unit": "s",
        "underlying_symbol": "R_25",
    }


def test_proposal_payload_rejects_unknown_direction():
    client = DerivClient()
    with pytest.raises(ValueError):
        client.build_proposal_payload("R_25", "SIDEWAYS", 1.0, "USD", 180)
