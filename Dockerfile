# Fly.io deploys this as a headless background worker — no HTTP port needed,
# it only makes outbound connections (Polymarket REST + WebSocket).
FROM python:3.12-slim

# build-essential + libssl-dev: some crypto/signing dependencies pulled in by
# py-clob-client-v2 (eth-account, coincurve, etc.) compile native extensions.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY momentum_bot.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Trade log / day-state / cached API creds live on the mounted Fly Volume so
# they survive restarts and deploys — see [mounts] in fly.toml.
ENV BOT_STATE_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["./entrypoint.sh"]
