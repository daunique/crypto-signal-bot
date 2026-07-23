# Deriv Higher/Lower Bot

Production-oriented 3-minute Deriv Higher/Lower bot.

## 2026-07-23 (later): real Higher/Lower barrier + settlement tracking fix

Two more issues surfaced once the fix above got trades actually executing:

### Trades were placed as Rise/Fall, not Higher/Lower

The previous fix (below) kept proposals barrier-free, matching what `config.py` had at the time — but that's Deriv's **Rise/Fall** product, not **Higher/Lower** (confirmed on Deriv's own dtrader.deriv.com UI: executed trades showed up as "Rise", not "Higher"). Deriv's Higher/Lower product uses the same `CALL`/`PUT` contract_type but *requires* a signed, relative `barrier` offset (e.g. `"+0.523"`) — see [developers.deriv.com/docs/higherlower](https://developers.deriv.com/docs/higherlower).

**Fix:** a real barrier is now always included. Rather than a fixed guessed point value, it's sized dynamically as a fraction of the recent average candle range (ATR) — `BARRIER_ATR_FRACTION` in `config.py`, default `0.25` — so it stays calibrated to R_25's actual current volatility instead of going stale if the volatility regime shifts. Positive offset for `CALL`/Higher (barrier above entry spot), negative for `PUT`/Lower (barrier below). Larger fraction = harder to win but bigger payout; smaller = odds closer to Rise/Fall. Tune to taste — there's no universally "correct" value, since it's a genuine risk/payout tradeoff.

The exact barrier used is now persisted (`Signal.barrier_offset`, `Trade.barrier`) and shown in the dashboard's Signals/Trades tables and Current Signal panel, so you can audit exactly what was traded while you tune the fraction.

### Settlement tracking was completely broken

Once trades started executing, every single settlement poll (`proposal_open_contract`, used to check whether a trade won or lost) failed with:

```
InputValidationFailed: Input validation failed: subscribe (Not in enum list: 1.)
```

**Root cause:** the request sent `"subscribe": 0`. Per [Deriv's own schema](https://developers.deriv.com/schemas/proposal_open_contract_request.schema.json), `subscribe` is optional but its *only* legal value is the integer `1` — there's no valid "0" for a one-shot check, you simply omit the field. This matches Deriv's own reference implementation. `contract_id` is now also sent as an integer, matching the schema's type (was a string).

**Impact:** trades were being placed correctly but the bot could never learn whether they won or lost — every poll failed, over and over, until the 5-minute settlement deadline gave up silently. Trades would stay stuck at `status: OPEN` with PnL never updating.

### Database migration for existing deployments

`Signal.barrier_offset` and `Trade.barrier` are new columns. `create_all()` only creates brand-new tables, so an already-running deployment's existing database file wouldn't get these columns automatically and would crash on first write. `db.py`'s `init_db()` now checks for each column and adds it if missing (checked, not try/except-and-ignore, since a failed `ALTER TABLE` on Postgres poisons the rest of the transaction). No manual steps needed — this runs automatically on startup.

### Known issue found, not fixed: a vacuous volatility check in the strategy

`strategy.py`'s scoring includes `atr >= 0.8 * avg_range` as a "sufficient volatility" filter, but `atr` and `avg_range` are computed identically over the same 15 candles — they're always exactly equal, so this check is always true and never actually filters anything. It doesn't crash or misfire trades, but it's likely not doing what was intended (probably meant to compare a *short* recent window against a *longer* baseline, to skip trading during unusually quiet periods). Left as-is since fixing it would change which candles actually qualify for a signal — a strategy-behavior change, not a wire-protocol bug — so it wasn't changed without being asked.

## 2026-07-23 fix: every trade was being rejected (`InvalidBarrierSingle`)

Every execution attempt was failing with:

```
RuntimeError: {'code': 'InvalidBarrierSingle', 'details': {'field': 'barrier'},
'message': 'Invalid barrier (Single barrier input is expected).', 'subcode': 'InvalidBarrierSingle'}
```

**Root cause:** `backend/app/deriv.py` sent `"contract_type": "HIGHER"` / `"LOWER"` to Deriv. Those are only this bot's own internal direction labels (used for the database and dashboard) — they were never valid values on the wire. Deriv's actual contract_type values for this trade are `CALL` (higher) and `PUT` (lower); see [developers.deriv.com/docs/higherlower](https://developers.deriv.com/docs/higherlower) and [legacy-docs.deriv.com/docs/higherlower](https://legacy-docs.deriv.com/docs/higherlower). Because the sent value wasn't recognized, Deriv's pricing engine fell through to its barrier-required path and rejected the request — even though the request wasn't attempting to use a barrier at all.

**Fix:** `DerivClient.build_proposal_payload()` now maps `UP` → `CALL` and `DOWN` → `PUT`, and raises `ValueError` on any other direction instead of silently defaulting. Covered by regression tests in `backend/tests/test_deriv.py`. (This payload now also always includes a real barrier — see the section above.)

### Other fixes in this pass

* **`BOT_MODE` case/whitespace mismatch could silently select the real account** (`config.py`): startup validation accepted `BOT_MODE` case-insensitively (`"Demo"`, `"DEMO"`, `"demo"` with stray whitespace all passed), but `deriv.py`'s account selection did an exact `== "demo"` check. Anything other than the literal lowercase string `"demo"` fell through to `"real"` — silently, with no error — meaning a perfectly natural env-var typo like `BOT_MODE=Demo` would start up looking completely healthy while actually trading on the **live account**. `bot_mode` is now normalized to lowercase/stripped once at startup, so every consumer sees the same canonical value. This is arguably worse than the `InvalidBarrierSingle` crash above, since a crash is at least loud — this failure mode wasn't.
* **OTP staleness race** (`deriv.py`, `connect()`): the account/OTP lookup now happens immediately before the trade WebSocket connects, instead of before the public WebSocket handshake (which can retry up to 3 times). Deriv's docs note OTPs are short-lived and must be used right after being minted.
* **Crash-loop on reconnect mid-candle** (`engine.py`, `on_exact_candle_open()`): `candle_epoch` is a unique DB column. A reconnect landing back on the same in-progress candle used to attempt a duplicate insert, raise an unhandled `IntegrityError`, and crash the whole engine loop every backoff cycle until the candle closed. It now checks for an existing signal first and reuses it instead.
* **Dashboard mode badge** (`frontend/`): the sidebar badge was hardcoded `DEMO` in `index.html` and never actually reflected `BOT_MODE`. It now renders the real mode from `/api/status` and turns red for `live`, so live (real-money) mode can't be mistaken for demo.
* Replaced hardcoded `180`s in `engine.py` with `self.settings.timeframe_seconds` for consistency.
* Replaced a tautological test (`assert "HIGHER" == ("HIGHER" if "UP" == "UP" else "LOWER")`, which asserted a literal against itself and would never have caught the bug above) with real tests, now split into `test_deriv.py` (contract-type/barrier payload), `test_config.py` (`BOT_MODE`/barrier-fraction validation), and `test_strategy.py` (strategy logic + `atr` exposure).

## Trading contract semantics

The bot predicts the direction of the next complete 3-minute candle and trades a genuine Higher/Lower contract:

* `UP` signal -> stored/shown as `HIGHER` -> sent to Deriv as `contract_type: CALL` with a positive `barrier` (above entry spot)
* `DOWN` signal -> stored/shown as `LOWER` -> sent to Deriv as `contract_type: PUT` with a negative `barrier` (below entry spot)

`HIGHER`/`LOWER` are this bot's own display labels (database column, dashboard). `CALL`/`PUT` plus the signed `barrier` are the actual values sent over the wire — see the fix notes above. The barrier's distance from spot is `BARRIER_ATR_FRACTION` × the recent average candle range; see `.env.example`.

The strategy is evaluated from completed candles only. A qualified signal is created on the first observed tick belonging to the new 180-second candle boundary and the proposal is requested immediately.

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
2. Completed-candle strategy features are evaluated, including the recent average candle range (ATR).
3. A qualified direction is mapped to `CALL` (higher) or `PUT` (lower) — see "Trading contract semantics" above.
4. A Higher/Lower proposal is requested with a signed barrier sized from the ATR.
5. The proposal is bought at the configured stake.
6. The contract is polled until settlement.
7. Trade result and PnL are persisted.

## Deployment verification

```bash
curl https://YOUR_APP.fly.dev/health
```

Port `8080` is used consistently by the container, Docker Compose, FastAPI, and Fly.io.

