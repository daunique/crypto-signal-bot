FROM python:3.12-slim

WORKDIR /app

# Only what the capture script actually needs — kept minimal so
# builds/deploys stay fast.
RUN pip install --no-cache-dir websockets requests

COPY capture/capture_market_lifecycle.py capture/capture_market_lifecycle.py
COPY cloud/run_forever.py cloud/run_forever.py

ENV CAPTURE_OUTPUT_DIR=/data
ENV RESTART_AFTER_HOURS=6

CMD ["python3", "cloud/run_forever.py"]
