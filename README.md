# Deriv Higher/Lower Bot

Production-oriented 3-minute Higher/Lower bot for Volatility 25 Index.

## Current Deriv API authentication

This build uses the current PAT application flow. It does **not** use the legacy `wss://ws.derivws.com/websockets/v3?app_id=...` + `authorize` flow.

1. REST: authenticate with `Deriv-App-ID` + `Authorization: Bearer <PAT>`
2. REST: discover the demo/real Options account, unless `DERIV_ACCOUNT_ID` is explicitly set
3. REST: request a short-lived OTP WebSocket URL
4. WebSocket: connect to the returned authenticated URL
5. Public WebSocket: stream market ticks separately

This separation prevents concurrent reads from one WebSocket from corrupting request/response handling.

## Fly.io secrets

```bash
fly secrets set \
  DERIV_APP_ID="YOUR_PAT_APP_ID" \
  DERIV_PAT="YOUR_FULL_PERSONAL_ACCESS_TOKEN" \
  BOT_MODE="demo" \
  -a crypto-signal-bot-kooj9a
```

Optional if you want to pin the account: `DERIV_ACCOUNT_ID="DOT..."`.

The PAT must belong to the PAT application and include the `trade` scope. Do not put the token in `fly.toml`, Dockerfile, frontend code, Git, or logs.

## Deploy

```bash
fly deploy
fly logs -a crypto-signal-bot-kooj9a
```

The service listens on port 8080 and exposes `/health`.

## Local

```bash
cp .env.example .env
docker compose up --build
```

## Runtime

At each exact UTC 3-minute boundary, the bot uses completed candle data only, evaluates the strategy, and if qualified maps UP to `HIGHER` with a positive relative barrier and DOWN to `LOWER` with a negative relative barrier. There is no automatic daily drawdown stop.

## Market-specific barriers

The bot does not contain a universal barrier value. Configure barriers explicitly per symbol:

```env
MARKET_SYMBOL=R_25
MARKET_BARRIERS=R_25=YOUR_TESTED_R25_BARRIER,R_10=YOUR_TESTED_R10_BARRIER,R_100=YOUR_TESTED_R100_BARRIER
```

If the selected market has no configured barrier, the bot refuses to execute the trade. This prevents a barrier tested for one Volatility Index from being accidentally reused on another.
