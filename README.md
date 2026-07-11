# ⚡ Candle Oracle — Deterministic V2 Prediction Market Bot

Rule-based, walk-forward-validated 5-minute and 15-minute candlestick direction
bot for BTC, ETH, SOL, XRP, BNB, DOGE. Signals delivered via Telegram. Executes
on **Limitless Exchange** and optionally **Polymarket** (independent balance),
**in parallel** — every pair that qualifies on either timeframe fires its own
independent order, not a single "best signal" pick.

---

## 📊 How It Works

### Signal Engine — no ML, fully deterministic (`signal_engine.py`)
The ML ensemble (Random Forest + Gradient Boosting, 40 technical indicators,
4-hourly retraining) has been **removed entirely** and replaced with a much
simpler, rule-based engine — every part of it was chosen because it was the
specific thing that survived extensive backtesting and walk-forward
validation (tuned on historical data, tested blind on unseen months), not
because it seemed reasonable in the abstract.

**Method — peek, don't forecast:** decide using the first few minutes of
*realized* price action inside the candle that just opened, resolve against
that same candle's own open/close.
- **5-minute candles:** peek = first 1-minute bar (decide ~1 min in)
- **15-minute candles:** peek = first 3-minute bar (decide ~3 min in)

**Qualification — magnitude only:** a candidate fires if `|early move| >=`
a per-pair, per-timeframe threshold (see `MAG_THRESHOLD` in
`signal_engine.py`). Multi-pair agreement and early-candle volume were both
tested extensively and dropped — neither added measurable win-rate value,
they only cut real signal volume.

**Backtested performance** (walk-forward: tuned on part of the historical
sample, validated blind on a held-out later period):
- 5-min combined (6 pairs): ~30 signals/day, ~87-88% win rate
- 15-min combined (6 pairs): ~47 signals/day, ~83-85% win rate

### Parallel, Not Single-Pick
Every pair that qualifies, on **both** timeframes, fires its own independent
order attempt every tick — this is not a "pick the single best signal
system-wide" design. Multiple pairs (and both timeframes) can hold open
positions at the same time. Each `(symbol, timeframe, venue)` stream is
gated by its own breaker, independently — there is deliberately no shared
global streak counter: pooling many independent streams into one shared
sequential ladder was measured to produce a realistic worst-case loss streak
around **14**, versus **~3-4** for each stream kept independent. See
`models.py` → `PairLadder`.

### The Breaker (per `symbol` + `timeframe` + `venue`)
State lives in the `PairLadder` table — one row per stream, tracking
`consecutive_losses` and `cooldown_until`.
- A **win** resets the counter to 0 and clears any cooldown.
- After **3 consecutive real losses**, the stream pauses. New signals for
  that exact stream are skipped until:
  1. `cooldown_until` has passed (`COOLDOWN_BARS` = 8 native bars of that
     timeframe — 40 min for 5m, 2h for 15m), **and**
  2. the next candidate's magnitude clears `base_threshold × REARM_MULT`
     (1.5×) — a stricter re-entry bar using magnitude, the one feature
     that's actually predictive.
- This does **not** mathematically guarantee a hard cap of 3 in live trading
  — nothing can, without seeing the future. Walk-forward validation showed
  the worst realized streak on any single stream was 4, occurring rarely.

`consecutive_losses` also drives **martingale position sizing** for that
exact stream — the same counter serves both purposes, looked up against
`Settings.martingale_sequence` / `poly_martingale_sequence`.

### Signal Timing
- **GENERATE** fires every minute, checking **both** timeframes every time.
  `signal_engine.get_signal_for_symbol_tf`'s peek-bar boundary-alignment
  check is what actually makes each timeframe "fire" only once per its own
  candle — a single every-minute schedule serves 5m (peek=1min) and 15m
  (peek=3min) at once without needing two separately-offset cron triggers.
- **RESOLVE** fires every 5 minutes at `:00/:05/:10.../:55` — 15-min candle
  closes are a subset of every-5-min boundaries, so one schedule catches
  both timeframes.
- **RECONCILE** fires every 20s, independent of both — re-checks any signal
  that had to fall back to OKX and corrects the record once the venue's own
  (Chainlink-backed) resolution actually lands.
- Generate and resolve are **fully decoupled** (no shared tick, no ordering
  dependency) — a big change from the old single-timeframe design where
  they raced each other on the same `:00/:15/:30/:45` boundary.

### Duplicate-Signal Protection
Two independent checks, not one:
1. Never create a second signal row for the exact same
   `(symbol, timeframe, candle_open_time)`.
2. **Never fire a new signal for a stream while a previous one for that
   exact `(symbol, timeframe)` is still PENDING**, regardless of
   `candle_open_time`. Under normal timing this can't happen — resolve
   always runs before the next candle's peek bar becomes actionable — but
   if resolution is ever delayed (an OKX hiccup, a slow platform response),
   this is what stops a second real order from being placed on top of a
   still-open first one.

### Order Execution — two independent venues, two order types each
| | Limitless | Polymarket |
|---|---|---|
| Role | Primary | Secondary, opt-in |
| Order types | **GTC** (limit) or **FOK** (market) — your choice, independent per venue | Same |
| Contract price cap | ≤ $0.50, **GTC only** | Configurable, default ≤ $0.50, **GTC only** |
| Position sizing | Independent martingale ladder per `(symbol, timeframe)` | Same, fully independent from Limitless's |
| Collateral | On-chain wallet | pUSD (CLOB V2) |

**Order type choice (Settings → Execution Method, per venue):**
- **GTC (limit)** — rests on the book at your price cap until filled or
  cancelled. May not fill at all if the market never reaches your price.
- **FOK (market)** — fills immediately in full at whatever price is
  available, or the whole order is cancelled (no partial fills, never
  rests). **Ignores the price cap entirely** — spends exactly your
  configured position size, prioritizing execution certainty over price.
  The on-chain order struct still requires a `takerAmount` field even for
  FOK (it's not optional at the wire level); this is set to the smallest
  representable unit so it's trivially satisfied at any real execution
  price — that's what "no price floor" means mechanically.
  ⚠️ Limitless's FOK path has not been live-tested against their API from
  this codebase — the request/response for the first few live FOK orders
  is logged in full so the actual fill can be checked by hand against
  Limitless's own order history before trusting it at size. Polymarket's
  FOK behavior is confirmed directly against their own documentation
  (makerAmount/takerAmount apply uniformly across GTC/FOK/GTD).

Both venues can run at once from the same signal, each with its own
credentials, balance, and fill/outcome tracking.

### Order Placement Safety
Retries only happen on a **clean rejection** (the exchange received the
request and explicitly said no — safe to retry, nothing was placed). A
**network timeout or connection error** is treated as genuinely ambiguous —
we don't know whether the server processed the signed order before the
connection dropped, and blindly retrying could place a second real order for
the same intended position. Ambiguous failures stop immediately and log
critically for manual review instead of auto-retrying.

### Outcome Resolution — venue-native first, OKX as fallback only
Each venue resolves its own markets against its own oracle price feed (both
Limitless and Polymarket settle their short-duration crypto markets against
**Chainlink** internally — Limitless via Chainlink Data Streams since
migrating from Pyth). Outcomes are resolved from **the venue's own result**
whenever available; OKX (USDT-quoted, the same reliable feed signal
generation reads) is used only as a fallback for BNB/DOGE specifically (not
on the free Chainlink relay this bot uses) or when a venue hasn't published
its result within the poll window.

Direct, authenticated access to Chainlink Data Streams' own REST/WebSocket
API is a **paid, credentialed product** (API key + secret issued by
Chainlink directly, HMAC-signed requests) — there is no free public way to
query it. `chainlink_feed.py` instead piggybacks on Polymarket's own public
real-time data relay (no auth needed), which covers BTC/ETH/SOL/XRP but not
BNB/DOGE. If real Chainlink Data Streams credentials are ever obtained, this
can be replaced with a direct, authenticated integration covering all six
pairs.

The native-resolution poll budget was widened substantially (from ~4-6
seconds to ~18 seconds per platform) since resolve no longer needs to stay
fast to avoid blocking generate — the old tight budget was the main reason
`resolution_source` showed `OKX_FALLBACK` far more often than necessary.
`job_reconcile_resolutions` remains as a safety net for genuinely slow
outliers, re-checking every 20s and correcting the record (never
retroactively touching a martingale decision already made).

### Live Execution per Pair — per timeframe now, not just per pair
Settings → **Live Execution per Pair** lets you enable/disable live order
placement independently for **each pair's 5m and 15m stream separately**
(e.g. BTC 5m live while BTC 15m stays signal-only) — 12 toggles, not 6. A
disabled stream still generates signals and feeds its per-pair stats; it
just skips placing a real order. Every toggle across the whole Settings page
now **saves immediately** on click — no separate Save step required.

### What Got Removed
Family rotation and directional saturation (features of the old
single-pick-per-candle scheduler design) don't apply to the parallel
architecture — there's no single "last signal" to rotate away from when
every qualifying pair fires independently. The old global 2-loss cooldown
and the old single shared martingale streak are superseded entirely by
`PairLadder`'s independent per-stream accounting. ML retraining is gone —
there's no model to retrain.

---

## 🚀 Deployment

### Option A — Fly.io (`fly.toml` included)
1. `fly launch` (or reuse the existing app name in `fly.toml`)
2. Set secrets: `fly secrets set KEY=value` for each env var below
3. `fly deploy`

### Option B — Render (`render.yaml` included)
1. Push this repo to GitHub
2. [render.com](https://render.com) → New → Web Service → connect the repo → **Apply** (auto-detects `render.yaml`)
3. Add each env var below under the service's **Environment** tab
4. Deploy — first build takes ~3–5 minutes

Either way, the dashboard is served from the deployed app's root URL. The
new `PairLadder` table and the `Signal.timeframe` / `Settings.*_order_type`
columns are created/migrated automatically on next boot — no manual DB
migration step needed.

---

## 🔑 Environment Variables

### Core
| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/botfather) |
| `TELEGRAM_CHAT_ID` | From [@userinfobot](https://t.me/userinfobot) |
| `DEFAULT_MODE` | `shadow` (start here) or `live` |
| `DEFAULT_POSITION_SIZE` | e.g. `10` |
| `DATABASE_URL` | Optional — Postgres connection string; defaults to local SQLite |

### Limitless
| Key | Value |
|-----|-------|
| `LIMITLESS_PRIVATE_KEY` | Wallet private key (`0x...`) — signs orders via EIP-712 |
| `LIMITLESS_SMART_WALLET` | Only if your account uses a smart/proxy wallet whose address differs from the signer above — otherwise leave unset |
| `LIMITLESS_TOKEN_ID` / `LIMITLESS_TOKEN_SECRET` | Optional — pins fixed HMAC credentials; auto-derived if unset |

### Polymarket (optional — enable via the toggle on the Polymarket dashboard page)
| Key | Value |
|-----|-------|
| `POLYMARKET_PRIVATE_KEY` | The **signer's** private key |
| `POLYMARKET_FUNDER_ADDRESS` | The wallet that actually **holds** your funds — your Polymarket profile address at [polymarket.com/settings](https://polymarket.com/settings). Required for anything other than signature type 0. |
| `POLYMARKET_SIGNATURE_TYPE` | `0` EOA · `1` POLY_PROXY (Magic Link **email/Google login — the default, and what most accounts use**) · `2` GNOSIS_SAFE (connected browser wallet) · `3` POLY_1271 (new deposit-wallet accounts) |
| `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_API_PASSPHRASE` | Optional — L2 trading credentials auto-derive on first use and are cached for the process lifetime; set these only to pin a fixed key across restarts |
| `POLYGON_RPC_URL` | Optional but recommended — a dedicated RPC (Alchemy/Infura/QuickNode free tier). Falls back through a short list of public RPCs if unset, but those are frequently rate-limited for automated/bot traffic. |
| `POLYMARKET_PROXY` | Optional — routes every Polymarket API call (and the Chainlink relay WebSocket) through a proxy instead of connecting directly. Format: `HOST:PORT:USER:PASS`, the standard format most datacenter/residential proxy providers (IPRoyal, etc.) hand you directly — paste it in as one string, no need to split it up. Use this if `/api/polymarket/geo-check` shows `blocked: true` for your hosting region, or if a persistent 401 on every authenticated call (including the heartbeat) turns out to be a regional block rather than a credential issue — see `/api/polymarket/status` to check. |

> Signed into Polymarket with email or Google? That's signature type 1 — the
> default. Your **signer** (this private key) and your **funder** (where your
> money actually is) are different addresses in that case; see the Polymarket
> dashboard page for a live credential check.

> ⚠️ **Never commit private keys or secrets to GitHub.** Set them only as
> platform environment variables/secrets.

---

## 🛠 Settings (via Dashboard)

### Global (Settings page)
| Setting | Description |
|---|---|
| Mode | Live / Shadow |
| Position Size | Limitless stake, $1–$1,000 |
| Martingale | On/Off — stake ladder on confirmed losses, per `(symbol, timeframe)` stream |
| Martingale Sequence | Comma-separated stakes per loss step |
| Loss Cap | Consecutive losses before a hard reset to the base stake |
| Max Contract Price | ≤ $0.50 — **applies to GTC orders only**, ignored entirely by FOK |
| Execution Method | **GTC** (limit) or **FOK** (market), independent choice per venue |
| Live Execution per Pair | On/off for real order placement, **independently per pair AND per timeframe** (12 toggles) |
| Stop Loss | Halts new trades once balance reaches a set floor |
| All toggles save immediately on click | No separate "Save" step |

### Limitless page
| Setting | Description |
|---|---|
| Fill Threshold | Minimum % filled to count as a complete trade for the martingale ladder |
| Reset Streak | Manually zero a stream's ladder without waiting for a win or the cap |
| Pending Trade | Live entry/close price + fill status for in-flight signals |
| Connection status | Signer/maker address, auth readiness |

### Polymarket page
| Setting | Description |
|---|---|
| Trade on Polymarket | Master on/off for this venue |
| Position Size / Max Contract Price | Independent of Limitless |
| Polymarket Martingale | Its own independent ladder per `(symbol, timeframe)` stream |
| Fill Threshold | Same concept as Limitless, tracked independently |
| Wallet & Signature Type | Signer/funder addresses, signature type, L2 key status, heartbeat status |
| Balance | pUSD + POL (gas), on-chain, with automatic RPC fallback |

### Dashboard — Active Streaks & Cooldowns panel
Live, per-`(symbol, timeframe, venue)` breakdown of current streak and any
active cooldown — this is the real, current state (`PairLadder`), not the
old frozen global counters.

---

## 📁 File Structure

```
candle-oracle/
├── app.py                  # Flask app + all API routes
├── wsgi.py                 # Gunicorn entrypoint (production)
├── main.py                 # Fallback entrypoint (identical to wsgi.py)
├── extensions.py           # db + socketio instances
├── models.py                # SQLAlchemy DB models (Signal, PairLadder, Settings, ...)
├── signal_engine.py         # Deterministic V2 signal engine — no ML, see "How It Works"
├── limitless_executor.py    # Limitless order placement (GTC/FOK), fill checks, resolution (live + shadow)
├── polymarket_executor.py   # Polymarket CLOB V2 order placement (GTC/FOK), fill checks, resolution (live + shadow)
├── chainlink_feed.py         # Chainlink close-price fetch via Polymarket's public relay (BTC/ETH/SOL/XRP only)
├── telegram_bot.py          # Telegram notifications
├── scheduler.py              # APScheduler jobs: generate (1min), resolve (5min), reconcile (20s), daily summary
├── templates/
│   └── index.html           # Dashboard UI (Dashboard/Signals/History/Shadow/Limitless/Polymarket/Settings)
├── requirements.txt
├── render.yaml
├── fly.toml
├── .env.example
└── .gitignore
```

---

## ⚠️ Important Notes

- **Start in Shadow mode** and run for a while before switching live —
  shadow mode discovers real market slugs and mirrors real venue resolution
  (both Limitless and Polymarket), so it's a genuine preview of live
  performance, not a simulated coin-flip.
- No ML retraining — the deterministic engine has fixed, walk-forward-
  validated thresholds (`signal_engine.MAG_THRESHOLD`). Changing them is a
  code change, not a scheduled background task.
- SQLite works for light usage; for anything serious, set `DATABASE_URL` to
  a Postgres connection string.
- Polymarket's CLOB V2 requires a background heartbeat to keep resting GTC
  orders alive — starts automatically at boot once `POLYMARKET_PRIVATE_KEY`
  is set. FOK orders resolve synchronously and don't depend on it the same
  way, but starting it is harmless either way.
- `/api/debug-resolution?signal_id=<id>` — inspect the raw Limitless/order-
  status response for a specific signal, useful if a fill or outcome ever
  looks wrong.
- `/api/stats/ladders` — live `PairLadder` state for all streams (what the
  dashboard's Active Streaks & Cooldowns panel reads).
- `/api/polymarket/debug` (GET) — walks every layer of the Polymarket
  connection independently (config → geo-check → fresh auth derivation →
  one real heartbeat → unauthenticated Gamma lookup) and reports the raw
  result of each, with a plain-language diagnosis of which layer actually
  failed. Completely safe — places no real order.
- `/api/polymarket/test-trade` (POST, requires `{"confirm": true}` in the
  body) — places one real $1 order to verify the full signing+submission
  pipeline end-to-end, the one thing `/debug` deliberately doesn't do.
  Defaults to a GTC limit priced far from market (won't fill); pass
  `{"confirm": true, "order_type": "FOK"}` to test a market order instead
  (will actually execute). **This uses real funds if the account is in
  live mode — the confirm flag exists specifically so it can't fire by
  accident.**

---

## 📝 Changelog

### v11 — Proxy support for geo-blocked Polymarket regions
- Diagnosed a persistent `401 Unauthorized/Invalid api key` on every
  Polymarket heartbeat (and, by the same mechanism, every authenticated
  call) tracing to Polymarket geo-blocking the hosting region's outbound
  IP — a block that can produce the exact same generic 401 as a genuine
  credential problem, since it can be enforced before the signature is
  even checked. Confirmed via the existing `/api/polymarket/geo-check`
  diagnostic rather than guessed.
- **Added `POLYMARKET_PROXY` support** — routes every Polymarket API call
  (auth, heartbeat, orders, market discovery, resolution polling) and the
  Chainlink relay WebSocket through a proxy instead of connecting
  directly. Accepts the standard `HOST:PORT:USER:PASS` format most
  datacenter/residential proxy providers hand you directly. Fully
  opt-in — unset, everything connects exactly as before.
- `/api/polymarket/geo-check` and `/api/polymarket/status` now report
  `proxy_configured` so it's immediately checkable whether a proxy is
  active, without needing to inspect environment variables directly.

### v10 — Post-deployment fixes: resolution reliability, order safety, market orders
- **Fixed a migration gap**: `Signal.timeframe` was added to the model but
  missed the app's explicit column-migration list — `db.create_all()` only
  creates missing *tables*, never new columns on an existing one, so every
  query against `signals` was failing outright in production
  (`UndefinedColumn`). Added to the migration list alongside every other
  column.
- **Found and fixed the actual root cause of best-dip tracking and
  resolution always falling back to OKX**: Limitless's `place_shadow_order`
  never discovered a market slug (Polymarket's did) — with no slug,
  `job_track_best_dip` had nothing to query and `job_resolve_outcomes`
  skipped straight past checking Limitless's own resolution. Fixed to
  discover the slug synchronously, matching the Polymarket path.
- Fixed a slug-resolution fallback hardcoded to only ever match 15-minute
  markets, silently unable to resolve a slug for any 5-minute signal that
  reached it.
- **Widened the native-resolution poll budget** (~4-6s → ~18s per platform)
  — the old short budget existed only because resolve used to share a tick
  with generate; they're fully decoupled now, so there's no reason to give
  up on Limitless/Polymarket's own Chainlink-backed resolution quickly and
  fall to OKX as often as it was.
- **Fixed a real double-execution risk**: retries previously treated a
  network timeout/connection error the same as a clean rejection, resubmitting
  a brand-new signed order without knowing if the first one actually went
  through. Ambiguous failures now stop immediately and log critically
  instead of auto-retrying.
- **Fixed a real duplicate-order gap**: a new signal for a `(symbol,
  timeframe)` stream could previously fire while a previous one on that
  exact stream was still PENDING, if resolution was ever delayed. Now
  explicitly blocked.
- **Added Market (FOK) order support**, independent choice per venue
  alongside the existing Limit (GTC): verified against each platform's own
  documentation (Polymarket: confirmed; Limitless: structurally sound at the
  wire level, flagged as not yet live-tested). FOK spends exactly the
  configured position size with no price floor, as a true market order.
  Fixed the price cap incorrectly still being applied to FOK on first
  implementation.
- Fixed the Settings toggle for every on/off control (martingale, per-venue
  enable, order type, live-execution-per-pair) not persisting — most
  required a separate manual Save click; two (`use_limitless`/
  `use_polymarket`) didn't persist at all. All toggles now save immediately.
  Root cause of a related "selection keeps snapping back" symptom:
  `Settings.to_dict()` was missing the two new order-type fields entirely,
  so every save's response silently overwrote the just-made selection back
  to the default.
- **Live Execution per Pair is now per-timeframe** (12 toggles instead of
  6) — a pair's 5m and 15m streams can be enabled/disabled independently.
- Fixed a `NameError` (stale variable name after a grouping-logic rename)
  that silently broke the resolve job's websocket push on every run.
- Fixed a global (not per-signal) fill-monitoring throttle that would only
  let one of several simultaneously-pending signals actually get checked
  per cycle — a direct consequence of moving to the parallel architecture
  that a single shared timestamp didn't account for.

### v9 — Deterministic V2 rewrite: no ML, parallel execution, dual timeframe
- **Replaced the entire ML ensemble** (Random Forest + Gradient Boosting, 40
  indicators, 4-hourly retraining, 1H trend filter, quality-vote filter)
  with a deterministic, walk-forward-validated magnitude-threshold engine.
  See "How It Works" above for the full method.
- **Added 5-minute timeframe support end-to-end** — previously 15-minute
  only. Both timeframes run from the same signal engine and scheduler,
  fully independently gated.
- **Rearchitected from single-pick-per-candle to parallel**: every
  qualifying pair on both timeframes fires its own order, not one "best
  signal" system-wide pick. Removed family rotation and directional
  saturation (both specific to the old single-pick design and meaningless
  once every pair fires independently).
- **New `PairLadder` model** — independent martingale streak + breaker
  cooldown per `(symbol, timeframe, venue)`, replacing the old single
  global martingale streak and global 2-loss cooldown. A shared/combined
  streak across all 12 independent streams was measured at a realistic
  worst case of 14 consecutive losses; kept independent, each stream's
  worst case was 3-4.
- Extended both executors' market discovery to accept a `timeframe`
  parameter (previously hardcoded to 15-minute markets only).
- Added `timeframe` filtering to the Signal Log (API + dashboard dropdown +
  a badge on each row).
- Removed the ML retraining job entirely — nothing to retrain.

---

## 🔭 Roadmap / Possible Future Updates

- **Direct Chainlink Data Streams integration** — would require obtaining
  real API credentials from Chainlink (paid, contact-to-access) to replace
  the current free Polymarket-relay workaround, extending Chainlink-sourced
  close prices to BNB/DOGE (currently OKX-only for those two).
- **Live-test Limitless FOK orders** at small size and confirm the actual
  fill behavior matches the wire-level structure this implementation sends.
- **Historical cooldown/streak event log** — the dashboard's Active Streaks
  & Cooldowns panel shows live current state; a proper timestamped history
  of past cooldown trips would need a new events table.
- **Cross-venue martingale unification** — the two ladders (and now 12
  independent per-stream ladders per venue) are fully independent by
  design. A combined, balance-aware sizing model across everything at once
  would be a bigger, separate design decision if ever wanted.
- Postgres-first setup docs (currently SQLite-by-default with Postgres as a
  documented option via `DATABASE_URL`).
