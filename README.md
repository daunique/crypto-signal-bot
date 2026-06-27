# ⚡ CryptoSignal Bot — 15m Prediction Market

ML-powered 15-minute candlestick direction bot for BTC, ETH, SOL, XRP, BNB, DOGE.  
Signals delivered via Telegram. Auto-executes on [Limitless Exchange](https://limitless.exchange).

---

## 🪰 Deploy to Fly.io (Step-by-Step)

> **Why Fly instead of Render?** Fly gives you a real Docker container with
> persistent always-on compute, which matches this bot's requirements better
> than Render's free tier. The bot **must** run as exactly **one** machine,
> always on — APScheduler runs in-process inside the single gunicorn worker,
> so a second machine (autoscaling) or an idle-stopped machine causes
> duplicate signal generation and duplicate live order execution. `fly.toml`
> in this repo already pins `min_machines_running = 1`,
> `auto_stop_machines = "off"`, and `auto_start_machines = false` to enforce
> this — don't change those settings unless you also rework the scheduler to
> use a distributed lock.

### Step 1 — Install flyctl
```bash
curl -L https://fly.io/install.sh | sh
export FLYCTL_INSTALL="$HOME/.fly"
export PATH="$FLYCTL_INSTALL/bin:$PATH"
```
(On Termux, `fly-deploy.sh` in this repo does this for you automatically.)

### Step 2 — Log in
```bash
flyctl auth login
```

### Step 3 — Launch the app
From inside the project folder (where `Dockerfile` and `fly.toml` live):
```bash
flyctl launch --copy-config --name your-app-name --no-deploy --yes
```
This reads the existing `fly.toml` instead of generating a new one. Update
the `app = "..."` line in `fly.toml` to match your chosen app name.

### Step 4 — Set secrets
Same variables as `.env.example`, set via `flyctl secrets set` instead of a
dashboard:
```bash
flyctl secrets set SECRET_KEY=$(openssl rand -hex 32)
flyctl secrets set DATABASE_URL=postgresql://user:password@host:5432/dbname
flyctl secrets set LIMITLESS_TOKEN_ID=your_token_id
flyctl secrets set LIMITLESS_TOKEN_SECRET=your_base64_secret
flyctl secrets set LIMITLESS_PRIVATE_KEY=0xyour_wallet_private_key
flyctl secrets set LIMITLESS_SMART_WALLET=0xyour_smart_wallet_address
flyctl secrets set TELEGRAM_BOT_TOKEN=your_telegram_bot_token
flyctl secrets set TELEGRAM_CHAT_ID=your_telegram_chat_id
```
`DEFAULT_MODE` and `DEFAULT_POSITION_SIZE` are already set as plain (non-secret)
env vars in `fly.toml` — edit them there if you want different defaults.

> Use a Postgres `DATABASE_URL` (e.g. Supabase), not SQLite — Fly's
> filesystem is ephemeral on redeploy just like Render's, so SQLite data
> would be lost on every deploy.

### Step 5 — Deploy
```bash
flyctl deploy
```
Or from Termux, run `bash fly-deploy.sh`, which wraps all of the above and
also checks afterward that exactly one machine is running.

### Step 6 — Verify only one machine is running
```bash
flyctl machines list
```
You should see exactly **one** machine. If you ever see more than one,
destroy the extra immediately:
```bash
flyctl machines destroy <extra-machine-id>
```

### Step 7 — Watch logs
```bash
flyctl logs
```
Your dashboard will be live at: `https://your-app-name.fly.dev`

---

## 🚀 Deploy to Render (Step-by-Step)

### Step 1 — GitHub Setup
1. Create a new GitHub repository (e.g. `crypto-signal-bot`)
2. Upload ALL files from this folder into it (drag & drop in GitHub UI)
3. Make sure `.env` is NOT uploaded (it's in `.gitignore`)

### Step 2 — Render Setup
1. Go to [render.com](https://render.com) → New → Web Service
2. Connect your GitHub repo
3. Render will auto-detect `render.yaml` — click **Apply**

### Step 3 — Set Environment Variables in Render
Go to your service → **Environment** tab → Add each:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | Your chat ID (use @userinfobot) |
| `LIMITLESS_TOKEN_ID` | Token ID from `POST /auth/api-tokens/derive` |
| `LIMITLESS_TOKEN_SECRET` | Base64 secret from same response (shown **once** — save it) |
| `LIMITLESS_PRIVATE_KEY` | Your wallet private key (starts with 0x) — signs orders via EIP-712 |
| `LIMITLESS_OWNER_ID` | Your numeric profile ID from `GET /profiles/{your_address}` |
| `DEFAULT_MODE` | `shadow` (start here, switch to live when ready) |
| `DEFAULT_POSITION_SIZE` | `10` |

### Step 4 — Deploy
Click **Deploy** in Render. First deploy takes ~3-5 minutes.  
Your dashboard will be live at: `https://your-service-name.onrender.com`

---

## 🤖 Telegram Bot Setup

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Send `/newbot` → follow prompts → copy the token
3. Message [@userinfobot](https://t.me/userinfobot) to get your Chat ID
4. Paste both into Render environment variables

---

## 🔑 Limitless Auth Setup (Two Layers)

### Layer 1 — HMAC Token (authenticates HTTP requests)
1. Go to [limitless.exchange](https://limitless.exchange) and connect your wallet
2. Call `POST https://api.limitless.exchange/auth/api-tokens/derive` with your Privy `Bearer` token in the `identity` header and `{"scopes": ["trading"]}` as the body
3. The response contains `tokenId` and `secret` — **the secret is shown only once, save it immediately**
4. Set `LIMITLESS_TOKEN_ID` and `LIMITLESS_TOKEN_SECRET` in Render

### Layer 2 — EIP-712 Signing (authenticates order payloads)
1. Export your wallet private key from MetaMask → Settings → Accounts → Export Private Key
2. Set `LIMITLESS_PRIVATE_KEY` in Render (the `0x...` key)

### Profile ID
Call `GET https://api.limitless.exchange/profiles/{your_wallet_address}` and copy the numeric `id` field → set as `LIMITLESS_OWNER_ID`

> ⚠️ **Never share your private key or token secret.** Only store them in Render's environment variables (never in GitHub).

---

## 📊 How It Works

### Signal Engine (v5 — RSI(2) Mean-Reversion, no ML)
- Fetches 15m OHLCV candles from **OKX Spot** for 6 pairs
- Computes a single indicator: **RSI(2)**, Wilder-smoothed
- Rule: **RSI(2) ≤ 10 → UP signal** (oversold mean-reversion) · **RSI(2) ≥ 90 → DOWN signal** (overbought mean-reversion)
- No machine learning of any kind — no Random Forest, no Gradient Boosting, no model training, no feature engineering. The rule is the signal.
- **Anti-spam**: Only fires the SINGLE best signal per 15-minute candle (when multiple pairs qualify on the same candle, the one with the higher backtested win rate for its direction is chosen — see table below)

This replaces the previous v4 engine, which used a Random Forest + Gradient Boosting + Extra Trees ensemble on ~70 engineered features. The ML engine has been fully removed (no `sklearn` import remains anywhere in the codebase); a backup of the old `signal_engine.py` is kept as `signal_engine_v4_ml_BACKUP.py` for reference/rollback.

### Backtest Results (next-candle resolution, real OKX/Binance 15m history)
Resolution rule matches `job_resolve_outcomes` exactly: signal UP wins if next candle's close > open; signal DOWN wins if close < open.

3-month aggregate (Feb–Apr 2026), and full-history check (Jan 2025–Jun 2026 for BNB/DOGE, Jan–May 2026 for the rest):

| Pair | Up signals/day | Up win% | Down signals/day | Down win% |
|------|----------------|---------|-------------------|-----------|
| BTC-USDT  | ~10.6 | ~57% | ~10.8 | ~58% |
| ETH-USDT  | ~10.6 | ~57% | ~10.7 | ~60% |
| SOL-USDT  | ~10.8 | ~60% | ~10.8 | ~56% |
| XRP-USDT  | ~11.6 | ~56% | ~10.5 | ~56% |
| BNB-USDT  | ~11.4 | ~54% | ~11.8 | ~54% |
| DOGE-USDT | ~11.3 | ~55% | ~10.5 | ~57% |

**Honest caveats, stated plainly:**
- Win rates are 54–60%, not 70%+. This is a real, modest, statistically-consistent edge — not a guaranteed-win system.
- Observed losing streaks of **8–11 consecutive losses** occurred in backtesting even on the better-performing pairs/directions. Martingale stake-doubling compounds losses geometrically through a streak like that — size and cap it accordingly.
- BTC and BNB show very little difference between their UP and DOWN win rates (often <1%, within noise for a few hundred monthly signals); don't read a strong directional bias into either pair.
- Signal frequency (~10.5–11.8/day per direction per pair) is what the RSI(2) rule actually produces on this data — it was NOT tuned to hit any particular target count.
- These numbers come from historical backtesting and are not a guarantee of future performance. Market conditions change.

### Live-Pair Selection & ATR Regime Filter (current configuration)
After backtesting every 2-, 3-, 4-, 5-, and 6-pair combination against 12 candidate confluence filters (ATR regime, EMA trend, VWAP deviation, volume z-score, ADX, Stochastic, MACD histogram turn, time-of-day), the best loss-streak-vs-volume tradeoff found was:

- **Live execution**: BTC-USDT + ETH-USDT + BNB-USDT only, each gated by an additional ATR(14) volatility-regime filter (`PAIR_CONFIG[sym]["atr_filter"] = 0.85`) — skips a signal if it falls in the most volatile 15% of that pair's own trailing 96-candle (1-day) ATR history.
- **Signal-only (no live orders)**: SOL-USDT, XRP-USDT, DOGE-USDT — these were not part of the validated live combo and do NOT have the ATR filter applied (a wider sweep showed this filter helps some pairs and hurts others; it is only enabled where it was actually validated).

April 2026 backtest (30 days, BTC+ETH+BNB combined, ATR filter active):

| Metric | Value |
|---|---|
| Signals | 520 (319W / 201L) |
| Win rate | 61.35% |
| Signals/day | 17.33 |
| Max loss streak | 6 (vs ~10 unfiltered) |
| Days at/above 50% win rate | 26/30 |

This is implemented as a per-pair `atr_filter` value in `signal_engine.py`'s `PAIR_CONFIG`, enforced inside `get_signal_for_symbol()` — a signal is fully discarded (not redirected to another pair) if it fails the ATR check, then normal family-rotation/pick_best_signal logic proceeds with whatever candidates remain. `Settings.no_execute_pairs` (migration v3.5 in `app.py`) keeps SOL/XRP/DOGE generating and tracking signals exactly as before, just without live order placement.

**This supersedes an earlier v3.4 migration** that had set all 6 pairs live — confirmed with the user that the backtested 3-pair configuration should take precedence over that earlier instruction.

### Signal Timing
- **:00, :15, :30, :45** UTC — Signal evaluated at candle open
- **:00, :15, :30, :45 (+0s vs +1s)** UTC — Outcome resolved at the SAME boundary, just before the next signal generates
- Win/Loss counted ONLY after the 15-minute candle fully closes

### Order Execution
- **Shadow mode**: Paper trades only, tracks P&L against $1,000 demo balance
- **Live mode**: Places real GTC limit orders on Limitless Exchange (and/or Polymarket, if enabled) using your configured wallet
- Contract price always ≤ $0.50 as required
- Order type: **GTC (Good Till Cancelled)**
- **Martingale mode**, if enabled, multiplies stake size after each loss per a configurable sequence — this materially increases risk of large drawdowns during the loss streaks described above. Test thoroughly in Shadow mode first.

### Signal Tiers
Tier is now a fixed label (`T2`) rather than a volume-spike-derived classification — the old T1/T2 split was tied to the ML engine's confidence + volume-spike logic, which no longer exists. The dashboard's "confidence" / "tier" fields are now populated from the fixed backtested win-rate table above, for display purposes only — they do not gate or filter live signals (the RSI(2) threshold, plus the ATR regime filter on BTC/ETH/BNB, are the only gates).

---

## 🛠 Settings (via Dashboard)

| Setting | Range | Description |
|---------|-------|-------------|
| Mode | Live/Shadow | Toggle via dashboard |
| Position Size | $1–$1,000 | Decimal sizing supported |
| Martingale | On/Off | Multiplies stake after a loss, per a configurable sequence |
| Martingale Sequence | comma-separated multipliers | e.g. `1,1.5,2,3,4.5,6.7` |
| Contract Price Cap | $0.01–$0.50 | Max per contract |
| Min Confidence | 0.0–1.0 | 0.0 = disabled (default). Confidence is now a fixed backtested win-rate per pair/direction, not a live ML score — see Signal Engine section above |

### Signal Log Filters
The Signal Log page (`/api/signals`) now supports:
- **Live column**: shows whether each row's pair is currently configured for live execution (`LIVE`) or signal-only (`SIGNAL ONLY`), computed against the live `Settings.no_execute_pairs` value at view-time — not a stored historical flag, so it always reflects the current configuration.
- **Calendar date range filter** (`date_from` / `date_to`, `YYYY-MM-DD`), replacing the old preset-only dropdown (Today/Yesterday/7d/30d). The preset `date_filter` param still works server-side for any other caller, but the dashboard UI now uses two date pickers.
- **Active Pairs filter** (`active_only=1`): restricts the table to only the pairs currently set for live execution.

---

## 📁 File Structure

```
crypto-signal-bot/
├── app.py              # Flask app + all API routes
├── wsgi.py             # Render/Gunicorn entrypoint
├── extensions.py       # db + socketio instances
├── models.py           # SQLAlchemy DB models
├── signal_engine.py    # OKX data fetch + RSI(2) signal generation (no ML)
├── signal_engine_v4_ml_BACKUP.py  # previous ML ensemble engine, kept for reference/rollback only — not imported anywhere
├── limitless_executor.py # Limitless order placement (live + shadow)
├── polymarket_executor.py # Polymarket order placement (live + shadow)
├── telegram_bot.py     # Telegram notifications
├── scheduler.py        # APScheduler jobs (signal, resolve, summary, retrain)
├── templates/
│   └── index.html      # Full dashboard UI
├── requirements.txt    # Python dependencies (scikit-learn removed — no longer used)
├── render.yaml         # Render deployment config
├── .env.example        # Environment variable template
└── .gitignore
```

---

## ⚠️ Important Notes

- **Start in Shadow mode** and run for at least 1 week before switching live
- **Retrain job**: scheduler runs `job_retrain` every 4 hours (`02/06/10/14/18/22:05 UTC`) — under the new RSI(2) engine this is now a harmless no-op (there is no model to retrain). Earlier documentation in this file claimed retraining happened weekly on Sundays; that line was a documentation/code mismatch even before the engine swap. Fixed here; the job itself is left scheduled as-is since removing it is a behavior change outside the scope of the engine swap.
- **Known issue found & fixed**: scheduler.py's Rule 2 ("directional saturation filter") raised the confidence floor to 0.67 after 3 losses in the same direction within the last 6 signals. The new engine's confidence values only range ~0.558–0.612, so a 0.67 floor was unreachable — Rule 2 would have permanently blocked any direction it tripped. Floor lowered to 0.60 (near the top of the engine's real range) so the rule still raises the bar without being unreachable.
- **Known issue found, NOT changed**: `app.py`'s `/api/stats/pairs` route reads `cfg.get("threshold", 0.58)` from per-pair config. The new engine's `PAIR_CONFIG` now explicitly sets `threshold: 0.50` for every pair so this stops silently falling back to the old ML engine's stale `0.58` default.
- Loss streaks of 8–11 consecutive losses were observed in backtesting (see table above). If Martingale is enabled, understand that a streak like that compounds stake size geometrically — this is a real risk, not a hypothetical one.
- SQLite is used for simplicity on Render free tier; for production use PostgreSQL
- Render free tier sleeps after inactivity — upgrade to paid for 24/7 operation
