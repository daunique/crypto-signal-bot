# Deriv Higher/Lower Bot

A production-oriented starter system for a 3-minute Deriv Higher/Lower bot.

## Architecture

- FastAPI backend
- Async Deriv WebSocket client
- Exact 3-minute candle boundary signal evaluation
- Higher signal -> Higher / Above Spot
- Lower signal -> Lower / Below Spot
- Demo / Live mode
- SQLite by default, PostgreSQL-ready via DATABASE_URL
- Real-time dashboard updates via WebSocket
- PnL, signals, trades, daily history
- Fly.io deployment configuration
- API credentials loaded only from environment variables / Fly secrets

## Important

The included strategy is intentionally modular. `R25Strategy` is a starter implementation based on the current confluence framework. Before live-money use, verify Deriv proposal semantics and contract parameters against your account and run the system in demo mode.

## Local run

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000`.

## Fly.io

Create the app, then set secrets:

```bash
fly launch --no-deploy
fly secrets set DERIV_APP_ID="..."
fly secrets set DERIV_DEMO_TOKEN="..."
fly secrets set DERIV_LIVE_TOKEN="..."
fly secrets set BOT_MODE="demo"
fly deploy
```

For durable production data, set `DATABASE_URL` to a managed PostgreSQL connection string or attach a Fly volume if using SQLite.

## Runtime model

At each exact UTC 3-minute boundary:

1. Finalize the previous candle.
2. Calculate features from completed candles only.
3. Evaluate the strategy.
4. If qualified, create a Higher or Lower signal.
5. Request a Deriv proposal.
6. Buy only when proposal conditions are valid.
7. Track settlement.
8. Persist the complete event and update the dashboard.

The bot does not impose an automatic daily drawdown stop. Drawdown is tracked for analytics. Manual emergency stop and technical circuit breakers remain available.

## Deployment verification

The application exposes a build fingerprint at `/health`. After deploying, verify the running image:

```bash
fly deploy -a crypto-signal-bot-kooj9a --remote-only
curl https://crypto-signal-bot-kooj9a.fly.dev/health
```

Expected build value:

```text
2026-07-22-pat-boundary-fix-1
```

If `/health` does not show this build value, the new source is not the source running in Fly.io.

Required secrets:

```bash
fly secrets set \
  DERIV_APP_ID="YOUR_PAT_APP_ID" \
  DERIV_PAT="YOUR_FULL_PAT_TOKEN" \
  MARKET_BARRIERS="R_25=YOUR_R25_BARRIER" \
  -a crypto-signal-bot-kooj9a
```

The PAT token is sent only as a Bearer token to the current Deriv REST API. The bot does not call the legacy `authorize` WebSocket method.
