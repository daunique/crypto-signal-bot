# PolyBot — Polymarket Arbitrage Bot

A real-time two-sided market making / arbitrage bot for Polymarket
crypto Up/Down markets.

**Assets traded:** BTC, ETH, XRP, SOL, BNB, DOGE
**Durations:** 5-minute and 15-minute markets
**Total pairs:** 12 (6 assets × 2 durations)

## Strategy

1. Monitors all 12 crypto markets via WebSocket (real-time, millisecond)
2. Calculates fair YES/NO probability internally
3. Buys YES + NO when combined cost is below threshold (guaranteed profit)
4. Manages unhedged exposure if only one leg fills
5. Holds directional positions when the model finds a strong mispricing

## Project Structure

```
polymarket-bot/
├── core/
│   ├── market_discovery.py     # Finds new markets every 10s (Gamma API)
│   ├── websocket_listener.py   # Real-time price feed + trade trigger
│   ├── order_executor.py       # FOK/IOC order placement (CLOB)
│   ├── capital_manager.py      # FIXED + AUTONOMOUS capital modes
│   ├── position_manager.py     # Tracks hits and hedge state
│   ├── expiry_guard.py         # 10-second cutoff, tiered edge rules
│   ├── simulation_engine.py    # No-wallet-needed profit simulation
│   └── simulation_listener.py  # Read-only WebSocket listener for simulation
├── risk/
│   ├── circuit_breaker.py      # Kill switches
│   └── fee_calculator.py       # Exact Polymarket fee math
├── data/
│   ├── trade_journal.py        # SQLite trade logging
│   ├── portfolio_tracker.py    # PnL aggregation for dashboard
│   └── runtime_settings.py     # Live-toggleable settings (5min/15min mode)
├── dashboard/
│   ├── app.py                  # Flask API (live + simulation modes)
│   ├── static/
│   │   ├── css/style.css       # Terminal design system (ink/mint/coral)
│   │   └── js/app.js           # Tab nav, live data, gauge rendering
│   └── templates/
│       ├── index.html          # 4-section mobile app shell (live mode)
│       └── simulation.html     # Simplified dashboard for simulation mode
├── scheduler.py                # Background jobs (wallet sync, health checks)
├── main.py                     # Live trading entry point — starts everything
├── run_simulation.py           # No-wallet-needed simulation entry point
├── preflight.py                # Pre-launch credential/balance diagnostic
├── config.py                   # ALL settings — edit this first
├── requirements.txt
├── .env.example                # Copy to .env and fill in your keys
├── .gitignore
├── Dockerfile                   # Fly.io / container deployment
├── .dockerignore
└── fly.toml                     # Fly.io app + persistent volume config
```

## Deployment: Fly.io (Alternative to VPS)

Fly.io deploys this bot as a container instead of a process on a VPS you SSH into. Two things matter specifically for this bot's needs, both already handled in the config files:

- **Persistent volume required.** `fly.toml` mounts a volume at `/app/data`, where `trades.db`, `runtime_settings.json` (your 5min/15min toggle state), and the derived API key debug file all live. Without this, every redeploy or restart silently wipes your entire trade history and settings — Fly's container filesystem is otherwise ephemeral.
- **Always-on, not auto-sleep.** `fly.toml` sets `auto_stop_machines = false` and `min_machines_running = 1`. Fly's default behavior for web apps stops idle machines to save cost — fine for a website, but this bot needs to keep reacting to live WebSocket price events regardless of whether anyone's looking at the dashboard.

### Fly.io setup steps

```bash
# 1. Install flyctl (one-time, on your local machine or Fly's web shell)
curl -L https://fly.io/install.sh | sh

# 2. Log in
fly auth login

# 3. From the polymarket-bot/ directory, launch (do NOT deploy yet —
#    this creates the app and fly.toml scaffolding, but we already
#    have a customized fly.toml, so answer "no" if it asks to
#    overwrite it)
fly launch --no-deploy

# 4. Edit fly.toml: change `app = "your-polybot-app-name"` to
#    something globally unique, since Fly app names are shared
#    across all users.

# 5. Create the persistent volume (must match fly.toml's
#    [[mounts]] source name exactly: "polybot_data", and must be
#    created in the SAME region as primary_region in fly.toml, "lhr")
fly volumes create polybot_data --size 1 --region lhr

# 6. VERIFY the volume actually exists before deploying — do not
#    skip this.
fly volumes list
#    You should see a row with Name "polybot_data", State "created",
#    Region "lhr". If the list is empty or doesn't show it, re-run
#    step 5 — check you're targeting the right app with `fly apps list`
#    and, if needed, add `-a your-actual-app-name` to the volumes
#    create command to be explicit about which app it attaches to.

# 7. Set your secrets — NEVER put these in fly.toml or commit them.
#    This is the Fly equivalent of your .env file:
fly secrets set PRIVATE_KEY="your_wallet_private_key_here"
fly secrets set FUNDER_ADDRESS="your_proxy_or_funder_address_here"
fly secrets set WALLET_SIGNATURE_TYPE="1"

# 8. Deploy WITH --ha=false — THIS FLAG IS REQUIRED, NOT OPTIONAL.
#    Fly's default behavior for any app with an [http_service]
#    section is to provision 2 machines for high availability. This
#    bot must run as exactly ONE instance — two independent bot
#    processes would trade against the SAME wallet and write to the
#    SAME SQLite database with no coordination between them, causing
#    duplicate trades and corrupted capital tracking. --ha=false
#    tells Fly to only ever create/run 1 machine for this app.
fly deploy --ha=false

# 9. Watch logs to confirm it started correctly
fly logs

# 10. Open your dashboard (Fly gives you a URL like
#     https://your-polybot-app-name.fly.dev)
fly open
```

Since secrets are set via `fly secrets set` rather than a `.env` file inside the container, `config.py`'s `os.getenv(...)` calls read them identically either way — no code changes needed between VPS and Fly.io deployment.

### Troubleshooting: "requires an unattached 'polybot_data' volume"

This error has **two different possible causes** — check them in this order:

**Cause 1 (most likely): you deployed without `--ha=false`.** Fly's default behavior provisions 2 machines for any app with an `[http_service]` section, and each machine needs its own attached volume with the same name. If only one `polybot_data` volume exists — which is all you actually want, since this bot must only ever run as a single instance — the second machine's creation fails with exactly this error, even though a volume genuinely exists and step 5 above succeeded correctly. This is why step 8 above includes `--ha=false`: it tells Fly to only ever create 1 machine, matching the 1 volume you created. If you deployed with plain `fly deploy` (no flag) and hit this error, this is almost certainly why — re-run as `fly deploy --ha=false`.

**Cause 2 (less likely, but check if Cause 1 doesn't resolve it): the volume genuinely wasn't created, or was created against the wrong app/region.**

```bash
fly apps list                              # confirm your app's exact name
fly volumes list -a your-actual-app-name   # confirm whether a volume actually exists
fly volumes create polybot_data --size 1 --region lhr -a your-actual-app-name
fly volumes list -a your-actual-app-name   # confirm it now shows up
fly deploy --ha=false
```

If a volume with a different name already exists (e.g. from an earlier `fly launch` that auto-created one with a name like `your_app_name_data`), either rename `fly.toml`'s `[[mounts]] source` to match the existing volume, or delete the unused one with `fly volumes destroy <volume-id>` and create `polybot_data` fresh — don't leave two volumes for the same app, since only one will actually get mounted.

**Run `preflight.py` inside the deployed container before trusting it with funds**, the same as you would on a VPS:

```bash
fly ssh console
python3 preflight.py
```

## Setup — Step by Step (From Your Phone, VPS Alternative)

### 1. Get a VPS

- Go to **digitalocean.com** or **vultr.com**
- Choose region: **London** or **Amsterdam** (low latency to Polymarket CLOB)
- OS: **Ubuntu 22.04**
- Plan: $6/month (1GB RAM) is enough to start
- You'll receive an IP address and root password by email

### 2. Push This Code to GitHub

- Create a **private** repository on github.com called `polymarket-bot`
- Upload all these files (everything except `.env` — only `.env.example`)

### 3. Connect to Your VPS

Install **Termius** (free SSH app, iOS/Android), then:

```bash
ssh root@YOUR_VPS_IP
```

### 4. Server Setup

```bash
apt update && apt upgrade -y
apt install python3 python3-pip python3-venv git nodejs npm -y

git clone https://github.com/YOUR_USERNAME/polymarket-bot.git
cd polymarket-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 5. Configure Your Secrets

```bash
cp .env.example .env
nano .env
```

Fill in `PRIVATE_KEY`, set `WALLET_SIGNATURE_TYPE` to match how you actually use Polymarket (see the table below), and fill in `FUNDER_ADDRESS` if your type requires it. Save (Ctrl+X, Y, Enter). The `.env.example` file has detailed inline comments for every field — it's designed so you only need to fill in the section matching your account type.

### 6. Review Settings

Open `config.py` and confirm:
- `CAPITAL_MODE` — "FIXED" or "AUTONOMOUS"
- `STARTING_BALANCE` — your actual USDC balance
- `UNIT_SIZE` — dollars per side per pair (FIXED mode)
- `ACTIVE_PAIRS` — which markets to trade

### 7. Run With PM2 (Keeps It Alive 24/7)

```bash
npm install -g pm2

pm2 start "venv/bin/python3 main.py" --name polybot
pm2 startup
pm2 save
```

### 8. View Your Dashboard

On your phone browser:
```
http://YOUR_VPS_IP:5000
```

### 9. Monitor Logs

```bash
pm2 logs polybot      # Live logs
pm2 status            # Check it's running
pm2 restart polybot   # Restart if needed
```

## CLOB V2 Migration (Critical — Read First)

Polymarket migrated its entire trading infrastructure to **CLOB V2** on 2026-04-28. This bot is built against V2. If you're comparing this code to older Polymarket bot tutorials online, expect differences:

- SDK package is `py-clob-client-v2` (imports as `py_clob_client_v2`), not the archived `py-clob-client`
- Collateral token is **pUSD**, not USDC.e — if your balance reads $0 despite holding USDC.e, you likely need to wrap it (see Pre-Flight Checklist)
- Every order now requires `tick_size` and `neg_risk` parameters, fetched per-market and cached by this bot automatically
- The order nonce system was removed entirely in V2

If you're on `WALLET_SIGNATURE_TYPE = 3` (the newer deposit-wallet/POLY_1271 flow), be aware the Python V2 SDK has a known open bug as of June 2026 where L1 auth binds to your EOA instead of the deposit wallet, causing order rejections. Use `signature_type = 2` instead until Polymarket ships a fix — `preflight.py` will warn you about this automatically.

## Execution Latency (Important — Sets Realistic Expectations)

Earlier documentation in this project described "millisecond" execution. After auditing against the real SDK, here's the honest picture:

| Stage | Time |
|---|---|
| WebSocket price event arrival | 2-8ms (network, London VPS) |
| Opportunity detection / math | <1ms |
| **Order signing (EIP-712, client-side)** | **~1 second per order** |
| Order POST to CLOB | 10-50ms |
| **Total per leg** | **~1-1.1 seconds** |
| **Total two-leg trade (YES + NO)** | **~2-3 seconds** |

The ~1 second signing cost is a characteristic of the `py_clob_client_v2` SDK itself (EIP-712 cryptographic signing in Python), not a bug in this bot, and not fixable by better networking. It's why `CUTOFF_SECONDS = 10` matters — at 2-3 seconds for a full two-leg trade, a 10-second buffer before market close is reasonable, but pushing it much lower risks a trade not completing before resolution.

If you need sub-second execution at scale later, the practical path is Polymarket's official Rust SDK, which doesn't carry this signing overhead — that would be a separate, larger rewrite.

## API Credentials — You Never Manually Generate These

There is no `API_KEY` field in `.env`, and that's intentional. Polymarket's L1/L2 auth flow works by **deriving** credentials from a signature made with `PRIVATE_KEY`, not by you creating a key through any dashboard:

```
PRIVATE_KEY (yours, in .env)
        ↓ signs a message
create_or_derive_api_key() called against Polymarket's server
        ↓
API key + secret + passphrase returned, DERIVED from your signature
```

This happens automatically on every bot startup (`core/order_executor.py`), and it's called without a nonce argument, which means it uses the default nonce (0) — **deterministic**, so restarting the bot re-derives the exact same credentials every time rather than creating new ones. Nothing to lose track of, nothing to rotate manually under normal operation.

For visibility, the derived API key (never the secret) is:
- Printed in `preflight.py`'s output
- Mirrored to `data/derived_api_creds.json` (gitignored, key only, no secret) — useful if you ever need to reference it in a support ticket or GitHub issue

**⚠ If you're on `WALLET_SIGNATURE_TYPE=1` (POLY_PROXY / email-Magic accounts): never generate an API key through polymarket.com's website Settings page.** This is a confirmed upstream bug — website-generated keys for these accounts get registered under your *proxy* wallet address, but CLOB V2 validates orders by your *signer* (EOA) address, and the two can never match. Every order fails with a 401 regardless of how correctly everything else is configured. Always let the bot derive credentials itself, which is the default and only path this codebase uses.

## Simulation Mode — No Wallet Needed

If you want to see what a fixed starting balance would earn before funding a real wallet, use `run_simulation.py` instead of `main.py`. This requires no `.env`, no `PRIVATE_KEY`, no Polymarket account at all — it uses only public, read-only data (Gamma API market discovery, the CLOB's public WebSocket price feed) and never constructs an `OrderExecutor` or sends anything to Polymarket's order endpoints.

```bash
python3 run_simulation.py --balance 50
```

A dashboard is available at the same port as the live bot (`http://YOUR_HOST:5000`), showing simulated balance, return, win rate, and recent simulated trades in real time. Disable it with `--no-dashboard` for terminal-only output.

**Why this exists instead of just running `main.py` with an empty wallet:** authentication only checks that your credentials sign correctly — it does not check your balance. Running `main.py` with $0 in your wallet would authenticate fine, discover real opportunities, and then have every single order attempt rejected by Polymarket's server for insufficient funds — burning through `MAX_CONSECUTIVE_MISSES` almost immediately and halting, all while generating real (if pointless) load against Polymarket's live order infrastructure. `run_simulation.py` is the correct, safe tool for this question.

**What this can and cannot tell you:**
- It reuses the exact same edge-detection, fee, and expiry-stage logic as live trading (`risk/fee_calculator.py`, `core/expiry_guard.py`), fed by the same real-time price data — not synthetic or historical data.
- It assumes every simulated order fills at the observed price. Real execution can fail to fill (thin books, latency, a stale quote) — this cannot model that, so its output is a **best case**, not a promise of what live trading would actually produce.
- It reflects only the period you let it run. A few hours is a weak sample — this bot's own trade journal duration-comparison data (see Changelog) showed real day-to-day variance even from an active, established wallet. Let it run longer, ideally across multiple days and different times, before treating any number as a real expectation.

## Pre-Flight Checklist (Run Before Going Live)

A `preflight.py` script is included specifically to verify your real credentials work before any money moves. On your VPS, after setting up `.env`:

```bash
source venv/bin/activate
python3 preflight.py
```

This checks, in order:
1. `.env` values are actually filled in (not placeholders)
2. CLOB authentication succeeds with your `WALLET_SIGNATURE_TYPE`
3. Your wallet balance is visible and non-zero
4. USDC allowance is set (critical for raw EOA/MetaMask wallets — Magic/email and browser-proxy wallets usually have this automatically)
5. Market discovery can reach Gamma API and match your configured pairs

**Do not run `main.py` with real funds until `preflight.py` shows all green.**

### Picking your `WALLET_SIGNATURE_TYPE`

This is the single most common cause of "authenticated but every order fails." The `.env` file is universal — one `WALLET_SIGNATURE_TYPE` switch determines which other fields actually get used, so you never need a different `.env` structure per account type.

| Type | Name | You log into Polymarket with... | `FUNDER_ADDRESS` needed? |
|---|---|---|---|
| `0` | EOA | A raw wallet trading directly, no Polymarket account UI involved (uncommon) | No — leave blank |
| `1` | POLY_PROXY | Email or Google (Magic Link) | **Yes** |
| `2` | POLY_GNOSIS_SAFE | Browser wallet connected via Polymarket's "Connect Wallet" (MetaMask, Coinbase Wallet, Rabby) | **Yes** |
| `3` | POLY_1271 | Newer "deposit wallet" flow (accounts created after ~April 2026) | **Yes** — but see warning below |

If you're unsure, `1` is correct for the large majority of users. Getting this wrong produces either an outright auth failure or orders that silently fail with `"not enough balance / allowance"` even when funded.

**⚠ Type 3 (POLY_1271) is not recommended right now.** As of June 2026 there's a confirmed, still-open bug in `py_clob_client_v2` (upstream issues [#70](https://github.com/Polymarket/py-clob-client-v2/issues/70) and [#75](https://github.com/Polymarket/py-clob-client-v2/issues/75)) where L1 authentication always binds the API key to your EOA instead of your deposit wallet — every order gets rejected with `"the order signer address has to be the address of the API KEY"`, regardless of correct setup. Two independent developers confirmed this on real funded accounts. If your account uses this flow, use `WALLET_SIGNATURE_TYPE = 2` instead as a working alternative until Polymarket ships a fix. `preflight.py` warns about this automatically if you have type 3 set.

For types 1/2/3, `FUNDER_ADDRESS` is your Polymarket proxy/Safe/deposit wallet address — found on your **Polymarket profile page**, not in your browser wallet extension. This is a different address than your signing key's own address.

### If you're on signature_type=0 (EOA)

You must set on-chain token allowances once before trading — this is separate from funding your wallet. Without it, every order fails with `not enough balance / allowance` regardless of your pUSD balance. `preflight.py` step 4 will catch this and tell you explicitly.

### Known SDK behaviors to expect

- FOK orders can be rejected on very thin order books if the requested size can't fill completely at that instant — this is expected behavior, not a bug, and the bot's circuit breaker tracks these as "misses."
- Order share sizes are rounded to 2 decimal places to satisfy CLOB precision requirements on FOK/FAK order types.
- Every order requires `tick_size` (and `neg_risk`) for the specific market — this bot fetches and caches these automatically per token, pre-warmed at market discovery time rather than at trade time (see Changelog).
- `GET /balance-allowance` is rate-limited to 200 requests/10s — the wallet sync job (every 60s) and allowance checks stay well under this.
- `POST /order` sustained limit was raised to 200/s (120,000 per 10 minutes) as of June 1, 2026 — far more than this bot's trade volume will approach.
- **Auth is still unsettled for some account types as of mid-June 2026.** Multiple open GitHub issues (as recent as June 16, 2026) report `signature_type` 0, 2, and 3 all being rejected with different 400 errors for accounts that pre-date the V2 migration (older Safe/proxy wallets). If `preflight.py` shows authentication failing and you've triple-checked your `WALLET_SIGNATURE_TYPE` and `FUNDER_ADDRESS`, this may be an account-side issue on Polymarket's end, not a misconfiguration on yours — check `github.com/Polymarket/py-clob-client-v2/issues` for your specific error message before assuming the bot is at fault.
- If you're in the United States, United Kingdom, France, Germany, Italy, the Netherlands, Belgium, or several other jurisdictions, Polymarket geo-blocks order placement — you can still read market data, but trading will fail regardless of credentials. `preflight.py` cannot detect this; if authentication succeeds but every order silently fails, check whether your region is restricted.

## Important Notes

- **Start small.** Test with `UNIT_SIZE = 1.0` and watch real results before scaling.
- **No testnet exists.** All trading happens on Polygon mainnet with real USDC.
- **The kill switch is real.** If it halts, check `pm2 logs polybot` for the reason before resuming.
- **Never commit `.env`** — it contains your private key.

## Edge Thresholds (config.py)

| Stage | Time Remaining | Max Combined Cost |
|---|---|---|
| ACTIVE | >30s | $0.95 |
| CAUTIOUS | 10–30s | $0.94 |
| FINAL | 10s (last window) | $0.93 |
| CLOSED | <10s | No trading |

Minimum net profit target: **$0.03–$0.05 per $1 traded**, after all fees.

## Capital Allocation (FIXED mode example)

With `UNIT_SIZE = 1.0` and 12 active pairs:
- $1 YES + $1 NO per pair = $2 per pair
- 12 pairs × $2 = **$24 total deployed**
- Idle capital from slow-filling pairs can be borrowed by faster-filling pairs (see `capital_manager.py`)

## Dashboard

The dashboard is a 4-section mobile-first app with a bottom tab bar (not a hamburger — built for one-handed thumb navigation while monitoring live trades):

- **Overview** — wallet balance, today's/total PnL, win rate, capital deployment bar, system health
- **Markets** — every active market as a card with a live YES|NO combined-cost gauge (visually shows the edge gap against the $1.00 threshold), filterable by 5min/15min
- **Trades** — color-coded trade log (mint left-border = profit, coral = loss)
- **Settings** — read-only view of current config.py values (mode, thresholds, risk limits, tracked assets)

Design system: ink-navy base (`#0B0E14`), mint (`#00E5A0`) for YES/profit, coral (`#FF5C7A`) for NO/loss, periwinkle (`#7B8CFF`) accent. Space Grotesk for headings, IBM Plex Mono for all numeric data (tabular alignment), Inter for body text. Fully responsive — scales from phone to desktop, with the tab bar floating as a pill on larger screens.

No rebuild needed to see UI changes — edit `dashboard/static/css/style.css` or `dashboard/static/js/app.js` directly and refresh the browser.

## 5-Min vs 15-Min Toggle (Live Testing Workflow)

Settings has a live toggle — 5 MIN / BOTH / 15 MIN — for choosing which market duration is actually being **traded** right now. It's live: changing it from the dashboard takes effect immediately, no restart needed, and it persists across restarts via `data/runtime_settings.json`.

The important design point: **discovery and edge-detection always run on both durations regardless of the toggle.** When a duration is toggled off, any real edge it finds gets logged as an "observed opportunity" — same numbers you'd have gotten if it had actually traded, just not executed. This means Settings also shows a side-by-side **5min vs 15min comparison panel** with real trades, real profit, and win rate for whichever duration is live, plus the observed-only potential for whichever is off. "Combined potential" (real + observed profit) is the fairest number for deciding which one to scale into, since it isn't biased by which side happened to be toggled on longer.

Workflow this enables: run BOTH for a while to get a baseline, or toggle to one duration for a focused test, then check the comparison panel — it tells you which duration would have performed better even during the time it wasn't live.

## Changelog

**2026-07-05 (correction — the actual root cause of the volume deploy failure)**

The previous changelog entry below ("Fly.io volume deployment failure fix") diagnosed this error as a missing volume-creation step. That fix was necessary but incomplete — the SAME error recurred on a second deploy attempt even after following those steps, which pointed to a different, more fundamental cause.

- **Root cause identified: Fly's default behavior provisions 2 machines for any app with an `[http_service]` section, and each one needs its own attached volume with the same name.** With only 1 `polybot_data` volume created (correctly, by the earlier fix), the second machine's creation fails with the exact same "requires an unattached volume" error — even though the volume genuinely exists and is correctly attached to the first machine. Confirmed via Fly's own documentation for their metrics autoscaler feature, which explicitly instructs using `--ha=false` because "the autoscaler only works on a single Machine."
- **This isn't just a deployment quirk — running 2 machines would be actively unsafe for this specific bot.** Two independent bot processes would trade against the same wallet and write to the same SQLite `trades.db` with zero coordination between them: duplicate trades, conflicting capital-allocation decisions, and a corrupted view of what's actually been traded. This system was built as a single-instance design from the start (in-process asyncio state, no distributed locking anywhere) and was never intended to run as more than one instance.
- **Fixed by making `--ha=false` a required part of the deploy command** (`fly deploy --ha=false`), not an optional flag mentioned in passing. Updated `fly.toml`'s inline comments and the README's Troubleshooting section to lead with this as the most likely cause, with the original volume-creation checks demoted to a secondary check.
- This is a direct instance of a broader lesson worth naming: the first fix addressed a real, legitimate error condition (the volume genuinely might not have existed), but treating an error message's most literal interpretation as the full explanation — without checking why creating that exact resource still wouldn't have been sufficient — meant the same failure recurred. The correction here came only after the user reported the identical error a second time, which is the signal that should have prompted checking Fly's multi-machine defaults immediately rather than re-asserting the same volume-creation diagnosis.

## Changelog

**2026-07-05 (later — Fly.io volume deployment failure fix)**

- **Fixed a real gap in the Fly.io deployment instructions that caused an actual failed deploy**: `fly deploy` correctly rejected the deployment with `"creating a new machine in group 'app' requires an unattached 'polybot_data' volume"` — the volume referenced in `fly.toml`'s `[[mounts]]` block hadn't actually been created before deploying was attempted. The original instructions had the create-volume step in the right order, but nothing verified it had actually succeeded before moving on to `fly deploy`, so a silent failure at that step (wrong app targeted, a transient CLI issue, `fly launch` not having fully registered the app yet) would only surface much later as a confusing deploy-time error.
- Added an explicit verification step (`fly volumes list`) between volume creation and deployment, so a failure is caught immediately with a clear next action, not discovered later at deploy time.
- Added a dedicated **Troubleshooting** section under the Fly.io deployment instructions covering this exact error message, including how to confirm which app a volume is attached to and what to do if a differently-named volume already exists from an earlier `fly launch` (a common cause: `fly launch` can auto-create its own default-named volume before you've customized `fly.toml`, leaving two volumes competing for the same mount).
- Added an inline comment directly in `fly.toml`'s `[[mounts]]` block pointing to the fix, since that's the file someone is most likely already looking at when this error occurs.



**2026-07-05 (Fly.io deployment + no-wallet simulation mode)**

- **Added Fly.io deployment support**: `Dockerfile`, `fly.toml`, `.dockerignore`. Two things specific to this bot's needs, both handled in the config rather than left as gotchas: a persistent volume mounted at `/app/data` (trade history and the 5min/15min toggle state would otherwise be silently wiped on every redeploy, since container filesystems are ephemeral by default), and `auto_stop_machines = false` / `min_machines_running = 1` (Fly's default behavior stops idle machines to save cost, which is fine for a website but would silently pause this bot's real-time price reactions whenever nobody's looking at the dashboard).
- **Added `run_simulation.py` — a genuinely separate, no-wallet-needed way to answer "what would $X have earned?"** Checked precisely what happens if you instead ran the live bot (`main.py`) with an unfunded wallet: authentication only checks that credentials sign correctly, not balance, so it would start up looking completely normal, discover real opportunities, and then have every single order attempt rejected by Polymarket's server for insufficient funds — exhausting `MAX_CONSECUTIVE_MISSES` almost immediately and halting, while generating real (if pointless) load against Polymarket's live infrastructure for no useful signal back. `run_simulation.py` is the correct tool instead: it never constructs an `OrderExecutor`, never reads `PRIVATE_KEY`, and structurally cannot place a real order — verified via test that its listener object has no `executor` or `capital` attribute at all, not just that it happens to behave correctly.
- New modules: `core/simulation_engine.py` (reuses the real fee calculator and expiry-stage logic, tracks a simulated balance) and `core/simulation_listener.py` (a minimal WebSocket listener that reuses only the real price-parsing logic, with zero execution machinery wired in). New dashboard template (`simulation.html`) and endpoints (`/api/simulation/summary`, `/api/simulation/trades`) added to `dashboard/app.py` via new optional parameters — verified the existing live dashboard (as called by `main.py`) is completely unaffected by this change.
- **Found and fixed a real bug during testing, before shipping it**: the simulation engine's settlement call was passing `net_profit + fees` instead of `net_profit` to the balance-update function — silently re-adding fees that had already been subtracted, overstating every single simulated trade's profit by exactly its fee amount. Caught by a test asserting the exact expected balance delta, not by inspection. Confirmed fixed with the same test, then re-verified across a 200-trade simulated sequence for internal consistency (balance always exactly equals starting balance + sum of all recorded net profits).
- **Important honest finding while validating the simulation with synthetic test data**: feeding it independently-random YES/NO prices produced an absurd 323% return, because real market prices are correlated (YES + NO naturally cluster near $1.00) while independent random draws land below $1.00 far more often than reality would. Switching to correlated synthetic pricing produced a far more plausible ~1% return over the same number of ticks. This isn't a bug in the simulator — it's a reminder that the simulator's numbers are only as trustworthy as the real market data feeding it, and confirms why `run_simulation.py`'s output should be judged against real live runs, never assumed reliable from short or synthetic test periods.



**2026-07-04 (two data-justified additions from real trade history analysis)**

Both additions below came from analyzing ~28,965 real trade entries (1.5 days) from an active Polymarket wallet, matched against REDEEM/MERGE resolution events to calculate real, verifiable per-market profit/loss — not estimates. Full methodology: markets grouped by conditionId, total spent (TRADE entries) compared against total received (REDEEM + MERGE), aggregated by timeframe and pair. See conversation history for the complete analysis.

- **Timeframe-weighted capital allocation** (`config.py` → `TIMEFRAME_WEIGHT`, `core/capital_manager.py` → `_timeframe_multiplier()`). The data showed 5-minute markets returning 10.69% at a 50.5% win rate (many small losses offset by fewer larger wins) versus 15-minute markets returning 8.27% at a 62.9% win rate (steadier, less variance) — a real, quantified difference in risk shape between durations, not just noise. Rather than the existing DURATION_MODE toggle's binary either/or (still useful for testing), this lets capital be weighted between durations when both are live — tilt toward 15-min's steadiness, 5-min's higher return, or the safe default even split. Weights are relative and normalized against their own average, so the default `{5MIN: 1.0, 15MIN: 1.0}` produces exactly zero behavior change — verified numerically that only a deliberate tilt away from equal weights has any effect. Applies to both FIXED and AUTONOMOUS capital modes.
- **Same-side re-entry streak cap** (`config.py` → `MAX_SAME_SIDE_STREAK`, `core/position_manager.py` → `can_hold_directional()`/streak tracking, `core/websocket_listener.py` → `_handle_one_leg()`). A specific real market in the data showed five consecutive same-side ("Up") directional holds at falling prices (0.40→0.43→0.31→0.31→0.21) with no intervening hedge, losing $6.52 on that single market — a nameable failure pattern of doubling down on a losing side rather than recognizing it and cutting. Added a same-side streak counter to `MarketPosition` that resets on either a genuine hedge (both sides balanced via `record_hit`) or a side switch, and a cap (`MAX_SAME_SIDE_STREAK = 3`) that forces `_handle_one_leg` to cut the position via sell-back instead of holding again once reached — verified with the exact real price sequence from the data, confirming the 4th and 5th holds (which caused the real loss) are now correctly blocked.
- **Fixed a real bug found while testing the streak cap**: `add_directional()` silently no-op'd if called before any prior `can_hit()`/`record_hit()` call had registered a `MarketPosition` entry for that pair — in the live trading flow this never actually happens (`can_hit()` always runs first in `_check_opportunity()` and registers the entry as a side effect), but relying on that ordering implicitly was fragile. Fixed by making `add_directional()` self-sufficient via the same `_get_or_create()` pattern every other method in the class already uses correctly.
- All of the above verified with dedicated unit tests plus a full end-to-end integration test through the real `_handle_one_leg` code path (not just isolated method calls), and the complete prior-session regression suite re-run to confirm neither addition affected the existing two-sided hedge guarantee or capital-reset behavior.



**2026-07-02 (later — startup safety + dashboard filter/halt fixes)**

- **`main.py` now hard-stops if authentication fails at startup**, instead of silently continuing to initialize the entire pipeline (discovery, WebSocket, scheduler, dashboard). Previously, a failed auth would let the bot look fully "live" on the dashboard — detecting edges, printing `[EDGE]` logs — while every actual trade attempt failed via the executor's internal `auth_ok` guard, only surfacing once `MAX_CONSECUTIVE_MISSES` was hit. `preflight.py` already catches this ahead of time, but `main.py` shouldn't depend on someone remembering to run it first, or on credentials staying valid in between.
- **Fixed a real dashboard bug: the 5-min/15-min market filter was silently wrong.** `id.includes('5MIN')` incorrectly matched `BTC_15MIN` too, since `"5MIN"` is literally a substring of `"15MIN"` — tapping the "5 min" filter showed both durations mixed together, undermining the entire point of comparing them separately. Fixed with an exact suffix match (`id.endsWith('_5MIN')`) and verified with a direct test that the two filters now produce zero overlap.
- Fixed the same substring fragility in the per-card duration label (`id.includes('15')`) — no wrong output with the current asset list, but fragile by luck rather than design. Now uses the same precise suffix check.
- **Made `CircuitBreaker.resume()` actually reachable.** It existed since an earlier session but had no way to call it short of a full bot restart, which also discards the halt reason and unnecessarily reinitializes every other component. Added `GET /api/circuit-breaker/status` and `POST /api/circuit-breaker/resume` endpoints, plus a dashboard panel that appears only when actually halted, showing the specific halt reason and a Resume button. Verified the full flow — halt, reason surfaced correctly, resume clears it, status reflects the change — through Flask's real test client, not just unit-level reasoning.
- `/api/health` now genuinely reflects WebSocket connection state instead of unconditionally returning `"ok"`.



**2026-07-02 (position reconciliation was querying the wrong API entirely)**

- **Found and fixed a real, previously-silent bug: position reconciliation was always a no-op.** `scheduler.reconcile_positions()` called `executor.get_open_positions()`, which queried the CLOB's order-book endpoints (`get_orders(OpenOrderParams())`) — but that endpoint only returns *resting limit orders* (GTC/GTD types). This bot exclusively places FOK and FAK orders, both of which either fill immediately or get cancelled and never rest on the book, so this call would always return an empty list regardless of how many positions were actually held. The reconciliation job has been running every 10 minutes since it was first built, silently checking nothing.
- **Confirmed via research** (cross-referencing multiple current sources including NautilusTrader's own Polymarket integration and the `polymarket-apis` package) that actual held positions live on a separate service — the Data API (`data-api.polymarket.com/positions`) — not the CLOB API at all.
- **Rebuilt position fetching correctly**: `order_executor.py`'s `get_open_positions()` replaced with `get_data_api_positions()`, which queries the Data API directly by wallet address and returns actual filled share holdings.
- **Built real comparison logic** in `scheduler.reconcile_positions()`: aggregates on-chain holdings by `pair_id` (via discovery's existing token→market index), compares against `PositionManager`'s internal tracking, and flags genuine drift — both "internal thinks we hold more/less than we do" and "on-chain shows a position internal has zero record of." Logs mismatches to the circuit breaker log for visibility.
- **Added a dust tolerance** (0.05 shares) after confirming via research that Polymarket's own protocol introduces small rounding drift on fills (the CLOB rounds matched fills to integer cent ticks; the SDK truncates taker amounts to pUSD's on-chain decimal scale) — comparing for exact equality would have produced constant false alarms.
- Verified all of the above with four explicit test scenarios: matching state stays quiet, a genuine large divergence is caught and logged, small protocol-level dust is correctly ignored, and an orphaned on-chain position with no internal record at all is caught.
- Added `Config.DATA_API` alongside the existing `GAMMA_API`/`CLOB_API` constants, and stored `positions_address` on `OrderExecutor` (reusing `FUNDER_ADDRESS` for this purpose across all signature types, including type 0/EOA, to avoid adding a fragile private-key-to-address derivation dependency).



**2026-07-01 (API credential visibility + type-1 website-key bug)**

- **Confirmed the existing auto-derivation approach is correct and safe** — `create_or_derive_api_key()` with no nonce argument is deterministic (default nonce 0), so re-deriving on every bot startup always produces the same credentials rather than creating new ones each time. No change needed to the underlying flow, but added explicit documentation of *why* this is safe, since it wasn't previously explained.
- **Added credential visibility** — the derived API key (never the secret/passphrase) is now surfaced in `preflight.py`'s output and mirrored to `data/derived_api_creds.json` (gitignored) for reference when debugging or filing support tickets. Verified via test that the secret is never written to this file under any code path, and that a failure to write it never breaks actual authentication.
- **Documented a confirmed upstream bug specific to `WALLET_SIGNATURE_TYPE=1` (POLY_PROXY / email-Magic accounts): API keys generated through polymarket.com's website Settings page get registered under the proxy wallet address, but CLOB V2 validates by signer (EOA) address, so the two can never match and every order returns 401 regardless of correct setup.** This codebase was already unaffected (it only ever uses programmatic derivation, never website-generated keys), but this wasn't documented anywhere, so a user manually generating a key through the website — a reasonable thing to try — could have silently broken a correctly-configured setup. Added explicit warnings in `.env.example`, the module docstring in `core/order_executor.py`, and a new README section.
- Added a new **"API Credentials"** README section explaining the full derive-don't-generate flow in one place, since this wasn't previously documented at all and is a common point of confusion coming from other trading bot tutorials that assume a manually-generated key.



**2026-07-01 (universal .env — all signature types in one file)**

- **Rebuilt `.env.example` to genuinely support all four signature types from a single file.** Previously `WALLET_ADDRESS` was a single generic field regardless of account type, which was imprecise: for type 0 it isn't used at all, and for types 1/2/3 it's specifically the *funder* (proxy/Safe/deposit wallet) address — a different address than your signing key's own. Renamed to `FUNDER_ADDRESS` throughout (`config.py`, `core/order_executor.py`, `preflight.py`, `main.py`) with inline documentation of exactly what it means per type.
- **`OrderExecutor` now only passes `funder` to the SDK when it's actually needed.** Previously always passed the field (even as an empty string for type 0 EOA users, where the SDK doesn't use it). Now conditionally includes `funder` in the L2 client construction only for types 1/2/3, and warns explicitly if a required `FUNDER_ADDRESS` is missing for those types — verified via 5 test scenarios covering all four signature types plus the blank-funder warning case.
- **`preflight.py`'s config check is now signature-type-aware.** Previously flagged a blank wallet address as an error regardless of type; now correctly treats a blank `FUNDER_ADDRESS` as fine for type 0 and as a real failure for types 1/2/3.
- **Verified and corrected signature type names against current upstream sources** (Polymarket's own docs, NautilusTrader's integration reference, and the Rust V2 client) to resolve inconsistent naming found across older community docs. Precise names: `0`=EOA, `1`=POLY_PROXY, `2`=POLY_GNOSIS_SAFE, `3`=POLY_1271. README and all in-code comments updated to match.
- **Added type 3 (POLY_1271) to the README signature-type table** — previously only documented in scattered warnings, not the main reference table. Confirmed via two independent upstream bug reports (issues #70 and #75, both May 2026) that this flow is currently broken in the SDK for all new-style deposit-wallet accounts, not just an edge case — recommends type 2 as a working alternative.



**2026-07-01 (deep diagnostic pass — capital reset, dead safety tier, latency optimization)**

Full fresh line-by-line read of every core file, each fix proven with an integration test rather than reasoned about in isolation.

- **CRITICAL — FIXED-mode capital was never actually reset between market cycles.** `capital_manager.reset_pair()` existed but was dead code — nothing called it. Since a pair's `pair_id` (e.g. `"BTC_5MIN"`) is reused every single 5-minute cycle while the underlying market itself is brand new each time, a pair's $1-per-side allocation was being treated as one continuous pool across ALL cycles forever, not reset per cycle. Confirmed via simulation: a pair's own capital was effectively exhausted by its 3rd trade, after which it became permanently dependent on borrowing from other pairs — even though each new 5-minute market should have started with a completely fresh budget. Fixed by wiring `capital.reset_pair()` into `finalize_market()`, which already runs automatically when a cycle's grace period passes. Proven via a 3-cycle regression test showing the bot now trades successfully on every single cycle with fresh capital each time, not just the first one.
- **Fixed a related capital-leak bug introduced while fixing the above.** An initial fix attempt made FIXED-mode borrowing immediately reserve capital from source pairs — but `get_size()` runs before we know if the trade will actually fill, and FOK orders commonly fail (thin books). Immediate reservation meant a failed trade would permanently deduct borrowed capital from the source pair with no way to give it back. Redesigned as a proper two-phase commit: borrows are recorded as *pending* at size-check time, only committed to the source pair on confirmed successful fill (`record_fill`), and discarded with zero trace on any failure path (`unlock`). Proven via 4 separate test scenarios: reset restores full allocation, successful borrows commit correctly, failed borrows leave the source pair completely untouched, and concurrent borrow attempts from different pairs don't double-count the same idle capital.
- **Fixed a dead safety tier.** `FINAL_SECONDS` and `CUTOFF_SECONDS` were both set to `10`, which made the `FINAL` expiry stage mathematically unreachable (`elif remaining > FINAL_SECONDS` and `elif remaining > CUTOFF_SECONDS` used identical thresholds, so the condition `10 < remaining <= 10` can never be true). This meant `MIN_EDGE_FINAL` — the tightest, most conservative edge requirement, meant specifically for the riskiest final window before a market closes — was silent dead code, and trades in the last 30 seconds all used the same `CAUTIOUS` threshold regardless of how close to expiry they actually were. Fixed by giving `FINAL_SECONDS` a genuinely distinct value (20s) between `CUTOFF_SECONDS` (10s) and `CAUTIOUS_SECONDS` (30s). Verified all four stages (ACTIVE/CAUTIOUS/FINAL/CLOSED) are now independently reachable with correctly tiered edge thresholds.
- **Fixed a stale lookup causing blank slugs in the trade journal.** Two places in `websocket_listener.py` looked up a market's slug via `active_markets.get(pair_id, {})`, left over from before `active_markets` was re-keyed by unique slug (not `pair_id`) in an earlier session. This meant every logged trade and observed-opportunity record had a blank `slug` field. Fixed by routing through `discovery.get_current_market_for_pair()`, the existing safe helper — also removes duplicated lookup logic that caused this exact class of bug once already.
- **Latency optimization: pre-warm tick_size cache at discovery time, not trade time.** Every order requires a `tick_size` lookup; previously this was fetched lazily on the first trade attempt for each new market, adding a ~15-50ms network round-trip before order signing could even begin — directly competing with the already-tight margin in the newly-fixed FINAL stage (10-20s before expiry). Now pre-fetched concurrently for both YES and NO tokens the moment a new market is discovered (every 10s, with no time pressure), so trade-time execution never pays this cost. Falls back safely to the existing lazy-fetch-with-default behavior if pre-warming didn't happen for any reason.



**2026-06-30 (5min/15min live toggle + comparison stats)**

- Added a live-toggleable Settings control (5 MIN / BOTH / 15 MIN) for choosing which market duration is actively traded, without restarting the bot. Backed by `data/runtime_settings.py`, persisted to `data/runtime_settings.json`.
- Discovery and edge-detection now always run on BOTH durations regardless of the toggle — a toggled-off duration's real edges get logged to a new `observed_opportunities` table instead of executed, so no comparison data is ever lost by testing one side at a time.
- Added `TradeJournal.get_duration_comparison()` and a dashboard comparison panel showing real trades/profit/win-rate plus observed-only potential, side by side for 5min vs 15min — this is the actual decision tool for "which one should I scale into."
- New API endpoints: `GET/POST /api/settings/duration-mode`, `GET /api/duration-comparison`.
- **Found and fixed a real bug while testing this feature**: the toggle gate was checked *after* `position_manager.can_hit()`, which has a side effect of creating a position entry on first check regardless of outcome. This meant a toggled-off duration was still leaving empty phantom position entries behind. Fixed by moving the toggle check earlier, before any position-manager state is touched — confirmed via integration test that a toggled-off pair now creates zero position-manager side effects.
- `.gitignore` updated to exclude `data/runtime_settings.json` (live operational state, not code — committing it would overwrite your VPS's actual toggle choice on every deploy) and the WAL/SHM sidecar files created by the earlier WAL-mode fix.



**2026-06-30 (deep diagnostic pass — structural fixes)**

This was a full line-by-line re-read of every file with an integration test built to prove the fixes, not just isolated math traces. Found and fixed six real issues, several serious.

- **CRITICAL — market discovery keying bug.** Markets were keyed by `pair_id` (e.g. `"BTC_5MIN"`), but a new market for the same pair opens every single 5 or 15 minute cycle with a different slug and different token IDs. Discovering the *next* cycle's market was silently overwriting the *current* cycle's still-open entry in `active_markets`, which meant: (1) the bot could no longer look up an in-flight position's market by token ID, silently losing the ability to manage it, and (2) the outgoing cycle's WebSocket subscription was never cleaned up, leaking subscriptions over time. Fixed by keying `active_markets` by unique `slug` instead, with `pair_id` kept as a secondary "current cycle" pointer (`get_current_market_for_pair`) for callers that want "the active BTC_5MIN market" specifically. Proven fixed with a simulated rollover test.
- **`resolve_market()` was dead code.** The position manager had a method to finalize a market cycle's hit count, guaranteed profit, and unhedged exposure — but nothing ever called it. This meant `PositionManager.positions` grew forever across every cycle (a slow memory leak) and per-cycle summaries were never actually recorded. Wired it in via a new `WebSocketListener.finalize_market()`, called automatically by `market_discovery._cleanup_expired()` when a cycle's grace period passes.
- **Capital unit-mismatch bug reintroduced during the previous sizing fix.** `record_fill()` was being called with a dollar cost passed as both the "shares" and "cost" arguments, and separately, `PairAllocation.yes_filled`/`no_filled` (which are dollar-denominated budgets, e.g. "$1 per side") were being incremented by share counts instead of dollar costs — comparing incompatible units in the idle-capital calculation. Both fixed; unit consistency verified numerically.
- **Global unhedged exposure was never checked.** `position_manager.get_total_unhedged()` existed but nothing read it. Per-market unhedged caps (`MAX_UNHEDGED_EXPOSURE`) don't prevent several different markets from each sitting near their own cap simultaneously. Added `MAX_TOTAL_UNHEDGED_EXPOSURE` (config.py) and wired it into `CircuitBreaker.check_all()`, checked on every 60s wallet sync.
- **Dashboard "Capital Deployed" was permanently stuck at $0.** `PortfolioTracker.update_deployed()` existed but was never called from anywhere. Wired `capital.get_status()` into the scheduler's wallet-sync job so the dashboard now reflects real deployed capital.
- **SQLite concurrency risk.** The trade journal's single connection is shared between the asyncio trading loop (continuous writes) and the Flask dashboard thread (concurrent reads), which can produce "database is locked" errors under load with SQLite's default journal mode. Enabled WAL mode plus a busy-timeout, which is specifically designed for this single-writer/multiple-reader pattern.
- Fixed a stale startup log message that only mentioned "BTC/ETH 5min + 15min markets" — now lists all 12 configured pairs dynamically.
- Also fixed while in this area: `get_market_by_token()` was an O(n) scan across every open market on every single WebSocket price event; it's now O(1) via a token→slug index, which matters since this runs on every price tick across up to 12 simultaneous markets.



**2026-06-30 (stress-test pass — critical sizing bug fixed)**

This was a deliberate stress-test of the existing strategy logic, separate from the SDK/auth audit. It found one critical bug and re-verified currency of earlier findings.

- **CRITICAL FIX — share-count mismatch broke the guaranteed hedge.** The original execution logic spent the *same dollar amount* on both the YES and NO leg (e.g. $1 of YES, $1 of NO). Because YES and NO trade at different prices, spending equal dollars buys **unequal share counts** (e.g. $1 at $0.47 = 2.13 YES shares, but $1 at $0.48 = 2.08 NO shares). This silently violated the project's own non-negotiable rule that YES shares must always equal NO shares, and meant trades were not actually fully hedged — the position carried unintended directional exposure that the accounting didn't detect. Fixed by computing a single share count up front (`shares = dollar_budget / combined_cost`) and using that identical share count for both legs. `order_executor.py`, `fee_calculator.py`, `capital_manager.py`, and `position_manager.py` were all updated to size by shares consistently rather than mixing dollar and share units across function boundaries.
- Re-verified rate limits are current: `POST /order` was raised to 200/s sustained (120,000/10min) as of June 1, 2026 — earlier README numbers were already stale and have been corrected.
- Re-verified auth stability: found multiple **still-open** GitHub issues (as recent as June 16, 2026) where accounts migrated from V1 (older Safe/proxy wallets) get rejected across every `signature_type` value. This is broader than the earlier-documented `signature_type=3` bug. `preflight.py` and the README now both flag this as a possible Polymarket-side account issue, not necessarily a misconfiguration, if auth fails after careful setup.



**2026-06-30 (production audit — CLOB V2 rewrite)**

This was a full re-audit against the live SDK and Polymarket API, not incremental patching. Major finding: Polymarket migrated to CLOB V2 on 2026-04-28, and the SDK this bot was previously built on (`py-clob-client`, V1) is archived and no longer functions against production at all. Everything below reflects fixing that plus a full pass for correctness and performance.

- **Rewrote `order_executor.py` entirely for `py-clob-client-v2`** — new import paths, `create_and_post_order()` API shape, `Side.BUY`/`Side.SELL` enums, two-step L1/L2 auth flow
- Collateral is now **pUSD**, not USDC.e — `get_wallet_balance()` and `check_allowance()` updated accordingly, with wrap-instructions surfaced in pre-flight output
- Added `tick_size` / `neg_risk` fetching and caching — every V2 order requires these per-market parameters; previously omitted entirely, which causes silent rejections or precision errors
- Threaded `condition_id` through the full call chain (`market_discovery.py` → `websocket_listener.py` → `order_executor.py`) since `get_market()` requires it, not a token_id
- Fixed a real blocking-call bug: `MarketDiscovery._fetch_active_markets()` was a synchronous `requests.get()` running directly inside the asyncio event loop — every 10-second discovery cycle was freezing the WebSocket listener. Now runs via `asyncio.to_thread()`.
- Added retry-with-backoff (3 attempts) to Gamma API fetches so a single dropped request doesn't stall a discovery cycle
- WebSocket reconnection now uses exponential backoff (capped at 30s) instead of a flat 2-second retry, resetting to immediate-retry on successful reconnection — avoids hammering Polymarket's servers during sustained outages
- Documented the real execution latency: order signing in `py_clob_client_v2` takes ~1 second per order (confirmed SDK characteristic), meaning a full two-leg trade is realistically ~2-3 seconds, not milliseconds as earlier documentation implied. Added an "Execution Latency" section to this README and a note in `config.py` near `CUTOFF_SECONDS`.
- Flagged a known open SDK bug for `signature_type=3` (deposit wallet / POLY_1271): L1 auth currently binds to the EOA instead of the deposit wallet as of June 2026, causing rejections — `preflight.py` now warns about this and recommends `signature_type=2` as a workaround
- Updated `requirements.txt` to `py-clob-client-v2`
- Rewrote `preflight.py` to check SDK version, pUSD balance/allowance, and surface the latency note before any live trading



**2026-06-30 (auth/execution diagnostic pass)**
- Added `WALLET_SIGNATURE_TYPE` config option (0=EOA, 1=Email/Magic, 2=Browser proxy) — previously hardcoded/missing, which would have caused order failures for most non-EOA users
- Fixed `get_wallet_balance()` and added `check_allowance()` — was calling `get_balance_allowance()` with no arguments (would throw); now passes `BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)` correctly
- Fixed `get_open_positions()` — was calling `get_orders()` with no arguments; now passes `OpenOrderParams()`
- Fixed a real share/dollar unit mismatch: all sell-back and one-leg-handling paths in `websocket_listener.py` were passing dollar amounts into `place_ioc()`'s SELL path, which needs share counts. Now uses the actual filled `shares` from the BUY leg's result.
- `place_ioc()` now uses `OrderType.FAK` (Polymarket's actual Immediate-or-Cancel equivalent) instead of a non-existent `OrderType.IOC`
- Order executor now fails loudly and clearly (`auth_ok` flag + explicit error results) instead of silently warning and continuing with a broken client
- Added `preflight.py` — a standalone diagnostic script to verify credentials, balance, allowance, and market discovery before running `main.py` with real funds
- Added Pre-Flight Checklist section to this README, including a signature-type lookup table and known SDK precision/behavior notes



**2026-06-30 (later)**
- Rebuilt dashboard from a single flat page into a **4-section app** with bottom tab navigation (Overview / Markets / Trades / Settings)
- New design system: ink-navy base, mint/coral YES-NO signal colors, periwinkle accent — Space Grotesk + IBM Plex Mono + Inter
- Added the **combined-cost gauge** — a per-market visual bar showing live YES/NO prices against the $1.00 threshold, so edge is visible at a glance
- Dashboard is now fully responsive (phone → tablet → desktop) with safe-area support for notched phones
- `dashboard/app.py` now accepts `capital` and `listener` so the Markets tab can show live YES/NO ask prices, and Settings shows the real capital mode/unit size
- `main.py` updated to pass `capital` and `listener` into `create_app()`


**2026-06-30**
- Expanded tradeable assets from BTC/ETH to all 6: **BTC, ETH, XRP, SOL, BNB, DOGE**
- Now trading **12 pairs total** (6 assets × 5min and 15min durations)
- Updated `config.py` — `ACTIVE_PAIRS` and `SLUG_PATTERNS` now cover all 12 pairs
- Updated FIXED capital mode math: $24 total deployed at `UNIT_SIZE = 1.0` (was $8 at 4 pairs)
- Market discovery interval set to **10 seconds** (was 60s)
- Wallet sync interval set to **60 seconds / 1 minute** (was 5 minutes)
- Expiry cutoff set to **10 seconds** with tiered edge requirements (ACTIVE/CAUTIOUS/FINAL)
- Minimum net profit corrected to **$0.03–$0.05 per $1 trade** (was mistakenly $0.30)

