# ─────────────────────────────────────────────────────────────────────────
# SignalBot — Fly.io Dockerfile
# Python 3.11.9 to match runtime.txt / .python-version
# ─────────────────────────────────────────────────────────────────────────
FROM python:3.11.9-slim

# Don't write .pyc files, don't buffer stdout/stderr (logs show up immediately
# in `fly logs`)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps needed to build psycopg2 / gevent / numpy wheels on slim images
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (separate layer so code changes don't bust the
# pip-install cache)
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# Now copy the rest of the app
COPY . .

# Fly's internal_port (see fly.toml) — gunicorn binds here directly instead
# of relying on a $PORT env var, since Fly doesn't inject one by default.
EXPOSE 8080

# IMPORTANT: -w 1 (single worker) is required. APScheduler runs in-process
# inside this one worker; a second worker or a second machine would double
# every signal generation and every order execution. Do not raise this, and
# do not let Fly autoscale/idle this app (enforced in fly.toml).
CMD ["gunicorn", \
     "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", \
     "-w", "1", \
     "--timeout", "120", \
     "--bind", "0.0.0.0:8080", \
     "wsgi:app"]
