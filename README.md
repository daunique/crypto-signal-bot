# Polymarket Momentum-Crossover Bot

Automates the momentum-crossover signal from our backtests: at K1 seconds
after a 5-min (or 15-min) crypto Up/Down market opens, record YES/NO
mid-price; at K2 seconds, record it again; buy whichever side gained more
ground, at that moment's ask price; hold to resolution.

Deployable two ways — **Fly.io** (recommended: runs continuously without
depending on your phone staying on and connected) or **Termux** (if you'd
rather keep it local). Both run the exact same script.

**Read the whole "Before you go live" section before doing anything with real
money.** Several things here will surprise you if you skip it.

---

## Before you go live

**1. The backtest is thin.** 33-53 trades across two sessions. We calculated
that distinguishing a real 65% win rate from a 55% (roughly breakeven) rate
needs about 190 trades at normal statistical confidence. You're well short of
that. This bot is a way to keep testing, not a proven system.

**2. Your account uses signature_type 1 (email/Magic-link wallet), which
this is now configured for by default.** That means you need **two** values,
not one: `POLY_PRIVATE_KEY` (the signing key, exported from Polymarket
Settings) *and* `POLY_FUNDER_ADDRESS` (your proxy wallet address, shown on
that same Settings page — this is where your funds actually live). The
private key alone isn't enough for signature_type 1; the bot will refuse to
start and tell you which one's missing if you leave either blank.

**3. Your $0.20 stake will mostly get skipped, not executed — probably.**
Polymarket's CLOB enforces a minimum order size (5 shares, as of when this
was written). At the entry prices this strategy actually trades at
(30-85c/share in our backtests), 5 shares costs **$1.50 to $4.25** — well
above $0.20. By default this script checks the minimum for each market
before trading and **skips** (logs, doesn't force) any trade that doesn't
clear it.

Whether that minimum applies to marketable FOK/FAK orders specifically
(vs. only to resting GTC/GTD orders) isn't clearly confirmed anywhere —
Polymarket's docs describe it generically, but a matching-engine rule meant
to stop dust *resting* orders plausibly doesn't apply to an order that fills
instantly and never rests. A rejected order costs nothing but an API call
(no funds move), so this is safe to test directly: set
`SKIP_BELOW_MIN_SIZE=false` and the bot will attempt below-minimum trades on
`--live` anyway, logging the exchange's real response tagged `MIN_SIZE_TEST`
in the trade log. That's a more reliable answer than anything either of us
can find in documentation. Until you've confirmed it that way, assume the
minimum applies and plan on raising `STAKE_USD` to $3-5 if you want trades
to reliably go through.

**4. There is no sandbox.** Polymarket doesn't offer test-mode order
placement. `--dry-run` in this script is a *local* simulation using real
live prices — it never sends a real order — but it's the only "practice"
available. Run it for a while and read the logs before ever using `--live`.

**5. Check you're even allowed to trade.** The main Polymarket CLOB (which
is what this script talks to) blocks order placement from a real list of
countries — the US, UK, France, Germany, Italy, Netherlands, Belgium,
Australia, and several others, as of when this was written (full list
changes over time: https://docs.polymarket.com/api-reference/geoblock).
US persons have a *separate* CFTC-regulated product, Polymarket US
(polymarket.us), with a completely different API this script does not
speak. The script calls Polymarket's own geoblock endpoint on startup and
refuses to go live if you're blocked — but it can't tell you which product
you're *supposed* to be using. Confirm that yourself first. Note this
becomes about Fly.io's server location if you deploy there, not your
phone's — pick `primary_region` accordingly.

**6. This SDK ecosystem changes fast.** Polymarket did a hard breaking
migration to "CLOB V2" in 2026 — old libraries stopped working entirely,
with no backward compatibility. This script uses `py-clob-client-v2`. If
something here throws an unexpected error, check
https://github.com/Polymarket/py-clob-client-v2 first — the method surface
may have moved on since this was written.

**7. Not financial advice.** I'm not a financial advisor. This is your
decision and your risk.

---

## Deploy on Fly.io (recommended)

```bash
# Install flyctl if you haven't: https://fly.io/docs/flyctl/install/
fly auth login
```

Edit `fly.toml`: change `app = "CHANGE-ME-polymarket-momentum-bot"` to
something unique (app names are global across all Fly.io users), and set
`primary_region` to whichever is closest to you (`fly platform regions` for
the list).

```bash
fly launch --no-deploy   # detects the Dockerfile, creates the app, skips auto-deploy
```

Set your credentials as secrets — **never put these in fly.toml**, which
isn't encrypted the way secrets are:

```bash
fly secrets set POLY_PRIVATE_KEY="0xyourprivatekey"
fly secrets set POLY_FUNDER_ADDRESS="0xyourproxywalletaddress"
fly secrets set BOT_MODE="check"   # start in check mode — see below
```

First deploy, in check mode (verifies auth/geo/environment, trades nothing):

```bash
fly deploy
fly logs
```

You should see the geoblock check, server-time check, and "All checks
passed." If something's wrong, `fly logs` will say what.

Switch to dry-run to watch it simulate trades against live prices for a
while before risking anything:

```bash
fly secrets set BOT_MODE="dry-run"
fly machine restart $(fly machine list -q)
fly logs
```

Only once you've read enough dry-run logs to trust it, go live:

```bash
fly secrets set CONFIRM_LIVE_TRADING="I UNDERSTAND THE RISK"
fly secrets set BOT_MODE="live"
fly machine restart $(fly machine list -q)
```

`CONFIRM_LIVE_TRADING` has to match that phrase exactly — this is the
headless equivalent of the typed confirmation prompt (there's no terminal
attached on Fly for `input()` to ask through). Setting it once means the
machine won't ask again on future restarts, so treat it with the same care
as the private key: remove the secret (`fly secrets unset
CONFIRM_LIVE_TRADING`) if you want to force yourself to re-confirm later.

**Monitoring:**
```bash
fly logs                 # stream logs live
fly status                # is the machine running
fly ssh console            # poke around inside the running machine
```

**Pulling the trade log off the volume** (it lives at `/data/trade_log.csv`
on the machine, not on your local disk):
```bash
fly ssh sftp get /data/trade_log.csv ./trade_log.csv
```

**Stopping it:**
```bash
fly scale count 0    # stops the machine, keeps the app/volume/secrets around
```

The volume (`polybot_data`, mounted at `/data`) is what makes the daily
trade-count / daily-loss circuit breaker survive restarts — without it,
every redeploy would silently reset today's counters. `fly deploy` and
`fly launch` create it automatically from the `initial_size` in `fly.toml`
on first deploy.

---

## Termux setup (alternative to Fly.io)

```bash
pkg update && pkg upgrade
pkg install python clang rust libffi openssl git
```

The `clang`/`rust`/`libffi`/`openssl` packages exist because some of this
stack's dependencies (crypto/signing libraries) sometimes need to compile
native code on install. If `pip install` fails partway through on Termux
with a build error, this is almost always why — install the missing native
package Termux's error message names, then retry.

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

If a specific package fails to build, try searching the exact error for
"termux" — this is a common enough combination that most native-build
issues here have a known fix.

### Configure

```bash
cp .env.example .env
nano .env   # or any editor you have
```

Fill in `POLY_PRIVATE_KEY` and `POLY_FUNDER_ADDRESS` at minimum (both are
required for the default signature_type=1). **Never share this file or
commit it anywhere.** Anyone with the private key can move your funds.

### Run

```bash
# 1. Verify environment, auth, and geo-eligibility — does nothing else
python3 momentum_bot.py --check

# 2. Watch it simulate trades against live prices, no real money
python3 momentum_bot.py --dry-run

# 3. Only once you've reviewed dry-run logs and understand the risk:
python3 momentum_bot.py --live
```

`--live` requires typing `I UNDERSTAND THE RISK` at a prompt before it does
anything (Termux has a terminal attached, so this works here, unlike on
Fly.io). This is intentional friction, not a bug.

### Keeping it running on a phone

This is the actual problem Fly.io avoids — Termux gets killed by Android's
battery manager if you background it, no matter what you do. Two things
help but don't fully fix it:

```bash
termux-wake-lock          # stops Android from sleeping the CPU
```

Run the bot inside `tmux` (`pkg install tmux`) so it survives you closing
the terminal app:

```bash
tmux new -s polybot
python3 momentum_bot.py --dry-run
# Ctrl+B then D to detach; `tmux attach -t polybot` to come back
```

Also disable battery optimization for Termux in Android's app settings. If
you're finding deployment through the phone unreliable, that's exactly why
Fly.io exists as an option above — a small always-on VM doesn't have a
battery manager deciding to kill it.

---

## Reading the trade log

Everything gets logged to `trade_log.csv` in `BOT_STATE_DIR` (`/data` on
Fly.io, `~/.polymarket_bot` by default on Termux). Every row is one decision
the bot made — traded, skipped, or errored — with the reason. This file is,
in effect, a smaller version of the same kind of capture data you've been
analyzing — worth periodically checking win rate against what we've
backtested, and worth being suspicious of if it *doesn't* match.

Columns: `timestamp_iso, slug, asset, duration_min, side, signal_strength,
entry_price, shares, stake_usd, mode, status, order_id, note`

`status` values you'll see: `dry_run`, `live_order_placed`,
`live_order_failed`, `skipped` (with a reason in `note`),
`skipped_no_signal` (market never got a clean K1 tick in time — same data
quality issue we ran into with your own captures).

## What this script does NOT do

- No exit/stop-loss logic — buy-and-hold-to-resolution only, exactly as
  backtested. Adding an exit rule means backtesting that rule first, not
  bolting it on blind.
- No resolution reconciliation of realized win/loss into the log yet —
  `status` tells you whether an order was placed, not whether it later won.
  You'd need to poll each market's resolution after its expiry and match it
  back to the log — a reasonable next step once you have live trades to
  reconcile.
- No support for deposit wallets (signature type 3) — that flow requires a
  separate Builder API key and relayer setup. See
  https://docs.polymarket.com/trading/deposit-wallets if that's your
  account type.
- No handling of Polymarket US — different product, different API.
