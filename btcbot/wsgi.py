"""
Render/Gunicorn entrypoint.
Eventlet monkey-patch MUST happen before any other import.
"""
import eventlet
eventlet.monkey_patch()

import os
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Import app AFTER monkey-patch
from app import app, socketio

def _background_init():
    with app.app_context():
        try:
            logger.info("[INIT] Training ML models on startup...")
            from signal_engine import retrain_all
            retrain_all(limit=300)
            logger.info("[INIT] Models ready")
        except Exception as e:
            logger.error(f"[INIT] Model training failed: {e}")

def _start_scheduler():
    try:
        from scheduler import start_scheduler
        start_scheduler()
        logger.info("[INIT] Scheduler started")
    except Exception as e:
        logger.error(f"[INIT] Scheduler failed to start: {e}")

# Start background tasks
threading.Thread(target=_background_init, daemon=True).start()
_start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
