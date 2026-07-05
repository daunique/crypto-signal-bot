# POLYBOT — Fly.io Dockerfile
FROM python:3.11-slim

WORKDIR /app

# System deps needed by some Python packages (cryptography, etc.
# used transitively by py-clob-client-v2's signing dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ is where the persistent Fly volume gets mounted (see
# fly.toml) — trades.db, runtime_settings.json, and the derived
# API creds debug file all live here and MUST survive restarts/
# redeploys, unlike the rest of the container filesystem.
RUN mkdir -p /app/data

EXPOSE 5000

CMD ["python3", "main.py"]
