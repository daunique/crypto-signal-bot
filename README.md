# Deriv Higher/Lower Bot

Production-oriented 3-minute Deriv Higher/Lower bot.

## Trading contract semantics

The bot predicts the direction of the next complete 3-minute candle:

* `UP` signal -> Deriv `HIGHER` -> Above Spot
* `DOWN` signal -> Deriv `LOWER` -> Below Spot

The strategy is evaluated from completed candles only. A qualified signal is created on the first observed tick belonging to the new 180-second candle boundary and the proposal is requested immediately. The system contains no manually configured barrier logic and does not require a barrier to trade.

## Preserved behavior

* No automatic maximum daily drawdown stop
* Daily signal tracking
* Daily PnL history
* Win rate
* Pending/open signals and trades
* Dashboard navigation
* Demo/live mode
* PAT authentication
* SQLite by default and PostgreSQL-compatible DATABASE_URL

## Reliability

The Deriv client uses separate public and trade WebSockets, bounded handshake timeouts, retrying handshakes, request timeouts, reader health checks, safe handling of non-text and non-JSON messages, and engine-level exponential reconnect backoff.

Transient settlement errors are retried until the settlement deadline. A technical reconnect does not activate a financial drawdown stop.

## Local run

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8080`.

## Fly.io

Set secrets:

```bash
fly secrets set DERIV_APP_ID="..." DERIV_PAT="..." BOT_MODE="demo" -a crypto-signal-bot-kooj9a
fly deploy -a crypto-signal-bot-kooj9a
```

Use `BOT_MODE=live` only when you intentionally want live account selection. Keep credentials in Fly secrets. Never commit `.env`.

## Runtime

At each 180-second UTC candle boundary:

1. The previous candle is finalized.
2. Completed-candle strategy features are evaluated.
3. A qualified direction is mapped to HIGHER or LOWER.
4. A barrier-free proposal is requested.
5. The proposal is bought at the configured stake.
6. The contract is polled until settlement.
7. Trade result and PnL are persisted.

## Deployment verification

```bash
curl https://YOUR_APP.fly.dev/health
```

Port `8080` is used consistently by the container, Docker Compose, FastAPI, and Fly.io.
