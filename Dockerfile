FROM python:3.11.9-slim

# System deps:
# - build-essential + libpq-dev: needed to build psycopg2-binary / gevent wheels on slim images
# - curl: used by Fly's healthcheck debugging only (optional, tiny)
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Fly injects PORT; gunicorn binds to it at runtime via fly.toml's cmd/entrypoint.
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["gunicorn", "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", \
     "-w", "1", "--timeout", "120", "--bind", "0.0.0.0:8080", "wsgi:app"]
