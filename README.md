# ⚡ CryptoSignal Bot — 15m Prediction Market

ML-powered 15-minute candlestick direction bot for BTC, ETH, SOL, XRP, BNB, DOGE.  
Signals delivered via Telegram. Auto-executes on [Limitless Exchange](https://limitless.exchange).

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
Tier is now a fixed label (`T2`) rather than a volume-spike-derived classification — the old T1/T2 split was tied to the ML engine's confidence + volume-spike logic, which no longer exists. The dashboard's "confidence" / "tier" fields are now populated from the fixed backtested win-rate table above, for display purposes only — they do not gate or filter live signals (the RSI(2) threshold is the only gate).

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
