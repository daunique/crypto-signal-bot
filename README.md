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
