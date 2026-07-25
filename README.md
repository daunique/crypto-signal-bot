# Deriv Higher/Lower Bot

Production-oriented 3-minute Deriv Higher/Lower bot.

## 2026-07-25 (latest): signal/trade history was never actually persisted across deploys

Not a database *connection* problem — the database was always there and working. The real issue: `fly.toml` had no `[[mounts]]`, so the SQLite file at `/app/data/bot.db` lived on the container's local, ephemeral disk. Every `fly deploy` schedules a genuinely new machine, wiping any local-disk-only files with it — so every deploy silently reset signal/trade/event history to empty, which is exactly what looked like "not connected to a database."

**Fix:** `fly.toml` now mounts a persistent Fly Volume at `/app/data`. **The volume has to be created once by hand first** (a config change alone can't create it) — see the "Persistent storage" section under Fly.io below for the exact commands. A real Postgres instance (e.g. Fly's Managed Postgres, which is what the Fly dashboard's "Managed Postgres Attachments" panel — showing "no attached MPG clusters" — refers to) is also already fully supported by just changing `DATABASE_URL`; a volume is simpler and sufficient for this single-machine deployment, so that's the default recommendation here.

## 2026-07-25: dashboard rendered giant/broken and unclickable + signals now trade inverted

### Dashboard was unstyled and unusable after the redesign deployed

Screenshots showed the new nav icons rendering at massive, raw, browser-default size, while dashboard content further down (which happened to share class names like `.card`/`.btn` with the pre-redesign CSS) looked mostly fine. That split is the signature of a **stale cached `styles.css`** being served alongside a fresh `index.html`/`app.js`: brand-new class names introduced only in the redesign (`.appnav`, `.navlink`, `.topbar`, `.hero`, ...) had no matching rules in the old stylesheet, so they rendered completely unstyled; anything reusing an old class name still looked approximately right. This also explains "nothing is clicking" — giant, wrongly-laid-out elements were very likely covering or displacing the real, correctly-wired touch targets, not a JS bug.

**Fix:** `index.html` is no longer served as a flat static file. `GET /` now reads it and rewrites the `styles.css`/`app.js` references to include `?v=<BUILD_VERSION>`, so a browser or intermediate CDN can never serve mismatched assets across a deploy again — a new build version means a genuinely new URL, guaranteed cache miss, every time. If the dashboard still looks broken after this deploys, try a hard refresh once as a one-time fix for whatever was already cached before this change existed.

### Every signal now trades the opposite direction

Requested directly: an "UP" (bullish) reading now places a **LOWER** trade, not a **HIGHER** one, and vice versa. Implemented in `engine.py` rather than `strategy.py`, so the strategy module stays a "pure" indicator (it still just reports what it detects); the engine is where the deliberate decision to trade the opposite lives. `Signal.direction` — and therefore `contract_type`, the barrier's sign, and what actually gets sent to Deriv — is now the **traded** (post-inversion) direction throughout, so the dashboard, the database, and the real trade always agree with each other. The signal's `reason` text is prefixed with `[Inverted from UP/DOWN]` so this is never a silent surprise when reviewing history later.

## 2026-07-24 (latest): barrier simplification, a real stall bug fixed, dashboard redesign, persisted live/demo toggle

### Barrier now always exactly matches what was computed at signal opening

The adaptive multi-candidate barrier retry (added while the `HIGHER`/`LOWER` root cause below was still unconfirmed) is removed now that it's confirmed fixed. `execute()` sends exactly the ATR-derived barrier computed when the signal was created — no substitution. If Deriv ever rejects it, that's now reported honestly (`EXECUTION_ERROR`, visible in diagnostics) instead of silently trying different magnitudes.

### Trades could silently stop executing until a manual restart

Root cause: the public (tick) and trade (authenticated) connections are separate WebSockets. A brief network blip can sever just the trade one while the public one stays alive — the bot kept detecting signals fine, but `execute()`'s own try/except was swallowing the resulting `"Deriv trade WebSocket is not connected"` error into `EXECUTION_ERROR` and returning normally. That meant `tick_loop()` never raised, so `run()`'s reconnect/backoff logic never triggered — every future signal would silently fail the same way forever, until the process was restarted by hand.

**Fix:** added `DerivClient.trade_connected`, checked on every tick (cheap — ticks arrive every 1-2s). The moment the trade connection is found dead, the engine raises and lets the existing reconnect/backoff logic take over, instead of waiting for a trade attempt to expose it.

### Dashboard redesign

Full visual rework of `frontend/` aimed at a professional trading-terminal feel and an actually-usable mobile layout:
* Dark, data-dense theme with a deliberate color system: teal-green for wins/higher, coral-red for losses/lower, amber for pending — every signal/trade status is now a clearly labeled, colored badge (not just plain text), directly addressing "can't differentiate win/loss/pending".
* Responsive nav: a bottom tab bar on narrow/mobile screens, a left sidebar on wide ones — same markup, CSS-driven, rather than two different navs to maintain.
* A live countdown ring for the current 180s candle, tabular/monospace numerals throughout (IBM Plex Mono) so price/stake columns actually align, and a hand-rolled inline-SVG cumulative PnL chart (no chart library dependency).
* Typography: Space Grotesk for headings, Inter for body/UI, IBM Plex Mono for all numeric data.

### Live/demo mode is now a dashboard toggle that persists across restarts

Previously `BOT_MODE` was an env var baked in at deploy time — changing it meant editing Fly secrets and redeploying. Settings now has a Demo/Live segmented toggle:
* A new `RuntimeSetting` key-value table (`db.py`) persists the override; `BOT_MODE` becomes just the fallback default for when no override has ever been saved.
* `POST /api/settings/mode` validates, persists, and — if the bot is currently running — stops and restarts it so the new mode actually takes effect immediately (account selection happens once, at connect time).
* Switching *to* Live asks for confirmation in the UI first (real money); switching to Demo doesn't need one.
* `deriv.py`'s `connect()`/`_select_account()` now take the effective mode as an explicit parameter from `engine.py`, rather than importing the DB-backed resolver directly — keeps `DerivClient` a decoupled, independently-testable Deriv API client with no knowledge of this app's settings-persistence mechanism (this also matters for the test suite, which stubs around `deriv.py` without a real database).
* `Trade.mode` now records the mode actually connected with at execution time, which can differ from today's dashboard setting if it's since been changed.

**Not execution-tested:** the DB-backed mode persistence and stop/restart cycle couldn't be run end-to-end in the sandbox this was built in (no network access to install the real SQLAlchemy/FastAPI stack — see "Testing" below for how the rest of this project's tests are verified without it). Reviewed carefully by hand; please exercise the actual toggle once deployed and report back if anything looks off.

## 2026-07-24 (resolved): `contracts_for` confirms `HIGHER`/`LOWER` was correct all along — the CALL/PUT fix was wrong for this account

The live `contracts_for` response (via the Settings page tooling above) settled this definitively. It lists **two separate contract categories** for R_25:

* `"contract_category": "callput"`, `contract_type: CALL/PUT` — barrier-free ATM Rise/Fall on this account.
* `"contract_category": "higherlower"`, `contract_type: HIGHER/LOWER` — the actual barrier product, with an `"intraday"` duration tier of `min_contract_duration: "15s"` to `max_contract_duration: "1d"` that covers our 180-second duration directly, e.g. `{"barrier": "+0.382", "contract_type": "HIGHER", ...}`.

**This means the earlier `InvalidBarrierSingle` → "fix" to `CALL`/`PUT` (further below) was wrong for this account.** That fix was reasonably made — it matched Deriv's general Higher/Lower docs (`developers.deriv.com/docs/higherlower`, `legacy-docs.deriv.com/docs/higherlower`), which do describe `CALL`/`PUT` for this product — but this account's actual API doesn't follow that. The original code, before any of this session's changes, already had the right `contract_type` (`HIGHER`/`LOWER`); it was only ever missing the barrier field, which is what actually caused `InvalidBarrierSingle`. Switching to `CALL`/`PUT` masked that by accident: `CALL`/`PUT` *without* a barrier is valid (as Rise/Fall), so trades started executing — just not as Higher/Lower. Then when the barrier was added on top of `CALL`/`PUT`, *every* value was rejected as `InvalidBarrier` regardless of magnitude or sign (documented in the two entries below) — because `CALL`/`PUT` was never going to accept a barrier on this account, no matter what value was sent. Magnitude was never the problem.

**Fix:** `DIRECTION_TO_CONTRACT_TYPE` is back to `{"UP": "HIGHER", "DOWN": "LOWER"}`, this time confirmed against this account's own live data rather than general docs. Barrier format/sign convention (signed relative offset, positive above spot for Higher, negative below for Lower) is unchanged and was already correct. The adaptive barrier-retry mechanism (below) stays in place as a narrower safety net, not because the contract shape is still in doubt.

**Takeaway for future debugging on this account:** trust `contracts_for` over Deriv's general documentation when the two disagree — it reflects what this specific account/landing company actually offers, which apparently differs from the docs in at least this one respect.

## 2026-07-24: `contracts_for` request shape corrected from Deriv's own error

The first live `contracts_for` query itself failed schema validation: `Properties not allowed: currency, underlying_symbol` — notably *not* complaining about `contracts_for` itself. That's decisive: unlike `proposal`/`proposal_open_contract`, this endpoint doesn't take a `flag=1` plus a separate `underlying_symbol` field. The symbol is the value of `contracts_for` directly (the old API's shape carried over unchanged): `{"contracts_for": "R_25"}`, no other properties. Fixed and covered by a regression test.

## 2026-07-24: live `contracts_for` diagnostic added — magnitude conclusively ruled out in both directions

A second diagnostics run confirmed the same failure again, this time for the `UP`/`CALL` (positive barrier) side: **all 13 candidates rejected**, `+0.050` through `+5.000`. Combined with the earlier `DOWN`/`PUT` run (all 13 rejected, `-0.050` through `-5.000`), every barrier candidate tried across a 100x range, in *both* directions, has now been rejected. This is conclusive: it is not the sign, and it is not the magnitude. Continuing to guess numbers isn't a productive path forward.

**New: a live `contracts_for` query, added directly to the diagnostics tooling.** `GET /api/diagnostics/contracts-for` has the bot itself ask Deriv what contract types, barriers, and durations are actually valid for this account and symbol right now — this is Deriv's own documented mechanism for exactly this question. It requires the bot to be running (connected first). On the Settings page, "Copy contract specs (live query)" copies the result.

**Deliberately not parsed/summarized:** the exact current response shape for this endpoint couldn't be confidently confirmed against available docs in advance (same reason the barrier limits themselves couldn't be looked up ahead of time) — attempting to pre-parse specific fields here would risk quietly hiding the real answer behind another wrong guess. It returns the raw JSON (or the raw error, if the request shape itself turns out to be wrong) so the actual field names can be read directly. If the request itself is malformed, the returned error is still useful signal about the correct shape.

**Suggested next step:** run the bot, then click "Copy contract specs (live query)" on the Settings page and share the result — that (or the manual DTrader duration/barrier check suggested earlier) is what actually resolves this, rather than further guessing.

## 2026-07-24: diagnostics endpoint + confirmation the issue isn't barrier magnitude

The widened sweep below was tested: **all 13 candidates were rejected**, spanning `-0.050` up to `-5.000` (a 100x range). That's conclusive on one point: barrier *magnitude* is not the (sole) issue — no reasonable min/max range fails uniformly across two full orders of magnitude. Something more structural is going on (account/token permissions for this contract category, or how this symbol/duration is actually offered via `contracts_for` are the leading suspects), and it needs either a `contracts_for` response in hand or a manual DTrader test (see below) to pin down with confidence rather than more guessing.

**New: a diagnostics endpoint, so this doesn't require exporting raw platform logs each time.** `GET /api/diagnostics` returns recent bot events, signals, and trades as JSON. On the dashboard's **Settings** page, "Copy diagnostics to clipboard" fetches it, formats it as readable text, and copies it — paste that directly here instead of a full log export. It deliberately never includes `DERIV_PAT`/`DERIV_APP_ID`/`DATABASE_URL`, since it's designed to be shared.

This also wired up `BotEvent` / `log_event()`, which existed in `db.py`/`engine.py` but were never actually called anywhere before now (dead infrastructure). Execution errors, engine reconnect-loop failures, and settlement polls that give up at the deadline without ever resolving (previously a *silent* failure mode — it just returned with no log line at all) are now all persisted there, which is what the new endpoint reads from.

`BUILD_VERSION` moved from `main.py` to `config.py` so `api.py` could import it for the diagnostics response without creating a `main.py` ↔ `api.py` circular import (`main.py` imports the router from `api.py`).

## 2026-07-23 (newest): all 5 retry candidates were rejected — search widened, still unresolved

The retry logic below worked exactly as designed — the log showed all 5 candidates tried in the right order (`0.560 → 0.280 → 0.140 → 0.100 → 0.050`) — but **every one was rejected** with the same `InvalidBarrier`. That's an important data point: since even 0.05 (very close to spot) failed, "barrier too large" isn't the (whole) explanation, and since 0.560 also failed, it isn't simply "too small" either. The previous retry list only ever tried values *at or below* the original ATR-derived estimate — it never tried anything larger.

**This round's fix:** `_propose_with_barrier_retry()` now sweeps a much wider range — smaller *and* larger than the original estimate (`×0.25` up to `×8`, plus fixed fallbacks from `0.05` up to `5.0`) — and logs the full signed barrier string plus the error's message/details on every rejection, not just the bare offset number, so the next log capture is maximally informative regardless of outcome.

**Honest status:** root cause still isn't confirmed. If this widened sweep also fails across the board, barrier magnitude probably isn't the issue at all, and something else needs to be checked — e.g. whether this specific Deriv account/token has trading permissions for this contract category, or whether R_25 Higher/Lower is actually offered at exactly 180s/duration_unit `s` for this account (as opposed to only being offered in fixed duration steps, or only in minutes) via `contracts_for`.

**A fast way to get ground truth that doesn't depend on me guessing further:** on dtrader.deriv.com, manually open the Higher/Lower ticket for Volatility 25 Index, set Duration to 3 minutes (matching this bot exactly), and see what barrier value/range the ticket itself accepts or defaults to. That's Deriv's own pricing engine for the exact same symbol/duration this bot uses — if the manual ticket also complains about a barrier in the same size range, that's a strong clue; if it accepts something specific, that number is worth trying here directly.

## 2026-07-23 (latest): barrier value rejected (`InvalidBarrier`) — adaptive retry

After the fix below, trades executed as genuine Higher/Lower, but then started failing with a *different* error:

```
RuntimeError: {'code': 'ContractBuyValidationError', 'message': 'Invalid barrier.', 'subcode': 'InvalidBarrier'}
```

This is a different failure than the earlier `InvalidBarrierSingle` — a barrier *is* present now, but its value was rejected. The traceback confirms this happens at the `proposal()` call itself.

**Root cause, honestly stated:** Deriv's documented way to know the valid barrier range for a symbol/duration is the `contracts_for` endpoint, but its precise current response shape (exact field names for min/max barrier limits) couldn't be confidently confirmed against current docs/schemas at the time of this fix, despite substantial research (multiple official Deriv sources were checked — see git history of this file for the trail). Rather than guess a hardcoded "safe" magnitude and risk being wrong again in a different way, the fix makes barrier sizing **self-correcting**:

**Fix:** `deriv.py` now raises a structured `DerivAPIError` (with `.code`/`.subcode`) instead of a generic stringified `RuntimeError`, so calling code can react to specific error types. `engine.py`'s new `_propose_with_barrier_retry()` tries the ATR-derived barrier first, and on a `subcode == "InvalidBarrier"` rejection specifically (any other error still fails immediately, unretried), retries with progressively smaller values, ending with two small fixed fallbacks: `[computed, computed×0.5, computed×0.25, 0.1, 0.05]` (widened further above). Whichever value Deriv actually accepts is what gets stored on the `Trade` record and used for the real trade — this also means future logs will show exactly what worked, turning this from a guessing game into an evidence trail.

**If trades are still failing after this:** check the logs for `"rejected"` / `"Execution failed"` — if every candidate in that list is being rejected, the barrier size isn't the (only) issue and something else needs investigating (e.g. account trading permissions, symbol availability, balance). Please share the new log if that happens.

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

* `UP` signal -> stored/shown as `HIGHER` -> sent to Deriv as `contract_type: HIGHER` with a positive `barrier` (above entry spot)
* `DOWN` signal -> stored/shown as `LOWER` -> sent to Deriv as `contract_type: LOWER` with a negative `barrier` (below entry spot)

The database/dashboard display label and the actual wire value are the same string here (`HIGHER`/`LOWER`) — confirmed against this account's own `contracts_for` response (see the changelog entry above); this is a separate, dedicated contract category from `CALL`/`PUT` (which is the barrier-free Rise/Fall product on this account). The barrier's distance from spot is `BARRIER_ATR_FRACTION` × the recent average candle range; see `.env.example`.

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

### Persistent storage (one-time setup)

`fly.toml` mounts a Fly Volume at `/app/data`, where the SQLite database lives. **The volume itself has to be created once, by hand, before it can be mounted** — a redeploy alone won't create it:

```bash
fly volumes create bot_data --region ams --size 1 -a crypto-signal-bot-kooj9a
fly deploy -a crypto-signal-bot-kooj9a
```

`--size 1` is 1GB, far more than a SQLite file of signals/trades needs. Without this, every redeploy runs on a brand-new machine with an empty local filesystem — the database was never actually broken or disconnected, it just had nowhere durable to live, so every deploy quietly reset it to empty. If you'd rather use a real Postgres instance instead of SQLite-on-a-volume (e.g. Fly Managed Postgres, shown as "Managed Postgres Attachments" in the Fly dashboard), that already works too — this app only needs `DATABASE_URL` pointed at it (`asyncpg` is already in `backend/requirements.txt`); a volume is the simpler option for a single-machine deployment like this one.

## Runtime

At each 180-second UTC candle boundary:

1. The previous candle is finalized.
2. Completed-candle strategy features are evaluated, including the recent average candle range (ATR).
3. A qualified direction is mapped to `HIGHER` or `LOWER` — see "Trading contract semantics" above.
4. A Higher/Lower proposal is requested with a signed barrier sized from the ATR, adaptively retrying with a smaller/fallback barrier if Deriv rejects the value (see changelog above).
5. The proposal is bought at the configured stake.
6. The contract is polled until settlement.
7. Trade result and PnL are persisted.

## Deployment verification

```bash
curl https://YOUR_APP.fly.dev/health
```

Port `8080` is used consistently by the container, Docker Compose, FastAPI, and Fly.io.

