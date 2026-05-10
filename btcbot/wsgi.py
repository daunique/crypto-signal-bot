"""
Render entrypoint — initialises models and scheduler before gunicorn serves
"""
import logging
import os

logger = logging.getLogger(__name__)

from app import app, socketio
from extensions import db
from scheduler import start_scheduler
from signal_engine import retrain_all

# Run initial training in background (don't block startup)
import threading

def _background_init():
    with app.app_context():
        try:
            logger.info("[INIT] Training ML models on startup...")
            retrain_all(limit=300)
            logger.info("[INIT] Models ready")
        except Exception as e:
            logger.error(f"[INIT] Model training failed: {e}")

threading.Thread(target=_background_init, daemon=True).start()
start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
