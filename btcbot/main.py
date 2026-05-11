"""
Fallback entrypoint — identical to wsgi.py.
Render sometimes auto-detects main.py and tries uvicorn main:app.
This file makes that work correctly with gunicorn too.
"""
import os, logging, threading
logger = logging.getLogger(__name__)

from app import app, socketio
from scheduler import start_scheduler
from signal_engine import retrain_all

def _background_init():
    with app.app_context():
        try:
            logger.info("[INIT] Training ML models...")
            retrain_all(limit=300)
        except Exception as e:
            logger.error(f"[INIT] Error: {e}")

threading.Thread(target=_background_init, daemon=True).start()
start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
