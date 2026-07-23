# Deriv Higher/Lower Bot

Production-oriented 3-minute Deriv Higher/Lower bot.

## 2026-07-23 fix: every trade was being rejected (`InvalidBarrierSingle`)

Every execution attempt was failing with:

```
RuntimeError: {'code': 'InvalidBarrierSingle', 'details': {'field': 'barrier'},
'message': 'Invalid barrier (Single barrier input is expected).', 'subcode': 'InvalidBarrierSingle'}
```

**Root cause:** `backend/app/deriv.py` sent `"contract_type": "HIGHER"` / `"LOWER"` to Deriv. Those are only this bot's own internal direction labels (used for the database and dashboard) — they were never valid values on the wire. Deriv's actual contract_type values for this trade are `CALL` (higher) and `PUT` (lower); see [developers.deriv.com/docs/higherlower](https://developers.deriv.com/docs/higherlower) and [legacy-docs.deriv.com/docs/higherlower](https://legacy-docs.deriv.com/docs/higherlower). Because the sent value wasn't recognized, Deriv's pricing engine fell through to its barrier-required path and rejected the request — even though the request wasn't attempting to use a barrier at all.

**Fix:** `DerivClient.build_proposal_payload()` now maps `UP` → `CALL` and `DOWN` → `PUT`, and raises `ValueError` on any other direction instead of silently defaulting. Covered by regression tests in `backend/tests/test_deriv.py`.

**Note on naming:** Deriv actually offers two related products that both use `contract_type: CALL/PUT`:
* **Rise/Fall** — no `barrier` field, wins/loses purely against the entry spot (ATM).
* **Higher/Lower** — adds a signed offset `barrier` (e.g. `"+0.37"`), which changes the payout/risk profile.

This bot is named "Higher/Lower" for its plain-English UP/DOWN framing, but after this fix it trades in the barrier-free Rise/Fall style — matching how it was already configured (no barrier setting anywhere in `config.py`) before this fix broke execution. If you actually want the barrier-offset Higher/Lower payout structure, that's a follow-up feature (a configurable `barrier` offset), not something this fix adds silently, since it changes trade economics.

### Other fixes in this pass

* **`BOT_MODE` case/whitespace mismatch could silently select the real account** (`config.py`): startup validation accepted `BOT_MODE` case-insensitively (`"Demo"`, `"DEMO"`, `"demo"` with stray whitespace all passed), but `deriv.py`'s account selection did an exact `== "demo"` check. Anything other than the literal lowercase string `"demo"` fell through to `"real"` — silently, with no error — meaning a perfectly natural env-var typo like `BOT_MODE=Demo` would start up looking completely healthy while actually trading on the **live account**. `bot_mode` is now normalized to lowercase/stripped once at startup, so every consumer sees the same canonical value. This is arguably worse than the `InvalidBarrierSingle` crash above, since a crash is at least loud — this failure mode wasn't.
* **OTP staleness race** (`deriv.py`, `connect()`): the account/OTP lookup now happens immediately before the trade WebSocket connects, instead of before the public WebSocket handshake (which can retry up to 3 times). Deriv's docs note OTPs are short-lived and must be used right after being minted.
* **Crash-loop on reconnect mid-candle** (`engine.py`, `on_exact_candle_open()`): `candle_epoch` is a unique DB column. A reconnect landing back on the same in-progress candle used to attempt a duplicate insert, raise an unhandled `IntegrityError`, and crash the whole engine loop every backoff cycle until the candle closed. It now checks for an existing signal first and reuses it instead.
* **Dashboard mode badge** (`frontend/`): the sidebar badge was hardcoded `DEMO` in `index.html` and never actually reflected `BOT_MODE`. It now renders the real mode from `/api/status` and turns red for `live`, so live (real-money) mode can't be mistaken for demo.
* Replaced hardcoded `180`s in `engine.py` with `self.settings.timeframe_seconds` for consistency.
* Replaced a tautological test (`assert "HIGHER" == ("HIGHER" if "UP" == "UP" else "LOWER")`, which asserted a literal against itself and would never have caught the bug above) with real tests, now split into `test_deriv.py` (contract-type/barrier payload), `test_config.py` (`BOT_MODE` normalization), and `test_strategy.py` (unchanged strategy logic).

## Trading contract semantics

The bot predicts the direction of the next complete 3-minute candle:

* `UP` signal -> stored/shown as `HIGHER` -> sent to Deriv as `contract_type: CALL`
* `DOWN` signal -> stored/shown as `LOWER` -> sent to Deriv as `contract_type: PUT`

`HIGHER`/`LOWER` are this bot's own display labels (database column, dashboard). `CALL`/`PUT` are the actual values sent over the wire — see the fix note above.

The strategy is evaluated from completed candles only. A qualified signal is created on the first observed tick belonging to the new 180-second candle boundary and the proposal is requested immediately. The system contains no manually configured barrier logic and does not require a barrier to trade.

## Preserved behavior

* No automatic maximum daily drawdown stop
* Daily signal tracking
* Daily PnL history
* Win rate
* Pending/open signals and trades
* Dashboard navigation
* Demo/live mode
* PAT authentication
* SQLite by default and PostgreSQL-compatible DATABASE_URL

## Reliability

The Deriv client uses separate public and trade WebSockets, bounded handshake timeouts, retrying handshakes, request timeouts, reader health checks, safe handling of non-text and non-JSON messages, and engine-level exponential reconnect backoff.

Transient settlement errors are retried until the settlement deadline. A technical reconnect does not activate a financial drawdown stop. A reconnect landing back on an already-signaled candle reuses the existing signal instead of crashing the engine loop (see fix notes above).

## Local run

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8080`.

## Testing

```bash
pip install -r backend/requirements.txt pytest
pytest
```

`pytest` itself isn't in `backend/requirements.txt` since it's a dev-only dependency, not needed in the production container.

## Fly.io

Set secrets:

```bash
fly secrets set DERIV_APP_ID="..." DERIV_PAT="..." BOT_MODE="demo" -a crypto-signal-bot-kooj9a
fly deploy -a crypto-signal-bot-kooj9a
```

Use `BOT_MODE=live` only when you intentionally want live account selection. Keep credentials in Fly secrets. Never commit `.env`.

## Runtime

At each 180-second UTC candle boundary:

1. The previous candle is finalized.
2. Completed-candle strategy features are evaluated.
3. A qualified direction is mapped to `CALL` (higher) or `PUT` (lower) — see "Trading contract semantics" above.
4. A barrier-free proposal is requested.
5. The proposal is bought at the configured stake.
6. The contract is polled until settlement.
7. Trade result and PnL are persisted.

## Deployment verification

```bash
curl https://YOUR_APP.fly.dev/health
```

Port `8080` is used consistently by the container, Docker Compose, FastAPI, and Fly.io.

