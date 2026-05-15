# SignalBot → Polymarket Migration
## Complete AWS eu-west-1 Deployment Guide

---

## What Changed

| Component | Before (Limitless) | After (Polymarket) |
|---|---|---|
| `limitless_executor.py` | GTC limit, EIP-712, Base chain | **DELETED** |
| `polymarket_executor.py` | — | FAK limit, HMAC L2 auth, Polygon |
| `scheduler.py` | imports `limitless_executor` | imports `polymarket_executor` |
| `app.py` | `/api/approval-status` (Base) | `/api/approval-status` (Polygon) |
| Chain | Base mainnet (8453) | Polygon mainnet (137) |
| Order type | GTC limit | **FAK limit** |
| Auth | HMAC + EIP-712 wallet signing | HMAC L2 API key |

**Signal engine is unchanged** — same OKX data, same ML models, same 15-min candles.

---

## Phase 1 — Update Your GitHub Repository

### Step 1.1 — Clone and copy new files

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO

# Copy the new/changed files in (from this package):
cp polymarket_executor.py .          # replaces limitless_executor.py
cp scheduler.py .                    # updated import
cp app.py .                          # updated routes
cp requirements.txt .                # adds psycopg2-binary
cp .env.example .                    # updated env vars
cp -r .ebextensions .                # AWS EB config (new)
```

### Step 1.2 — Remove the old executor

```bash
git rm limitless_executor.py
git rm limitless_executor.py.bak     # if present
```

### Step 1.3 — Update .gitignore

Make sure `.gitignore` contains:
```
.env
*.pyc
__pycache__/
*.db
.ebextensions/         # only if you want to keep EB config private
```

### Step 1.4 — Commit and push

```bash
git add polymarket_executor.py scheduler.py app.py requirements.txt \
        .env.example .ebextensions/
git commit -m "Migrate from Limitless to Polymarket FAK limit orders"
git push origin main
```

---

## Phase 2 — Get Polymarket API Credentials

### Step 2.1 — Create Polymarket API key

1. Go to **https://polymarket.com** (must use a non-geo-blocked IP, or VPN to Ireland/EU)
2. Connect your Polygon wallet (MetaMask recommended)
3. Click profile icon → **"API Keys"** → **"Create new key"**
4. Choose a passphrase (write it down — you'll need it)
5. Copy and save immediately:
   - `API Key` → `POLY_API_KEY`
   - `Secret` → `POLY_API_SECRET` (**shown once only**)
   - `Passphrase` → `POLY_API_PASSPHRASE`

### Step 2.2 — Export wallet private key

This is your Polygon wallet private key (same wallet connected to Polymarket):
- MetaMask: Account menu → "Account details" → "Export private key"
- Store as `POLY_PRIVATE_KEY` — **never commit to git**

### Step 2.3 — Find market condition IDs

You need a Polymarket market condition_id for each crypto pair you want to trade.
These are prediction markets like "Will BTC close above $X on [date]?"

**After deploying (or locally):**
```bash
# Install deps first
pip install requests

# Search for markets
python polymarket_executor.py --search "Will BTC"
python polymarket_executor.py --search "Will ETH"
# Copy the condition_id values into your env vars
```

Or browse https://polymarket.com/markets and find crypto price markets.
The condition_id is in the market URL or API response.

> ⚠️  Markets expire when they resolve. You must update condition IDs
> regularly (weekly for daily markets, monthly for weekly markets).

---

## Phase 3 — AWS Elastic Beanstalk Setup (eu-west-1 Ireland)

AWS Free Tier gives you 750 hours/month of t2.micro (enough for 1 instance running 24/7).

### Step 3.1 — Install AWS CLI and EB CLI

```bash
# AWS CLI
pip install awscli
aws configure
# Enter: Access Key ID, Secret Access Key, region=eu-west-1, output=json

# EB CLI
pip install awsebcli
```

Get your AWS Access Key: AWS Console → IAM → Users → Your user → Security credentials → Create access key.

### Step 3.2 — Initialise EB in your repo

```bash
cd YOUR_REPO

eb init signalbot-polymarket \
  --platform "Python 3.11" \
  --region eu-west-1
```

When prompted:
- Select region: **eu-west-1** (Ireland)
- Select platform: Python 3.11
- SSH key: create or select one (needed for debugging)
- CodeCommit: No

### Step 3.3 — Create the environment

```bash
eb create signalbot-prod \
  --instance-type t2.micro \
  --region eu-west-1 \
  --single \
  --timeout 20
```

`--single` = single-instance mode (no load balancer = free tier eligible).
This takes ~5 minutes to provision.

### Step 3.4 — Set environment variables on AWS

**Option A — EB CLI (recommended):**
```bash
eb setenv \
  SECRET_KEY="your_32char_random_string" \
  FLASK_ENV=production \
  DATABASE_URL="sqlite:////tmp/signals.db" \
  POLY_API_KEY="your_api_key" \
  POLY_API_SECRET="your_api_secret" \
  POLY_API_PASSPHRASE="your_passphrase" \
  POLY_PRIVATE_KEY="0xyour_private_key" \
  POLY_RPC_URL="https://polygon-rpc.com" \
  POLY_CHAIN_ID=137 \
  POLY_MARKET_BTC="your_btc_condition_id" \
  POLY_MARKET_ETH="your_eth_condition_id" \
  POLY_MARKET_SOL="your_sol_condition_id" \
  POLY_MARKET_XRP="your_xrp_condition_id" \
  POLY_MARKET_BNB="your_bnb_condition_id" \
  POLY_MARKET_DOGE="your_doge_condition_id" \
  TELEGRAM_BOT_TOKEN="your_telegram_token" \
  TELEGRAM_CHAT_ID="your_chat_id" \
  DEFAULT_MODE=shadow \
  DEFAULT_POSITION_SIZE=10
```

**Option B — AWS Console:**
1. AWS Console → Elastic Beanstalk → Your environment
2. Configuration → Software → Edit
3. Add each key-value pair under "Environment properties"

> 🔒  For `POLY_PRIVATE_KEY`, consider AWS Parameter Store instead:
> ```bash
> aws ssm put-parameter --name /signalbot/POLY_PRIVATE_KEY \
>   --value "0xyour_key" --type SecureString --region eu-west-1
> ```
> Then reference it in a `.ebextensions/secrets.config` file.

### Step 3.5 — Deploy

```bash
eb deploy
```

Watch the logs during deploy:
```bash
eb logs --stream
```

### Step 3.6 — Verify deployment

```bash
# Get your app URL
eb open

# Check health endpoint
curl https://your-app.eu-west-1.elasticbeanstalk.com/api/health

# Expected:
# {"status":"ok","exchange":"polymarket","time":"2026-..."}
```

---

## Phase 4 — One-Time Polymarket USDC Approval

Before live trading, approve USDC spending on Polygon:

### Option A — Dashboard (easiest)

```bash
curl -X POST https://your-app.eu-west-1.elasticbeanstalk.com/api/approve-usdc
```

### Option B — CLI

```bash
# SSH into your EB instance
eb ssh

# Run approval
cd /var/app/current
python polymarket_executor.py --approve
```

### Verify approval

```bash
curl https://your-app.eu-west-1.elasticbeanstalk.com/api/approval-status
# Expected: {"ready": true, "ctf_approved": true, "neg_approved": true}
```

---

## Phase 5 — Configure Markets and Go Live

### Step 5.1 — Find active condition IDs

```bash
curl "https://your-app.eu-west-1.elasticbeanstalk.com/api/markets/search?q=BTC"
```

Pick markets with:
- `active: true`
- High volume
- End date in the future (enough candles to trade)

### Step 5.2 — Update market IDs

```bash
eb setenv POLY_MARKET_BTC="0xabc123..." POLY_MARKET_ETH="0xdef456..."
```

This redeploys automatically.

### Step 5.3 — Test in shadow mode

The bot starts in `DEFAULT_MODE=shadow` — it runs the full signal pipeline and places fake orders, but no real USDC is spent. Watch the dashboard for a few hours to confirm signals are generating correctly.

```bash
curl https://your-app.eu-west-1.elasticbeanstalk.com/api/stats/today
```

### Step 5.4 — Switch to live mode

When you're confident:
```bash
curl -X POST https://your-app.eu-west-1.elasticbeanstalk.com/api/settings \
  -H "Content-Type: application/json" \
  -d '{"mode": "live"}'
```

Or from the dashboard UI.

---

## Phase 6 — GitHub → AWS Auto-Deploy (CI/CD)

So every `git push` auto-deploys to AWS:

### Step 6.1 — Add GitHub Actions workflow

Create `.github/workflows/deploy.yml` in your repo:

```yaml
name: Deploy to AWS EB

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install EB CLI
        run: pip install awsebcli

      - name: Deploy to Elastic Beanstalk
        env:
          AWS_ACCESS_KEY_ID:     ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        run: |
          eb init signalbot-polymarket \
            --platform "Python 3.11" \
            --region eu-west-1
          eb deploy signalbot-prod --timeout 20
```

### Step 6.2 — Add GitHub secrets

GitHub repo → Settings → Secrets and variables → Actions → New repository secret:
- `AWS_ACCESS_KEY_ID` — your AWS access key
- `AWS_SECRET_ACCESS_KEY` — your AWS secret key

> Create a dedicated IAM user for CI with only ElasticBeanstalk permissions.

Now every push to `main` auto-deploys.

---

## Ongoing Operations

### Check logs
```bash
eb logs
eb logs --stream   # live tail
```

### SSH into instance
```bash
eb ssh
tail -f /var/log/web.stdout.log
```

### Update market IDs (when markets expire)
```bash
# Find new markets
python polymarket_executor.py --search "Will BTC"

# Update
eb setenv POLY_MARKET_BTC="0xnew_condition_id"
```

### Check balance
```bash
curl https://your-app.eu-west-1.elasticbeanstalk.com/api/balance
```

### Check open positions
```bash
curl https://your-app.eu-west-1.elasticbeanstalk.com/api/positions
```

---

## AWS Free Tier Notes

- **EC2**: 750 hours/month t2.micro — enough for 1 instance 24/7
- **RDS**: 750 hours/month db.t2.micro — use for persistent DB (optional)
- **Data transfer**: 15 GB/month outbound free — bot uses very little
- **CloudWatch**: 5 GB log ingestion free

The bot currently uses SQLite (resets on redeploy). To persist data across deploys:
1. Create an RDS PostgreSQL instance (free tier)
2. Set `DATABASE_URL=postgresql://...` in EB env vars

---

## Troubleshooting

| Error | Fix |
|---|---|
| `403 Forbidden` from Polymarket | IP geo-blocked — verify you're deployed in eu-west-1 |
| `Missing credentials` | Check POLY_API_KEY/SECRET/PASSPHRASE in EB env vars |
| `No market found for condition_id` | Market expired — update POLY_MARKET_* vars |
| `USDC allowance too low` | Run `/api/approve-usdc` again |
| `Token resolution failed` | Market condition_id wrong or inactive |
| EB health `Degraded` | Check `eb logs` for Python errors |
