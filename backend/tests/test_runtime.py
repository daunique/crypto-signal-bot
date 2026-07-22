from backend.app.config import Settings


def test_market_barrier_is_symbol_specific():
    s = Settings(market_barriers="R_25=0.375,R_10=0.2")
    assert s.barrier_for_symbol("R_25") == 0.375
    assert s.barrier_for_symbol("R_10") == 0.2


def test_unknown_market_barrier_fails():
    s = Settings(market_barriers="R_25=0.375")
    try:
        s.barrier_for_symbol("R_100")
        assert False
    except RuntimeError:
        assert True


def test_pat_is_preferred():
    s = Settings(deriv_pat="pat", deriv_api_token="old", deriv_token="older")
    assert s.auth_token == "pat"
