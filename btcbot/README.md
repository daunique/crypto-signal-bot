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
| `LIMITLESS_API_KEY` | From limitless.exchange → Profile → API Keys |
| `LIMITLESS_PRIVATE_KEY` | Your wallet private key (starts with 0x) |
| `LIMITLESS_OWNER_ID` | Your numeric profile ID from Limitless |
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

## 🔑 Limitless API Setup

1. Go to [limitless.exchange](https://limitless.exchange)
2. Connect your wallet (Base network)
3. Profile menu → **API Keys** → Generate new key (starts with `lmts_`)
4. Export your wallet private key from MetaMask → Settings → Accounts → Export
5. Find your Owner ID: call `GET https://api.limitless.exchange/profiles/{your_address}`

> ⚠️ **Never share your private key.** Only store it in Render's environment variables.

---

## 📊 How It Works

### Signal Engine
- Fetches 15m OHLCV candles from **OKX Spot** for 6 pairs
- Computes 40 technical indicators (RSI, MACD, Stochastic, BB, ADX, Williams %R, CCI, etc.)
- Runs **Random Forest + Gradient Boosting ensemble** (58.25% accuracy, ~16 signals/day)
- **Anti-spam**: Only fires the SINGLE best signal per 15-minute candle (highest confidence across all 6 pairs)

### Signal Timing
- **:00, :15, :30, :45** UTC — Signal evaluated at candle open
- **:01, :16, :31, :46** UTC — Outcome resolved at candle close
- Win/Loss counted ONLY after the 15-minute candle fully closes

### Order Execution
- **Shadow mode**: Paper trades only, tracks P&L against $1,000 demo balance
- **Live mode**: Places real GTC limit orders on Limitless Exchange
- Contract price always ≤ $0.50 as required
- Order type: **GTC (Good Till Cancelled)**

### Signal Tiers
| Tier | Condition | Daily Avg | Accuracy |
|------|-----------|-----------|----------|
| T1 (High) | ML ≥58% + Volume spike >1.5× | ~1-2/day | ~62% |
| T2 (Standard) | ML ≥58% | ~14-15/day | ~58% |

---

## 🛠 Settings (via Dashboard)

| Setting | Range | Description |
|---------|-------|-------------|
| Mode | Live/Shadow | Toggle via dashboard |
| Position Size | $1–$1,000 | Decimal sizing supported |
| Martingale | On/Off | Double after loss |
| Martingale Multiplier | 1.1×–5× | Loss multiplier |
| Contract Price Cap | $0.01–$0.50 | Max per contract |
| Min ML Confidence | 55%–75% | Higher = fewer but better signals |

---

## 📁 File Structure

```
crypto-signal-bot/
├── app.py              # Flask app + all API routes
├── wsgi.py             # Render/Gunicorn entrypoint
├── extensions.py       # db + socketio instances
├── models.py           # SQLAlchemy DB models
├── signal_engine.py    # OKX data fetch + ML signal generation
├── limitless_executor.py # Limitless order placement (live + shadow)
├── telegram_bot.py     # Telegram notifications
├── scheduler.py        # APScheduler jobs (signal, resolve, summary)
├── templates/
│   └── index.html      # Full dashboard UI
├── requirements.txt    # Python dependencies
├── render.yaml         # Render deployment config
├── .env.example        # Environment variable template
└── .gitignore
```

---

## ⚠️ Important Notes

- **Start in Shadow mode** and run for at least 1 week before switching live
- The bot retrains ML models every Sunday at 02:00 UTC automatically
- SQLite is used for simplicity on Render free tier; for production use PostgreSQL
- Render free tier sleeps after inactivity — upgrade to paid for 24/7 operation
