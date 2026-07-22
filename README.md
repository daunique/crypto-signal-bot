# Deriv Higher/Lower Bot v3

Production-oriented 3-minute Higher/Lower bot for Deriv Options trading.

## Authentication architecture

This version uses the current PAT application flow:

1. `DERIV_APP_ID` identifies the PAT application.
2. `DERIV_PAT` is sent as `Authorization: Bearer <PAT>` to the current REST API.
3. The bot discovers the configured demo/real Options account, unless `DERIV_ACCOUNT_ID` is pinned.
4. The bot requests a short-lived OTP WebSocket URL.
5. The bot connects directly to that returned authenticated WebSocket URL.
6. No legacy `authorize(token)` request is used anywhere.

The market tick stream uses the public WebSocket. Trading operations use the authenticated WebSocket.

## Fly.io secrets

```bash
fly secrets set \
  DERIV_APP_ID="YOUR_PAT_APP_ID" \
  DERIV_PAT="YOUR_FULL_PERSONAL_ACCESS_TOKEN" \
  -a crypto-signal-bot-kooj9a
```

Then keep the app in demo mode:

```bash
fly secrets set BOT_MODE="demo" -a crypto-signal-bot-kooj9a
```

The PAT must belong to the PAT application and have the required `trade` scope. Do not put the PAT in `fly.toml`, Dockerfile, frontend code, Git, or logs.

## Deploy

```bash
fly deploy -a crypto-signal-bot-kooj9a
fly logs -a crypto-signal-bot-kooj9a
```

The service listens on port 8080 and exposes `/health`.

## Market-specific barriers

There is no universal barrier. Configure each market explicitly:

```env
MARKET_SYMBOL=R_25
MARKET_BARRIERS=R_25=YOUR_TESTED_R25_BARRIER,R_10=YOUR_TESTED_R10_BARRIER,R_100=YOUR_TESTED_R100_BARRIER
```

If the selected market has no configured barrier, the bot refuses to execute.

## Signal timing

At the first tick received in a new UTC 3-minute interval, the engine triggers the boundary evaluation, fetches the completed previous candle, evaluates the strategy, and maps:

* `UP` -> `HIGHER` with a positive relative barrier
* `DOWN` -> `LOWER` with a negative relative barrier

No automatic daily drawdown stop is implemented.
