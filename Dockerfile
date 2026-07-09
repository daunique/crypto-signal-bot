FROM python:3.11.9-slim

# Build deps for any packages without prebuilt wheels (gevent/numpy/etc. usually
# have manylinux wheels, but build-essential is kept as a safety net).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Fly's health checks / proxy talk to this port (see fly.toml internal_port).
ENV PORT=8080
EXPOSE 8080

# Same process as the Render Procfile: single gevent-websocket worker, since
# the scheduler + ML models live in-process and must not be split across workers.
CMD ["gunicorn", "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", \
     "-w", "1", "--timeout", "120", "--bind", "0.0.0.0:8080", "wsgi:app"]
