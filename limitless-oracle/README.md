# Limitless Oracle — Deployment Guide

15-minute candlestick direction prediction dashboard with live/shadow trading
on [limitless.exchange](https://limitless.exchange).

---

## Project Structure

```
limitless-oracle/
├── server/
│   ├── app.py                  ← Flask backend (API + serves React build)
│   ├── limitless_executor.py   ← Your existing executor (unchanged)
│   └── requirements.txt        ← Python dependencies
├── client/
│   ├── src/
│   │   ├── main.jsx            ← React entry point
│   │   └── App.jsx             ← Full dashboard component
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
├── render.yaml                 ← Render Blueprint (auto-config)
├── .gitignore
└── README.md
```

---

## Deploying to Render

### Step 1 — Push to GitHub

```bash
# From the limitless-oracle/ folder:
git init
git add .
git commit -m "Initial commit — Limitless Oracle"

# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/limitless-oracle.git
git branch -M main
git push -u origin main
```

---

### Step 2 — Create the Render Service

**Option A — Blueprint (recommended, auto-configures everything):**

1. Go to [render.com](https://render.com) → **New → Blueprint**
2. Connect your GitHub account and select the `limitless-oracle` repo
3. Render reads `render.yaml` and pre-fills all settings
4. Click **Apply** — done

**Option B — Manual Web Service:**

1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your `limitless-oracle` GitHub repo
3. Fill in:

| Field | Value |
|---|---|
| **Name** | `limitless-oracle` |
| **Runtime** | `Python 3` |
| **Region** | Oregon (or nearest to you) |
| **Branch** | `main` |
| **Root Directory** | *(leave blank)* |
| **Build Command** | see below |
| **Start Command** | see below |

**Build Command:**
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs && cd client && npm install && npm run build && cd .. && pip install -r server/requirements.txt
```

**Start Command:**
```bash
cd server && gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

---

### Step 3 — Set Environment Variables

In your Render service → **Environment** tab, add:

| Variable | Value | Required |
|---|---|---|
| `LIMITLESS_PRIVATE_KEY` | `0x...` your EOA private key | ✅ For live trading |
| `LIMITLESS_TOKEN_ID` | from POST /auth/api-tokens/derive | ✅ For live trading |
| `LIMITLESS_TOKEN_SECRET` | base64 secret (shown once) | ✅ For live trading |
| `BASE_RPC_URL` | `https://mainnet.base.org` | Optional (has default) |
| `PYTHON_VERSION` | `3.11.0` | Recommended |

> ⚠️ **EOA wallets only.** Do NOT set `LIMITLESS_SMART_WALLET`.
> Your maker = signer = EOA address derived from your private key.

> 💡 You can also enter credentials directly in the dashboard Settings tab
> without setting env vars. Dashboard credentials override env vars.

---

### Step 4 — Deploy & Open

Click **Create Web Service**. Render will:
1. Run the build command (installs Node → builds React → installs Python deps)
2. Start Gunicorn serving Flask
3. Give you a URL like `https://limitless-oracle.onrender.com`

Open that URL — your dashboard is live.

---

## Local Development

### Prerequisites
- Python 3.11+
- Node.js 20+

### Run backend
```bash
cd server
pip install -r requirements.txt

# Set credentials (or use the dashboard Settings tab)
export LIMITLESS_PRIVATE_KEY=0x...
export LIMITLESS_TOKEN_ID=...
export LIMITLESS_TOKEN_SECRET=...

python app.py
# → Flask runs on http://localhost:5000
```

### Run frontend (separate terminal)
```bash
cd client
npm install
npm run dev
# → Vite runs on http://localhost:3000
# → /api/* calls are proxied to Flask on :5000
```

Open `http://localhost:3000`

---

## Free Plan Keep-Alive

The dashboard pings `/api/ping` every **2 seconds** from the browser.
This prevents Render's free plan from spinning down the service after
15 minutes of inactivity. You do not need to do anything extra.

If you need guaranteed uptime (e.g. to catch signals while the browser
is closed), upgrade to the **Starter plan ($7/mo)** in Render —
this disables sleep entirely.

---

## API Endpoints

All endpoints are served by Flask at `/api/`:

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/ping` | Health check / keep-alive |
| `POST` | `/api/limitless/execute` | Place live or shadow order |
| `POST` | `/api/limitless/claim` | Redeem winning positions |
| `POST` | `/api/limitless/order-status` | Check if order was filled |
| `POST` | `/api/limitless/validate` | Validate credentials |
| `GET` | `/api/limitless/slug/:symbol` | Discover active market slug |

---

## Signal System

- **Pairs:** BTC-USDT, ETH-USDT, SOL-USDT, DOGE-USDT, XRP-USDT, BNB-USDT
- **Data source:** OKX spot market (public API, no key needed)
- **Families:** BTC·ETH (A) | SOL·DOGE (B) | XRP·BNB (C) — rotates between families
- **Confluence:** 15 strategy layers scored and weighted per candle
- **Cooldown:** 2 candles after 2 consecutive losses
- **Win/Loss tracking:** Starts at candle close (00:00→00:15 boundary)
- **Daily reset:** Stats reset at 00:00 UTC

---

## Getting Your Limitless API Token

```
POST https://api.limitless.exchange/auth/api-tokens/derive
```

Do this from your browser DevTools console while logged in:
```javascript
fetch("https://api.limitless.exchange/auth/api-tokens/derive", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  credentials: "include",
  body: JSON.stringify({ name: "oracle-bot" })
}).then(r => r.json()).then(console.log)
```

Copy `tokenId` → `LIMITLESS_TOKEN_ID`  
Copy `secret` → `LIMITLESS_TOKEN_SECRET` (shown once — save it now)

---

## Troubleshooting

**"Signer does not match"**
→ You have `LIMITLESS_SMART_WALLET` set. Remove it — EOA mode only.

**"USDC not approved"**
→ Visit limitless.exchange with your wallet and place one manual trade.
The UI triggers the USDC approval automatically.

**Build fails on Render**
→ Make sure your root directory is blank (not `server/` or `client/`).
The build command handles both subdirectories.

**Dashboard shows "—" prices**
→ OKX API is public but rate-limited. Prices refresh every 15 seconds.
Check browser console for CORS errors.

**Free plan keeps sleeping**
→ The 2-second ping only works while the browser tab is open.
Upgrade to Starter plan for always-on execution.
