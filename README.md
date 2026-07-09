# ⚡ Candle Oracle — 15m Prediction Market Bot

ML-powered 15-minute candlestick direction bot for BTC, ETH, SOL, XRP, BNB, DOGE.
Signals delivered via Telegram. Executes on **Limitless Exchange** (primary) and
optionally **Polymarket** (secondary, independent balance) simultaneously, with a
martingale staking ladder gated on confirmed, real fills rather than assumed ones.

---

## 📊 How It Works

### Signal Engine
- Fetches 15m OHLCV candles from **OKX Spot** for 6 pairs
- Computes 40 technical indicators (RSI, MACD, Stochastic, BB, ADX, Williams %R, CCI, etc.)
- Runs a **Random Forest + Gradient Boosting ensemble**
- **Anti-spam**: only fires the single best signal per 15-minute candle (highest confidence across all 6 pairs)
- Per-pair confidence thresholds and tiering live in `PAIR_CONFIG` (`signal_engine.py`)

### Signal Timing
- **:00, :15, :30, :45 UTC** — signal evaluated at candle open, orders placed
- **:00, :15, :30, :45 UTC (+0s)** — `job_resolve_outcomes` fires at the exact candle boundary
- **+20s, repeating** — `job_reconcile_resolutions` re-checks anything that had to fall back to OKX and corrects it once the venue's own resolution is actually in (see **Outcome Resolution** below)
- Win/Loss is only counted once a candle has fully closed

### Order Execution — two independent venues
| | Limitless | Polymarket |
|---|---|---|
| Role | Primary | Secondary, opt-in |
| Order type | GTC limit | GTC limit |
| Contract price cap | ≤ $0.50 | configurable, default ≤ $0.50 |
| Position sizing | Martingale ladder | Flat (independent balance) — optional toggle to scale with the same ladder shape |
| Collateral | On-chain wallet | pUSD (CLOB V2) |

Both venues can run at once from the same signal, each with its own credentials,
balance, and fill/outcome tracking (see the **Limitless** and **Polymarket**
dashboard pages).

### Martingale — two fully independent ladders, each gated on confirmed fills
Limitless and Polymarket each run their **own** martingale ladder — own
sequence, own loss cap, own streak (`martingale_streak` / `poly_martingale_streak`).
They are deliberately not coupled: different accounts, different balances,
different liquidity. GTC limit orders can go completely unfilled or only
partially fill if the limit price is tight relative to available liquidity,
so each ladder only advances (on a loss) or resets (on a win) when *that
venue's own trade* is confirmed **filled** above its **Fill Threshold**
(Settings, default 95%) — below that, including a partial fill, the streak
freezes and the same stake fires again next candle rather than silently
mis-tracking the ladder. Every trade's exact fill ratio and dollar amount are
recorded per venue (`fill_ratio`/`poly_fill_ratio`, `filled_usd`/`poly_filled_usd`),
not just a filled/unfilled boolean. Either streak can be manually reset to 0
from its own settings page at any time.

### Outcome Resolution — venue-native, not OKX-only
Each venue resolves its own markets against its own oracle price feed
(both Limitless and Polymarket now settle their short-duration crypto markets
against **Chainlink Data Streams** — Limitless migrated from Pyth to
Chainlink; Polymarket's 15-min crypto markets have used Chainlink from the
start). That can
occasionally differ from OKX's own candle open/close, since they're reading
different feeds on different timing. Outcomes are resolved from **the venue's
own result** (`winningOutcomeIndex` for Limitless) whenever available, with
OKX used only as a fallback and always shown side-by-side (`okx_outcome`) for
comparison. A "Resolve via Limitless" toggle exists to force OKX-only behavior
if you ever want it.

In practice, a venue's resolution can take longer to actually publish than the
few seconds `job_resolve_outcomes` can afford to wait without delaying the next
candle's signal — `job_reconcile_resolutions` runs independently every 20s and
corrects any signal that had to fall back to OKX once the real answer lands,
without touching a martingale decision that's already been made. The dashboard
distinguishes **⏳ fallback** (routine — venue hadn't answered yet, self-corrects
shortly) from a genuine **🔀 confirmed disagreement** (rare — the venue's real
result and OKX's candle actually differ) — these used to show as the same icon,
which is why the fallback case could look alarming even when nothing was wrong.

### Per-Pair Live Execution
Settings → **Live Execution per Pair** lets you enable/disable live order
placement per pair without touching code. A disabled pair still generates
signals and feeds its per-pair stats/ML tuning — it just skips placing a real
order on either venue. Useful for keeping a pair's signal quality under
observation before trusting it with real stake.

### Platform-Native Entry/Close Prices
The Signal Log's `open_price`/`close_price` stay OKX-sourced — that's the
ML signal's own basis. Separately, each venue's own dashboard page shows
**that venue's own** entry and close price, sourced from Chainlink Data
Streams (the oracle both venues actually settle against) rather than OKX —
`limitless_open_price`/`limitless_close_price` and
`poly_open_price`/`poly_close_price`. Limitless's open price comes directly
from the market's own `metadata.openPrice`; close prices for both venues, and
Polymarket's open price, come from a shared Chainlink fetch (`chainlink_feed.py`)
covering BTC/ETH/SOL/XRP — Polymarket doesn't currently offer these 15-min
markets for BNB/DOGE, so those two pairs don't have a Chainlink figure to show here.

### Best-Dip Tracking (both venues)
`best_entry_pct` (Limitless) and `poly_best_entry_pct` (Polymarket) record the
best (lowest) GTC limit price seen on that venue's own orderbook during the
candle, tracked continuously in the background — useful for calibrating how
tight a limit price a pair's liquidity can actually support.

### Continuous Fill Monitoring + Pending Trade Log
Both the Limitless and Polymarket dashboard pages show a live **Pending
Trade** card for whatever signal is currently in flight — its entry/close
price and current fill status on that specific venue. Fill status isn't only
checked once at resolve time: a background job re-checks every ~15s for as
long as a signal is pending, so "is my order filled yet" is visible in real
time rather than only after the candle closes.

### Duplicate-Position Guard (Polymarket)
Before placing a Polymarket order, the bot checks whether a position already
exists for that exact 15-min market (by slug) and refuses to open a second
one if so — a safety net against a scheduler overlap or manual retrigger
resulting in two open positions on the same signal.

### Signal Tiers
| Tier | Condition | Notes |
|------|-----------|-------|
| T1 (High) | ML confidence ≥ threshold + volume spike >1.5× | Rarer, historically higher win rate |
| T2 (Standard) | ML confidence ≥ threshold | Most signals |

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

Either way, the dashboard is served from the deployed app's root URL.

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
| Martingale | On/Off — stake ladder on confirmed losses |
| Martingale Sequence | Comma-separated stakes per loss step |
| Loss Cap | Consecutive losses before a hard reset to the base stake |
| Max Contract Price | ≤ $0.50 |
| Family Rotation | Excludes the last signal's pair family from the next candle |
| Live Execution per Pair | Per-pair on/off for real order placement (signals still generate either way) |
| Stop Loss | Halts new trades once balance reaches a set floor |

### Limitless page
| Setting | Description |
|---|---|
| Resolve via Limitless | Use Limitless's own result as source of truth (default on); off = OKX-only |
| Fill Threshold | Minimum % filled to count as a complete trade for the martingale ladder |
| Reset Streak | Manually zero the Limitless streak without waiting for a win or the cap |
| Pending Trade | Live entry/close price + fill status for whatever signal is currently in flight on Limitless |
| Connection status | Signer/maker address, auth readiness |
| Recent Fill Quality | Today's FILLED / PARTIAL / UNFILLED counts |

### Polymarket page
| Setting | Description |
|---|---|
| Trade on Polymarket | Master on/off for this venue |
| Position Size / Max Contract Price | Independent of Limitless |
| Polymarket Martingale | Its own independent ladder — own sequence, cap, and streak, gated on Polymarket's own confirmed fills/outcomes. Not coupled to Limitless's. |
| Fill Threshold | Same concept as Limitless, tracked independently |
| Reset Streak | Manually zero the Polymarket streak without waiting for a win or the cap |
| Pending Trade | Live entry/close price + fill status for whatever signal is currently in flight on Polymarket |
| Wallet & Signature Type | Signer/funder addresses, signature type, L2 key status, heartbeat status |
| Server Region Check | Confirms this server's own outbound IP isn't geo-blocked by Polymarket — a blocked region and a bad credential can both surface as the same 401 |
| Balance | pUSD + POL (gas), on-chain, with automatic RPC fallback |

---

## 📁 File Structure

```
candle-oracle/
├── app.py                  # Flask app + all API routes
├── wsgi.py                 # Gunicorn entrypoint
├── main.py                  # Fallback entrypoint (identical to wsgi.py)
├── extensions.py           # db + socketio instances
├── models.py                # SQLAlchemy DB models
├── signal_engine.py         # OKX data fetch + ML signal generation
├── limitless_executor.py    # Limitless order placement, fill checks, resolution (live + shadow)
├── polymarket_executor.py   # Polymarket CLOB V2 order placement, fill checks, resolution (live + shadow)
├── chainlink_feed.py         # Shared Chainlink price fetch (both platforms settle against it) — entry/close prices
├── telegram_bot.py          # Telegram notifications
├── scheduler.py              # APScheduler jobs: generate, resolve, reconcile, daily summary, retrain
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

- **Start in Shadow mode** and run for a while before switching live — shadow mode now mirrors real venue resolution (both Limitless and Polymarket), not just a simulated coin-flip, so it's a genuine preview of live performance.
- ML models retrain automatically every 4 hours (02:05, 06:05, 10:05, 14:05, 18:05, 22:05 UTC).
- SQLite works for light usage; for anything serious, set `DATABASE_URL` to a Postgres connection string.
- Polymarket's CLOB V2 requires a background heartbeat to keep resting orders alive — this starts automatically at boot once `POLYMARKET_PRIVATE_KEY` is set; no action needed, but if you ever see orders vanishing shortly after being placed, check the Polymarket page's heartbeat status first.
- `/api/debug-resolution?signal_id=<id>` — inspect the raw Limitless/order-status response for a specific signal, useful if a fill or outcome ever looks wrong.

---

## 📝 Changelog

### v8 — Platform-native pricing, independent Polymarket martingale, best-dip & fill monitoring for both venues
- **Found and fixed a severe price-parsing bug**: Limitless recently migrated its crypto markets from Pyth to Chainlink. Chainlink-resolved markets return `metadata.openPrice` as an 18-decimal-padded integer string (like a wei value) rather than the plain decimal Pyth markets used — parsing it as a plain float (the old code's behavior) would have been wrong by a factor of ~10^18. Now detected by magnitude and parsed correctly regardless of which oracle a given market used.
- Added a shared Chainlink price-fetch utility (`chainlink_feed.py`, via Polymarket's public real-time data socket, no auth needed) — both venues now show their **own** entry/close price (BTC/ETH/SOL/XRP; BNB/DOGE aren't on this feed) instead of Limitless's dashboard defaulting to OKX everywhere.
- **Polymarket now has its own fully independent martingale ladder** — own sequence, own loss cap, own streak, gated on Polymarket's own confirmed fill and Polymarket's own resolved outcome, replacing the old "scale stake with Limitless's ladder" toggle.
- Added a manual **Reset Streak** action for both venues (Settings / each venue's page) — zero the streak on demand without waiting for a win or the loss cap.
- Added Polymarket best-dip tracking (`poly_best_entry_pct`), mirroring Limitless's, using Polymarket's own CLOB orderbook for the specific token held (with a guard against the CLOB's known stale "ghost market" snapshot quirk).
- Added a duplicate-position guard for Polymarket — refuses to open a second position against a market that already has one for the current signal.
- Added continuous fill monitoring (not just a single check at resolve time) and a live **Pending Trade** card on both venue pages, showing that venue's own entry/close price and current fill status for whatever signal is in flight.
- Fixed the Polygon balance check silently failing — a single public RPC (`polygon-rpc.com`) is frequently rate-limited for bot traffic; now tries a short fallback list, and `POLYGON_RPC_URL` is documented as recommended (not just optional) for production.
- Updated pair confidence thresholds: ETH 0.65, SOL 0.67, XRP 0.70, DOGE 0.67 (BTC/BNB unchanged).
- Confirmed both venues resolve these markets against Chainlink Data Streams (not Pyth) — comments and docs updated accordingly.

### v7 — Geo-restriction and stale-credential diagnostics
- Diagnosed persistent Polymarket 401 "Unauthorized/Invalid api key" errors as **Germany being fully blocked by Polymarket** (frontend and API both), confirmed via Polymarket's own help center — not a code/credential bug. Render's available regions (Oregon/Ohio/Virginia/Frankfurt/Singapore) have no viable option for Polymarket's international exchange; Fly.io's Stockholm region does not appear on any restriction list.
- Added a **Server Region Check** (Polymarket page + `/api/polymarket/geo-check`) that asks Polymarket directly whether the server's own outbound IP is currently blocked, rather than relying on secondhand country lists.
- Added `l2_source` visibility (env-var-pinned vs. auto-derived this run) to the Polymarket credential status — a credential pinned via `POLYMARKET_API_KEY`/`_SECRET`/`_PASSPHRASE` that was derived while on a blocked host stays stale after moving hosts, since pinned env vars always take priority over re-deriving a fresh one.

### v6 — Resolution reconciliation, SOL live, per-pair execution toggle
- **Fixed a false-positive "OKX/Limitless disagree" indicator** that was showing on effectively every signal. Root cause: `job_resolve_outcomes` only waits a few seconds for a venue's own resolution before falling back to OKX (it has to stay quick, or it risks delaying the next candle's signal generation) — in production, Limitless routinely took longer than that window, so it was falling back to OKX almost every time, not because the two sources actually disagreed.
- Added `job_reconcile_resolutions` — a new job running every 20s, independent of the candle boundary, that re-checks recently-fallen-back signals on a much more relaxed budget and corrects the record (for both Limitless and Polymarket) once the venue's real answer is in. Deliberately does not retroactively touch the martingale streak.
- Widened a thread-join timeout that was actually tighter than the polling work it was waiting on, which could cut off a check that was about to succeed.
- Dashboard and Telegram now show **⏳ fallback** (routine, self-corrects) separately from a genuine **🔀 confirmed disagreement** (rare) instead of one icon for both.
- Re-enabled SOL-USDT for live execution (previously signal-only by default).
- Added a **Live Execution per Pair** toggle to Settings — enable/disable any pair's real order placement without a code change or redeploy.

### v5 — Polymarket CLOB V2 migration, dashboard restructuring, Signal Log filters
- Full migration to Polymarket's CLOB V2 (breaking, no V1 compatibility): new order struct (dropped `nonce`/`feeRateBps`/`taker`, added `timestamp`/`metadata`/`builder`), new EIP-712 domain, new CTF Exchange V2 / Neg Risk CTF Exchange V2 contract addresses, new pUSD collateral token.
- Fixed L1 auth to use the correct EIP-712 `ClobAuth` typed-data signature (was signing a plain timestamp string).
- Fixed auth header names (`POLY_ADDRESS` etc. — underscores, not hyphens).
- Added universal signature-type support: `0` EOA, `1` POLY_PROXY (Magic Link email/Google login), `2` GNOSIS_SAFE, `3` POLY_1271, each with an independent funder address from the signer.
- Fixed market discovery — was querying a static, non-existent slug on the wrong API host; now uses the Gamma API with the correct dynamic per-window slug.
- Added the mandatory heartbeat keepalive V2 requires (resting orders were being silently auto-cancelled without one).
- Fixed the order-status endpoint (wrong path, wrong status values) and added precise partial-fill detection.
- Added full Polymarket data parity with Limitless: independent fill-ratio tracking, venue-native outcome resolution, optional martingale-scaling toggle.
- Fixed the Limitless order-execution retry delay (`discover_slug` was sleeping 30s between attempts instead of the intended 2s — could stall order placement up to 2 minutes right at the moment speed matters most).
- Restructured the dashboard: dedicated Limitless and Polymarket pages/nav sections instead of one shared settings card.
- Visual refresh: venue-specific accent colors, tabular-numeral monospace for prices/countdowns, deeper background palette.
- Signal Log: calendar From/To date-range filtering (capped at today), and pair-combination selection (tap any combination of pairs to see combined win/loss/win-rate for exactly that set, computed server-side across all matching signals).

### v4 — Martingale fill-tracking and outcome-resolution overhaul
- Replaced fuzzy trade-history matching with the exact order-ID lookup for fill checking, enabling real partial-fill detection (`fill_ratio`, `filled_usd`) instead of a bare filled/unfilled boolean.
- Added FILLED / PARTIAL / UNFILLED classification with a configurable fill threshold; only a complete fill (above threshold) advances or resets the martingale streak — a partial or zero fill freezes it.
- Fixed a shadow-mode bug where every simulated trade was treated as "unfilled," permanently freezing the martingale streak in shadow mode.
- Fixed `limitless_fill` never updating at all when martingale was switched off.
- Outcome resolution now uses Limitless's own `winningOutcomeIndex` (Chainlink-fed) as the source of truth instead of OKX candles alone, with OKX kept as a fallback and for comparison.
- Fixed SOL-USDT's outcome being scored against the wrong side — the pair's invert flag (bot buys the opposite of the raw signal) wasn't being accounted for in resolution, only in order placement.
- Set SOL-USDT's invert flag to `False` (trades the same direction as its signal, like every other pair).

---

## 🔭 Roadmap / Possible Future Updates

- **Polymarket resolution timing** — the fast-cycling crypto markets appear to resolve automatically and reasonably quickly, but this hasn't been independently confirmed the way Limitless's Chainlink-based resolution was (Polymarket's general-purpose UMA resolution path can take on the order of hours for other market types). The reconciliation job makes this self-correcting either way, but tightening the confidence here would let the fast path rely on it less.
- **WebSocket-based resolution/fill push** instead of polling, for both venues, if latency ever needs to drop further than polling can reasonably deliver.
- **Cross-venue martingale unification** — the two ladders are now fully independent by design (see v8). A combined model (e.g. balance-aware sizing across both venues at once) would be a bigger, separate design decision if ever wanted.
- **Retroactive stats correction** — `job_reconcile_resolutions` corrects a signal's own stored outcome on a genuine disagreement, but doesn't currently re-derive daily aggregate stats or shadow balance for that historical day. Rare enough in practice that it hasn't been a priority, but worth revisiting if it turns out to matter.
- Postgres-first setup docs (currently SQLite-by-default with Postgres as a documented option via `DATABASE_URL`).
