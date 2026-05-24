# Limitless Oracle — Render Deployment Guide
### Pure Node.js · No apt-get · Deploys first time, every time

---

## What changed from the Python version

The backend is now a **Node.js Express server** (`server/index.js`).
It does everything the Python executor did:
- HMAC-signed requests to Limitless API
- EIP-712 order signing via **ethers v6** (same math, no Python/web3 needed)
- Market slug discovery, orderbook fetch, owner ID resolution
- Shadow and live order execution
- Claim winnings, order status check

The entire project runs on **one Node runtime** — Render builds it with
a single `npm install && npm run build` and starts it with `node server/index.js`.

---

## File Structure

```
limitless-oracle/
├── package.json          ← root (Express + ethers deps, build/start scripts)
├── render.yaml           ← Render Blueprint
├── .gitignore
├── server/
│   └── index.js          ← Express API + EIP-712 signing (replaces executor.py)
└── client/
    ├── package.json      ← Vite + React
    ├── vite.config.js
    ├── index.html
    └── src/
        ├── main.jsx
        └── App.jsx       ← Full dashboard (unchanged)
```

---

## Deploy to Render — Step by Step

### 1. Push to GitHub

Open a terminal in the `limitless-oracle/` folder:

```bash
git init
git add .
git commit -m "Limitless Oracle — initial deploy"
```

Create a new repo at https://github.com/new (name it `limitless-oracle`), then:

```bash
git remote add origin https://github.com/YOUR_USERNAME/limitless-oracle.git
git branch -M main
git push -u origin main
```

---

### 2. Create the Web Service on Render

**Option A — Blueprint (easiest):**
1. render.com → **New → Blueprint**
2. Connect GitHub → select `limitless-oracle`
3. Render reads `render.yaml` and fills everything automatically
4. Click **Apply**

**Option B — Manual:**
1. render.com → **New → Web Service**
2. Connect `limitless-oracle` repo
3. Fill in exactly:

| Field | Value |
|---|---|
| Runtime | **Node** |
| Build Command | `npm install && npm run build` |
| Start Command | `node server/index.js` |
| Node Version | `20.11.0` |

---

### 3. Set Environment Variables

Render dashboard → your service → **Environment** tab → **Add Environment Variable**:

| Key | Value | Notes |
|---|---|---|
| `LIMITLESS_PRIVATE_KEY` | `0x...` | Your EOA private key |
| `LIMITLESS_TOKEN_ID` | `tok_...` | From Limitless API token endpoint |
| `LIMITLESS_TOKEN_SECRET` | base64 string | Shown once — save it immediately |
| `NODE_ENV` | `production` | Already in render.yaml |

> **EOA wallets only.** Do NOT set `LIMITLESS_SMART_WALLET`.
> In EOA mode: maker = signer = the address derived from your private key.

> **Tip:** You can also enter credentials in the dashboard Settings tab at runtime.
> Dashboard credentials override env vars, so you don't need to redeploy to switch wallets.

---

### 4. Deploy

Click **Create Web Service** (or Render auto-deploys from the Blueprint).
Build takes ~2 minutes. You'll get a URL like:
```
https://limitless-oracle.onrender.com
```

---

## Getting Your Limitless API Token

Run this in your browser DevTools console **while logged into limitless.exchange**:

```javascript
fetch("https://api.limitless.exchange/auth/api-tokens/derive", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  credentials: "include",
  body: JSON.stringify({ name: "oracle-bot" })
}).then(r => r.json()).then(d => {
  console.log("TOKEN ID:", d.tokenId);
  console.log("SECRET (save now!):", d.secret);
})
```

- `tokenId` → `LIMITLESS_TOKEN_ID`
- `secret` → `LIMITLESS_TOKEN_SECRET` *(shown once — copy it immediately)*

---

## Local Development

### Run the backend
```bash
# From project root:
npm install

export LIMITLESS_PRIVATE_KEY=0x...
export LIMITLESS_TOKEN_ID=...
export LIMITLESS_TOKEN_SECRET=...

node server/index.js
# → http://localhost:5000
```

### Run the frontend (separate terminal)
```bash
cd client
npm install
npm run dev
# → http://localhost:3000  (proxies /api → :5000 automatically)
```

---

## Commands Reference

| Purpose | Command |
|---|---|
| Install all deps | `npm install` (root) + `cd client && npm install` |
| Build frontend | `npm run build` (runs `cd client && npm install && npm run build`) |
| Start server | `node server/index.js` |
| Full Render build | `npm install && npm run build` |
| Full Render start | `node server/index.js` |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/ping` | Keep-alive / health check |
| POST | `/api/limitless/execute` | Place live or shadow order |
| POST | `/api/limitless/claim` | Redeem winning positions |
| POST | `/api/limitless/order-status` | Check if order was filled |
| POST | `/api/limitless/validate` | Validate credentials |
| GET | `/api/limitless/slug/:symbol` | Discover active market slug |

---

## Troubleshooting

**Build failed — `Cannot find module`**
→ Make sure `package.json` at the root has `"type": "module"` and all
dependencies listed. Run `npm install` locally first to verify.

**`"type": "module"` errors in server**
→ The server uses ES module syntax (`import`/`export`). Confirm root
`package.json` has `"type": "module"`.

**"No active 15-min market found"**
→ This is normal at the :00/:15/:30/:45 boundary — the old market expires
and the new one takes a few seconds to appear. The engine retries 5×.

**"Signer does not match"**
→ Do not set `LIMITLESS_SMART_WALLET`. EOA mode only: your private key
*is* both your maker and signer.

**"USDC not approved"**
→ Visit limitless.exchange with your wallet and place one manual trade first.
The UI triggers the USDC approval on-chain automatically.

**Free plan sleeping (signals missed)**
→ The 2s keep-alive ping only works while your browser tab is open.
Upgrade to Render **Starter ($7/mo)** for always-on execution.

**Order rejected with HTTP 400**
→ The full API response is returned in `api_response`. Check the dashboard
console or Render logs for the exact rejection reason.
