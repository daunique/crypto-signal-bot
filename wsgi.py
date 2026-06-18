"""
Render/Gunicorn entrypoint.
gevent monkey_patch MUST be first before any other import.
psycogreen makes psycopg2 gevent-compatible (patches its wait callback).
"""
from gevent import monkey
monkey.patch_all()

# Make psycopg2 cooperate with gevent — must come right after monkey.patch_all()
try:
    from psycogreen.gevent import patch_psycopg
    patch_psycopg()
except ImportError:
    pass  # psycogreen not installed — will work but may block briefly on DB calls

import os
import logging
import threading

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

from app import app, socketio


def _background_init():
    with app.app_context():
        try:
            logger.info("[INIT] Training ML models for all 6 pairs...")
            from signal_engine import retrain_all
            retrain_all(limit=960)
            logger.info("[INIT] All models ready — signals enabled")
        except Exception as e:
            logger.error(f"[INIT] Model training failed: {e}")


def _start_scheduler():
    try:
        from scheduler import start_scheduler
        start_scheduler()
        logger.info("[INIT] Scheduler started")
    except Exception as e:
        logger.error(f"[INIT] Scheduler failed: {e}")


threading.Thread(target=_background_init, daemon=True).start()
_start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
